"""本地页面渲染：把一页 PDF 变成一张 PNG。

plan 第 37 行：没有文本层的页面（扫描件、照片、讲义图片）要在**本地**渲染成页面图片，再交给
BYOK 多模态模型读。这一层只做前面半步——渲染——因为它零网关依赖，而且它有自己的一串陷阱。

三件事在这里定死：

1. **像素通道顺序**。PDFium 给的是 BGR（实测：一页红色方块读回来是 `[0, 0, 255]`，且
   `bitmap.format == 2`）。搞错了不会报错，只会让红蓝互换——而这是要交给视觉模型的图。
2. **像素上限**。一页海报尺寸的扫描件在 scale=2 下能到三千多万像素，多模态模型要么拒收，
   要么按尺寸收费。上限挡在这里，而不是等网关回一个用户看不懂的错。
3. **不引入 PIL**。打包时 PIL 是被排除的（它只被 pypdfium2 的惰性 `import PIL.Image` 拉
   进来）。为编码 PNG 把那条依赖链带回来不划算，所以编码器用 `zlib` + `struct` 自己写。

这一层只做渲染，**不判断这一页该不该走视觉路径**：那是调用方的事，而且判断依据（这一页有没有
文本层）已经由解析器写在 `kind="image_page"` 上了。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import math
import struct
import zlib

import numpy as np
import pypdfium2 as pdfium

from .store import StoreError

__all__ = [
    "MAX_PIXELS",
    "RENDER_SCALE",
    "RenderedPage",
    "RenderingError",
    "render_page",
]

#: 默认缩放。PDF 用户空间的单位是 1/72 英寸，所以 scale=2 等于 144 DPI——扫描页在这个
#: 分辨率下正文笔画仍然清楚，而 A4 只是 1224×1584，落在常见视觉模型的输入范围内。
RENDER_SCALE = 2.0

#: 单页渲染的像素上限。约等于 2000×2000，是常见多模态模型输入上限的量级。它不是"越大越好"
#: 的旋钮：超出的部分既换不来识别率，又要按尺寸付费。
MAX_PIXELS = 4_000_000

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class RenderingError(StoreError):
    """这一页渲染不出来。"""


@dataclass(frozen=True, slots=True)
class RenderedPage:
    """一页的位图，以及它有多大。"""

    page_number: int
    image: bytes
    width: int
    height: int


def _fit_scale(width_points: float, height_points: float, *, scale: float) -> float:
    """在像素预算内尽量用请求的缩放，超了就等比缩下来。"""
    pixels = (width_points * scale) * (height_points * scale)
    if pixels <= MAX_PIXELS:
        return scale
    return scale * math.sqrt(MAX_PIXELS / pixels)


def _encode_png(width: int, height: int, rgb: bytes) -> bytes:
    """最小 PNG 编码器：8 位真彩、每行 filter 0、zlib 压缩。

    自己写而不是用 PIL：PIL 在打包时是被排除的，而这里需要的能力只有"把一块 RGB 字节写成
    PNG"这一件事。`struct` 保证字节序和字段宽度都明确，比"某天 PIL 换了版本"更容易长期信任。
    """

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    stride = width * 3
    raw = bytearray()
    for row in range(height):
        raw.append(0)  # filter type 0 (None)
        raw += rgb[row * stride:(row + 1) * stride]
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        _PNG_MAGIC
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )


def render_page(
    path: str | Path, page_number: int, *, scale: float = RENDER_SCALE,
) -> RenderedPage:
    """把第 `page_number` 页（**1 基**，与 `page:N` 位置对齐）渲染成 PNG。

    页号越界、文件打不开都抛错，**不返回一张空白图**：空白图会让视觉模型对着"一页白纸"
    编出一段描述，而没有任何地方显示这一页根本没读到。
    """
    if scale <= 0:
        raise RenderingError(f"缩放必须是正数，收到 {scale}")
    if page_number < 1:
        raise RenderingError(f"页号从 1 开始，收到 {page_number}")

    source = Path(path)
    try:
        document = pdfium.PdfDocument(source)
    except (pdfium.PdfiumError, OSError) as exc:
        raise RenderingError(f"{source.suffix} 打不开：{exc}") from exc

    try:
        total = len(document)
        if page_number > total:
            raise RenderingError(
                f"这份文档只有 {total} 页，却要求渲染第 {page_number} 页"
            )
        page = document[page_number - 1]
        try:
            width_points, height_points = page.get_size()
            effective = _fit_scale(width_points, height_points, scale=scale)
            bitmap = page.render(scale=effective)
            try:
                # PDFium 的通道顺序是 BGR（实测 format=2）而 PNG 要 RGB，所以要反转。
                array = np.ascontiguousarray(bitmap.to_numpy()[:, :, ::-1])
                height, width = array.shape[0], array.shape[1]
                return RenderedPage(
                    page_number=page_number,
                    image=_encode_png(width, height, array.tobytes()),
                    width=width,
                    height=height,
                )
            finally:
                bitmap.close()
        finally:
            page.close()
    finally:
        document.close()
