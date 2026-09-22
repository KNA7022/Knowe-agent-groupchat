"""主密钥与字段级封装的测试。

这一层要挡的两件事都不在"能不能加解密"上——那条自然会过——而在两个反面上：

1. 密文被搬到别的位置还能不能解开（能解开就说明绑定没做）；
2. 主密钥文件坏掉时会不会被悄悄换掉（换掉就等于所有旧资料永久报废）。

所以大部分测试是负向的。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend.knowe_rag.crypto import MasterKey, MasterKeyError, field_aad


def _key() -> MasterKey:
    return MasterKey(key_material=os.urandom(32))


# ── 正常路径 ──────────────────────────────────────────────────────────

def test_sealing_round_trips() -> None:
    key = _key()
    aad = field_aad("chunk-1", "text")

    blob = key.seal("一段学习笔记".encode("utf-8"), aad=aad)

    assert key.open(blob, aad=aad) == "一段学习笔记".encode("utf-8")


def test_an_empty_plaintext_round_trips() -> None:
    """空值也要能封能解。

    它的密文并不空（带 nonce 和标签），如果把它当成"没有值"，那么"这一栏确实是空的"和
    "这一栏根本没读到"就分不开了。
    """
    key = _key()
    aad = field_aad("chunk-1", "text")

    blob = key.seal(b"", aad=aad)

    assert blob != b""
    assert key.open(blob, aad=aad) == b""


def test_two_seals_of_the_same_text_differ() -> None:
    """同一段明文封装两次必须得到不同密文。

    相同就说明 nonce 被复用了，而 GCM 下 nonce 复用不是"能看出两段一样"，是密钥流复用：
    两份密文异或就还原出明文，认证也一并失效。
    """
    key = _key()
    aad = field_aad("chunk-1", "text")
    plaintext = b"same text"

    assert key.seal(plaintext, aad=aad) != key.seal(plaintext, aad=aad)


# ── 绑定：密文不能换位置 ──────────────────────────────────────────────

def test_a_blob_sealed_for_one_record_does_not_open_for_another() -> None:
    """密文绑定到记录：把 A 记录的密文搬到 B 记录必须打不开。

    没有这层绑定，能写数据库的人可以把两个 chunk 的正文对调，两边都正常解开，于是引用
    指向的位置和内容对不上——"这条结论出自原文第 3 页"这句话从此不可信。
    """
    key = _key()
    blob = key.seal("甲".encode("utf-8"), aad=field_aad("chunk-1", "text"))

    with pytest.raises(MasterKeyError):
        key.open(blob, aad=field_aad("chunk-2", "text"))


def test_a_blob_sealed_for_one_field_does_not_open_for_another() -> None:
    """同一行的不同字段之间也要挡住：正文的密文不能冒充向量。"""
    key = _key()
    blob = key.seal("甲".encode("utf-8"), aad=field_aad("chunk-1", "text"))

    with pytest.raises(MasterKeyError):
        key.open(blob, aad=field_aad("chunk-1", "embedding"))


def test_field_aad_separates_its_two_parts() -> None:
    """两部分之间要有分隔符。

    直接拼接的话 `("a", "b")` 与 `("ab", "")` 会得到同一串字节，于是"把密文从 a 记录搬到
    ab 记录"这种事会悄悄通过认证——正好是这层绑定要挡的。
    """
    assert field_aad("a", "b") != field_aad("ab", "")
    assert field_aad("a", "b") != field_aad("a", "b\x00")


# ── 完整性 ────────────────────────────────────────────────────────────

def test_a_tampered_ciphertext_is_rejected() -> None:
    key = _key()
    aad = field_aad("chunk-1", "text")

    for position in (0, 13, -1):
        blob = bytearray(key.seal(b"secret", aad=aad))
        blob[position] ^= 0x01
        with pytest.raises(MasterKeyError):
            key.open(bytes(blob), aad=aad)


@pytest.mark.parametrize("blob", [b"", b"\x00" * 12, b"\x00" * 27])
def test_a_blob_too_short_to_hold_a_nonce_and_a_tag_is_rejected(blob: bytes) -> None:
    """连 nonce 加标签都不够的输入要在解密**之前**被拦下，而且要说清是"太短"。

    少了这一步它照样会失败——GCM 自然解不开——但报出来的会是"没有通过认证"，于是"这份
    数据被截断了"和"这份数据被改过"变成同一句话，而这两件事该做的处理不一样。
    """
    key = _key()

    with pytest.raises(MasterKeyError) as caught:
        key.open(blob, aad=field_aad("chunk-1", "text"))

    message = str(caught.value)
    assert "损坏" in message, message


def test_a_master_key_of_the_wrong_length_is_refused() -> None:
    """AES-256 的密钥必须是 32 字节。短密钥被接受的话，实际强度会悄悄低于声称的。"""
    for length in (0, 16, 31, 33):
        with pytest.raises(MasterKeyError):
            MasterKey(key_material=os.urandom(length))


# ── 密钥文件 ──────────────────────────────────────────────────────────

def test_the_key_file_does_not_contain_the_key(tmp_path: Path) -> None:
    """密钥文件里不能有明文密钥。

    明文写进同一个目录，整套加密就只是改了扩展名：任何拿到这个目录的人（备份、同步盘、
    另一台机器上的副本）都能直接解开。
    """
    path = tmp_path / "master.key"

    key = MasterKey.load_or_create(path)
    raw = path.read_bytes()

    assert key.key_material not in raw
    assert len(raw) > len(key.key_material), "受保护的 blob 应该有额外的头部与校验数据"


def test_the_same_key_file_yields_the_same_key(tmp_path: Path) -> None:
    """两次启动必须得到同一把钥匙，否则重启之后之前存的资料全都读不出来。"""
    path = tmp_path / "master.key"

    first = MasterKey.load_or_create(path)
    second = MasterKey.load_or_create(path)

    assert first.key_material == second.key_material
    blob = first.seal(b"persisted", aad=field_aad("r", "text"))
    assert second.open(blob, aad=field_aad("r", "text")) == b"persisted"


def test_two_key_files_are_independent(tmp_path: Path) -> None:
    first = MasterKey.load_or_create(tmp_path / "a.key")
    second = MasterKey.load_or_create(tmp_path / "b.key")

    assert first.key_material != second.key_material
    blob = first.seal(b"secret", aad=field_aad("r", "text"))
    with pytest.raises(MasterKeyError):
        second.open(blob, aad=field_aad("r", "text"))


def test_creating_a_key_creates_the_parent_directory(tmp_path: Path) -> None:
    """首次启动时数据目录还不存在。这里失败会让整个知识库在第一步就起不来。"""
    path = tmp_path / "nested" / "deeper" / "master.key"

    MasterKey.load_or_create(path)

    assert path.exists()


def test_a_corrupted_key_file_fails_loudly_and_is_not_replaced(tmp_path: Path) -> None:
    """损坏的密钥文件必须报错，而且**不能**被换成一把新钥匙。

    换掉之后所有已加密的资料永久读不出来，界面却一切正常——用户只会发现"以前的资料不见
    了"，而且没有任何办法恢复。所以这里的默认动作是拒绝启动，而不是"修好它"。
    """
    path = tmp_path / "master.key"
    damaged = b"someone overwrote this file"
    path.write_bytes(damaged)

    with pytest.raises(MasterKeyError):
        MasterKey.load_or_create(path)

    assert path.read_bytes() == damaged, "密钥文件被改写了——这正是最坏的那种'修复'"


def test_an_empty_key_file_is_treated_as_corrupted(tmp_path: Path) -> None:
    """空文件是"被截断的密钥"，不是"还没有密钥"。

    把它当后者会生成新密钥并覆盖，于是"文件被清空了"变成"资料全没了"。
    """
    path = tmp_path / "master.key"
    path.write_bytes(b"")

    with pytest.raises(MasterKeyError):
        MasterKey.load_or_create(path)

    assert path.read_bytes() == b""
