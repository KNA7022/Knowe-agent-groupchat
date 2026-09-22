"""本地页面渲染：把一页 PDF 变成一张 PNG。

plan 第 37 行：没有文本层的页面（扫描件、照片、讲义图片）要在**本地**渲染成页面图片，再交给
BYOK 多模态模型读。这一层只做前面半步——渲染——因为它零网关依赖，而且它有自己的一串陷阱。

三件事在这里定死：

1. **像素通道顺序**。PDFium 给的是 BGR（实测：红色方块读回来是 `[0, 0, 255]`）。搞错了不会
   报错，只会让红蓝互换——而这是要交给视觉模型的图。
2. **像素上限**。一页海报尺寸的扫描件渲染出来是几千万像素，多模态模型要么拒收要么按尺寸
   收费。上限在这里挡，而不是等网关报错。
3. **不引入 PIL**。打包时 PIL 是被排除的（它只被 pypdfium2 的惰性 import 拉进来）。为了编码
   PNG 把它和它的依赖链带回来不划算，所以编码器用 `zlib` + `struct` 自己写——测试里也自己
   解码，于是"自己写的编码器对不对"不靠另一个库来判断。
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from backend.knowe_rag.rendering import (
    MAX_PIXELS,
    RenderingError,
    render_page,
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _build_pdf(streams: list[str], *, media_box: str = "[0 0 612 792]") -> bytes:
    font = 3
    kids = " ".join(f"{4 + 2 * index} 0 R" for index in range(len(streams)))
    objects: dict[int, str] = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{kids}] /Count {len(streams)} >>",
        font: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for index, stream in enumerate(streams):
        page_number = 4 + 2 * index
        objects[page_number] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox {media_box} "
            f"/Contents {page_number + 1} 0 R "
            f"/Resources << /Font << /F1 {font} 0 R >> >> >>"
        )
        objects[page_number + 1] = f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"
    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n{objects[number]}\nendobj\n".encode("latin-1")
    start_xref = len(out)
    size = max(objects) + 1
    out += f"xref\n0 {size}\n".encode("latin-1")
    out += b"0000000000 65535 f \n"
    for number in range(1, size):
        out += f"{offsets[number]:010d} 00000 n \n".encode("latin-1")
    out += (
        f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{start_xref}\n%%EOF\n"
    ).encode("latin-1")
    return bytes(out)


def _decode_png(blob: bytes) -> tuple[int, int, bytes]:
    """最小 PNG 解码器，只为验证编码器。

    刻意不用第三方库：用另一个库来验证自己写的编码器，等于把"它俩约定一致"当成"它符合
    规范"。
    """
    assert blob[:8] == PNG_MAGIC, blob[:8]
    offset = 8
    header = b""
    payload = b""
    while offset < len(blob):
        length = struct.unpack(">I", blob[offset:offset + 4])[0]
        tag = blob[offset + 4:offset + 8]
        body = blob[offset + 8:offset + 8 + length]
        if tag == b"IHDR":
            header = body
        elif tag == b"IDAT":
            payload += body
        offset += 12 + length
    width, height, depth, colour = struct.unpack(">IIBB", header[:10])
    assert (depth, colour) == (8, 2), (depth, colour)
    raw = zlib.decompress(payload)
    stride = width * 3
    pixels = bytearray()
    for row in range(height):
        start = row * (stride + 1)
        assert raw[start] == 0, f"第 {row} 行的过滤器不是 0"
        pixels += raw[start + 1:start + 1 + stride]
    return width, height, bytes(pixels)


def _colour_pdf(path: Path) -> Path:
    """一个红色方块加一个绿色方块，左上都靠上。"""
    stream = "1 0 0 rg 0 592 200 200 re f 0 1 0 rg 200 592 200 200 re f"
    path.write_bytes(_build_pdf([stream]))
    return path


def _pixel(pixels: bytes, width: int, x: int, y: int) -> tuple[int, int, int]:
    offset = (y * width + x) * 3
    return (pixels[offset], pixels[offset + 1], pixels[offset + 2])


def _chunks(blob: bytes):
    offset = 8
    while offset < len(blob):
        length = struct.unpack(">I", blob[offset:offset + 4])[0]
        tag = blob[offset + 4:offset + 8]
        body = blob[offset + 8:offset + 8 + length]
        crc = struct.unpack(">I", blob[offset + 8 + length:offset + 12 + length])[0]
        yield tag, body, crc
        offset += 12 + length


# ── 基本形状 ──────────────────────────────────────────────────────────

def test_a_page_renders_to_a_png(tmp_path: Path) -> None:
    page = render_page(_colour_pdf(tmp_path / "a.pdf"), 1, scale=1)

    assert page.image.startswith(PNG_MAGIC)
    assert page.page_number == 1
    assert page.width > 0 and page.height > 0


def test_the_png_dimensions_follow_the_scale(tmp_path: Path) -> None:
    """A4 的 612×792 点在 scale=2 下是 1224×1584。

    尺寸不只是"看着对"：它决定视觉模型看到多少细节，也决定 token 花多少。
    """
    page = render_page(_colour_pdf(tmp_path / "b.pdf"), 1, scale=2)

    assert (page.width, page.height) == (1224, 1584)
    width, height, _ = _decode_png(page.image)
    assert (width, height) == (1224, 1584)


def test_the_reported_size_matches_the_encoded_image(tmp_path: Path) -> None:
    """`RenderedPage` 上的宽高必须与 PNG 里的一致。

    不一致时调用方按报出来的尺寸算预算，而实际发出去的图是另一个大小——两个数字都对，
    只是说的不是同一张图。
    """
    page = render_page(_colour_pdf(tmp_path / "c.pdf"), 1, scale=1)

    width, height, _ = _decode_png(page.image)
    assert (page.width, page.height) == (width, height)


# ── 像素（通道顺序的守门测试） ────────────────────────────────────────

def test_a_red_shape_comes_out_red_not_blue(tmp_path: Path) -> None:
    """PDFium 给的是 BGR。不做转换，红色会变成蓝色——而它不报错。

    这是这一层里唯一一个"错了也看不出"的地方：图还是那张图、尺寸还对、能解码、模型也能读，
    只是颜色不对。对这种图来说颜色可能正是内容（化学试剂的颜色、地图的图例）。
    """
    page = render_page(_colour_pdf(tmp_path / "d.pdf"), 1, scale=1)
    width, _, pixels = _decode_png(page.image)

    red = _pixel(pixels, width, 100, 100)
    green = _pixel(pixels, width, 300, 100)

    assert red[0] > 200 and red[1] < 60 and red[2] < 60, red
    assert green[1] > 200 and green[0] < 60 and green[2] < 60, green


def test_the_background_is_white_not_black(tmp_path: Path) -> None:
    """空白区域是白纸。

    透明或黑色背景会让扫描页在视觉模型眼里变成"一张黑图"，而这类失败往往被当成模型不灵。
    """
    page = render_page(_colour_pdf(tmp_path / "e.pdf"), 1, scale=1)
    width, _, pixels = _decode_png(page.image)

    assert _pixel(pixels, width, 500, 700) == (255, 255, 255)


def test_every_png_chunk_carries_a_valid_crc(tmp_path: Path) -> None:
    """每个 chunk 的 CRC 都要对。

    不校验 CRC 的图在宽松的解码器上看不出问题——`_decode_png` 就不看 CRC——但严格的解码器
    会直接拒绝，而那时错误说的是"图片格式不支持"，不是"CRC 算错了"，于是人会去查格式而不是
    查编码器。
    """
    page = render_page(_colour_pdf(tmp_path / "k.pdf"), 1, scale=1)

    chunks = list(_chunks(page.image))
    assert [tag for tag, _, _ in chunks] == [b"IHDR", b"IDAT", b"IEND"]
    for tag, body, crc in chunks:
        assert crc == zlib.crc32(tag + body) & 0xFFFFFFFF, tag


# ── 像素上限 ──────────────────────────────────────────────────────────

def test_a_huge_page_is_capped_by_the_pixel_budget(tmp_path: Path) -> None:
    """海报尺寸的页面要按像素预算缩下来。

    不缩的话，一页 3000×3000 点在 scale=2 下是 3600 万像素——多模态模型要么拒收，要么按
    尺寸收费。上限挡在这里，而不是等网关报一个用户看不懂的错。
    """
    path = tmp_path / "huge.pdf"
    path.write_bytes(_build_pdf(["0 0 1 rg 0 0 10 10 re f"], media_box="[0 0 3000 3000]"))

    page = render_page(path, 1, scale=2)

    assert page.width * page.height <= MAX_PIXELS, (page.width, page.height)
    assert page.width > 0 and page.height > 0


def test_a_page_under_the_budget_keeps_the_requested_scale(tmp_path: Path) -> None:
    """没超预算就不该被动过：悄悄缩小会让所有页面都损失细节。"""
    page = render_page(_colour_pdf(tmp_path / "f.pdf"), 1, scale=1)

    assert (page.width, page.height) == (612, 792)


def test_the_aspect_ratio_survives_the_cap(tmp_path: Path) -> None:
    """缩放要保持长宽比。压扁或拉长会让模型看到的版面与原文不符。"""
    path = tmp_path / "wide.pdf"
    path.write_bytes(_build_pdf(["0 0 1 rg 0 0 10 10 re f"], media_box="[0 0 4000 1000]"))

    page = render_page(path, 1, scale=2)

    assert abs((page.width / page.height) - 4.0) < 0.05, (page.width, page.height)


# ── 页号与失败 ────────────────────────────────────────────────────────

def test_page_numbers_are_one_based_like_the_locations(tmp_path: Path) -> None:
    """`render_page(path, 1)` 是第一页，与 `page:1` 这条位置对齐。

    差一位会让"第 1 页是扫描件"变成"渲染了第 2 页"，而引用仍然写 page:1——错的是图，
    不是位置。
    """
    path = _colour_pdf(tmp_path / "g.pdf")

    assert render_page(path, 1, scale=1).page_number == 1


@pytest.mark.parametrize("number", [0, -1, 4])
def test_a_page_number_out_of_range_is_refused(tmp_path: Path, number: int) -> None:
    with pytest.raises(RenderingError):
        render_page(_colour_pdf(tmp_path / "h.pdf"), number, scale=1)


def test_a_file_that_is_not_a_pdf_fails_loudly(tmp_path: Path) -> None:
    """与解析层同一条规矩：打不开就说打不开，不返回一张空白图。

    空白图会让视觉模型"看着一页白纸"编出一段描述，而没有任何地方显示这一页根本没读到。
    """
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"this is not a pdf")

    with pytest.raises(RenderingError):
        render_page(path, 1, scale=1)


def test_a_missing_file_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(RenderingError):
        render_page(tmp_path / "nothing-here.pdf", 1, scale=1)


def test_a_non_positive_scale_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RenderingError):
        render_page(_colour_pdf(tmp_path / "i.pdf"), 1, scale=0)


# ── 确定性 ────────────────────────────────────────────────────────────

def test_rendering_the_same_page_twice_gives_the_same_image(tmp_path: Path) -> None:
    """同一页渲染两次要得到同样的 PNG。

    不确定的话，"这一页的图变了没有"就无法判断，而按内容指纹去重向量化的那套做法在这里
    也就失效了。
    """
    path = _colour_pdf(tmp_path / "j.pdf")

    assert render_page(path, 1, scale=1).image == render_page(path, 1, scale=1).image
