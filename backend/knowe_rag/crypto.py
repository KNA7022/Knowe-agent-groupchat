"""加密原语：主密钥的保护，以及"字段级"的 AES-256-GCM 封装。

plan 第 49 行要求源文件、抽取文本、chunk、向量与缓存都用 AES-256-GCM 加密，只留不透明的
元数据明文。这一层负责其中两件最容易做错的事：

1. **密钥放哪里**。密钥与密文放在一起等于没加密；让用户每次输密码又与"本地优先、打开就用"
   冲突。这里用 Windows DPAPI：密钥文件可以被复制，但只在**同一个用户、同一台机器**上解得
   开——对应的正是真实威胁（数据目录进了备份、共用电脑、云同步文件夹）。
2. **密文绑到哪里**。AAD 里带上记录 id 和字段名，于是密文不能从一行挪到另一行、也不能从
   一列挪到另一列。没有这层绑定，能写数据库的人把两个值对调，两边都还能正常解开。

**主密钥文件损坏时绝不重新生成**：那会让已有数据永久不可读，而用户看到的是"一切正常、
只是以前的资料不见了"。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import hashlib
import hmac
import os
from pathlib import Path
from typing import Callable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = [
    "ContentHasher",
    "MasterKey",
    "MasterKeyError",
    "field_aad",
    "keyed_content_hasher",
]

#: 内容指纹函数的形状。`chunks.build_chunks` 接受一个，于是"指纹怎么算"可以由调用方决定。
ContentHasher = Callable[[str], str]

#: GCM 的 nonce 长度。随机 96 位在 2^96 的空间里碰撞概率可忽略，比计数器安全——计数器一旦
#: 在复用密钥时归零，就是灾难性的失密。
_NONCE_BYTES = 12

#: GCM 的认证标签长度。
_TAG_BYTES = 16

_KEY_BYTES = 32

#: DPAPI 的 blob 以这段固定的头开始。用它区分"这是受保护的密钥"和"这是有人手写的文件"。
_DPAPI_MAGIC = bytes.fromhex("01000000d08c9ddf0115d1118c7a00c04fc297eb")

#: 额外的熵把 blob 绑到这个应用上。它写在源码里，因此**不是一个秘密**——它的作用是让"随手
#: 拿另一个程序解一下"不成立，而不是让拿到源码的人解不开。真正的保护来自 DPAPI 本身。
_DPAPI_ENTROPY = b"studycrew.rag.master-key.v1"

_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class MasterKeyError(RuntimeError):
    """主密钥不可用，或者密文没能通过校验。"""


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob_from(data: bytes) -> _DataBlob:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))


def _blob_bytes(blob: _DataBlob) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def _dpapi_protect(plaintext: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    incoming = _blob_from(plaintext)
    entropy = _blob_from(_DPAPI_ENTROPY)
    outgoing = _DataBlob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(incoming), None, ctypes.byref(entropy), None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(outgoing),
    )
    if not ok:
        raise MasterKeyError(
            f"DPAPI 无法保护主密钥（错误码 {ctypes.GetLastError()}）："
            "当前用户配置下无法安全保存密钥"
        )
    try:
        return _blob_bytes(outgoing)
    finally:
        kernel32.LocalFree(outgoing.pbData)


def _dpapi_unprotect(protected: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    incoming = _blob_from(protected)
    entropy = _blob_from(_DPAPI_ENTROPY)
    outgoing = _DataBlob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(incoming), None, ctypes.byref(entropy), None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(outgoing),
    )
    if not ok:
        raise MasterKeyError(
            f"DPAPI 无法解开主密钥（错误码 {ctypes.GetLastError()}）："
            "这份密钥不属于当前用户或当前机器，已加密的资料无法读取"
        )
    try:
        return _blob_bytes(outgoing)
    finally:
        kernel32.LocalFree(outgoing.pbData)


def field_aad(record_id: str, field: str) -> bytes:
    """把密文绑定到"哪条记录的哪个字段"。

    分隔符必须在两部分**之间**：直接拼接的话 `("a", "b")` 和 `("ab", "")` 会得到同一串
    字节，于是"把密文从 a 记录搬到 ab 记录"这种事会悄悄通过认证——正好是这层绑定要挡的。
    """
    return f"{record_id}\x00{field}".encode("utf-8")


@dataclass(frozen=True, slots=True)
class MasterKey:
    """一个 32 字节的 AES-256 主密钥，以及围绕它的封/解封装。"""

    #: 仅供测试与诊断使用。不要写进日志、不要持久化、不要放进异常消息。
    key_material: bytes

    def __post_init__(self) -> None:
        if len(self.key_material) != _KEY_BYTES:
            raise MasterKeyError(f"主密钥必须是 {_KEY_BYTES} 字节，收到 {len(self.key_material)}")

    @classmethod
    def load_or_create(cls, path: Path) -> "MasterKey":
        """读出已有密钥；没有就生成一个。**存在但读不出来时抛错，绝不覆盖。**

        静默换一把新钥匙会让所有已加密的资料永久不可读，而界面看起来一切正常——这是这类
        功能能造成的最坏结果，所以这里的默认动作是拒绝启动而不是"修复"。
        """
        path = Path(path)
        if path.exists():
            return cls._load(path)
        key = cls(key_material=os.urandom(_KEY_BYTES))
        key._store(path)
        return key

    @classmethod
    def _load(cls, path: Path) -> "MasterKey":
        try:
            protected = path.read_bytes()
        except OSError as exc:
            raise MasterKeyError(f"读不出主密钥文件 {path.name}：{exc}") from exc
        if not protected.startswith(_DPAPI_MAGIC):
            raise MasterKeyError(
                f"{path.name} 不是一份受 DPAPI 保护的主密钥（文件头不对）。"
                "它可能被别的程序覆盖或截断了；这里不会重新生成密钥，"
                "因为那会让已经加密的资料永久读不出来"
            )
        material = _dpapi_unprotect(protected)
        if len(material) != _KEY_BYTES:
            raise MasterKeyError(
                f"{path.name} 解出的主密钥长度是 {len(material)} 字节，期望 {_KEY_BYTES}"
            )
        return cls(key_material=material)

    def _store(self, path: Path) -> None:
        protected = _dpapi_protect(self.key_material)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再替换：中途断电只会留下一个临时文件，不会留下半个密钥文件——
        # 半个密钥文件在下次启动时会被上面那条"文件头不对"拦下，然后用户就再也起不来了。
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_bytes(protected)
        os.replace(temporary, path)

    def seal(self, plaintext: bytes, *, aad: bytes) -> bytes:
        """加密并认证。返回 `nonce || ciphertext||tag`，可以直接当 BLOB 存。"""
        nonce = os.urandom(_NONCE_BYTES)
        return nonce + AESGCM(self.key_material).encrypt(nonce, plaintext, aad)

    def open(self, blob: bytes, *, aad: bytes) -> bytes:  # noqa: A003 - mirrors "seal"
        if len(blob) < _NONCE_BYTES + _TAG_BYTES:
            raise MasterKeyError(
                f"密文只有 {len(blob)} 字节，连一个 nonce 加认证标签都不够，它已经损坏"
            )
        nonce, sealed = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
        try:
            return AESGCM(self.key_material).decrypt(nonce, sealed, aad)
        except InvalidTag as exc:
            # 两种可能与用户有关、且处理方式不同的原因，所以两条都要说出来。
            raise MasterKeyError(
                "密文没有通过认证：它要么被改动过，要么不属于这个位置（记录或字段对不上）"
            ) from exc


def keyed_content_hasher(master_key: "MasterKey") -> ContentHasher:
    """把内容指纹**绑定到主密钥**上（HMAC-SHA256）。

    无盐 `sha256(明文)` 的问题不是"能从指纹反推原文"，而是**库文件变成一台免费的确认机**：
    这个产品的块经常很短（姓名+学号+电话、一行成绩、一句评语），候选空间很小，穷举得出来；
    对一份已知文档也能立刻验证"这句话在不在库里"。加密保护的正是这类数据，而指纹把其中可猜
    的那部分变回了明文。

    绑密钥不削弱去重：同一个库里相同文本仍然得到同一个指纹，所以"同一段话只算一次向量"
    照常成立。代价是**换主密钥必须重建索引**——指纹会全变，而那是正确的：换了钥匙的库本来
    就不该被当成同一个库。
    """

    def hasher(text: str) -> str:
        return hmac.new(
            master_key.key_material, str(text).encode("utf-8"), hashlib.sha256,
        ).hexdigest()

    return hasher