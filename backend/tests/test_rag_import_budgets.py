"""Production byte imports must bound extraction, not merely ZIP member sizes."""
from io import BytesIO
import zipfile

import pytest

from backend.knowe_rag import parsers
from backend.tests.test_rag_office_parsers import _docx, _p, _tbl, _pptx, _slide, _xlsx, _row, _shared_cell, _inline_cell
from backend.tests.test_rag_pdf_parser import _build_pdf, _text_stream


def amplified_workbook(tmp_path):
    return _xlsx(tmp_path / "amplified.xlsx", {"Shared": "".join(
        _row(i, _shared_cell(f"A{i}", 0)) for i in range(1, 8201)
    )}, shared=["x" * 8192]).read_bytes()


def test_shared_string_expansion_stops_during_cell_extraction_before_band_joins(tmp_path, monkeypatch):
    raw = amplified_workbook(tmp_path)
    assert len(raw) < 50_000
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        members = archive.infolist()
        assert len(members) < 4096 and sum(m.file_size for m in members) < 64 * 1024 * 1024
        assert max(m.file_size for m in members) < 16 * 1024 * 1024
    calls = 0
    real_cell = parsers._xlsx_cell_value

    def cell(*args):
        nonlocal calls
        calls += 1
        return real_cell(*args)

    def band(*args):
        pytest.fail("Amplified rows reached the text joining stage")

    monkeypatch.setattr(parsers, "_xlsx_cell_value", cell)
    monkeypatch.setattr(parsers, "_xlsx_bands", band)
    with pytest.raises(parsers.UnsupportedFormat) as caught:
        parsers.parse_document_bytes(raw, ".xlsx")
    assert "extracted_text" in str(caught.value.__cause__ or caught.value)
    assert 1 < calls < 1000  # Would otherwise expand into >67 million characters.
    assert parsers._IMPORT_BUDGET.get() is None


def test_sparse_column_padding_is_charged_before_wide_row_join(tmp_path, monkeypatch):
    raw = _xlsx(tmp_path / "sparse.xlsx", {"Sparse": _row(1,
        _inline_cell("A1", "a") + _inline_cell("ZZZ1", "z"))}).read_bytes()
    monkeypatch.setattr(parsers, "MAX_PARSE_WORK_CHARS", 1000)
    real_join = parsers._join

    def join(parts, separator=""):
        assert separator != "\t", "Wide TSV allocation happened before padding budget check"
        return real_join(parts, separator)

    monkeypatch.setattr(parsers, "_join", join)
    with pytest.raises(parsers.UnsupportedFormat) as caught:
        parsers.parse_document_bytes(raw, ".xlsx")
    assert "extracted_text" in str(caught.value.__cause__ or caught.value)


@pytest.mark.parametrize("suffix", [".txt", ".md", ".html", ".docx", ".pptx", ".xlsx", ".pdf"])
def test_block_budget_is_cumulative_for_every_byte_parser(tmp_path, monkeypatch, suffix):
    if suffix == ".docx":
        raw = _docx(tmp_path / "blocks.docx", _p("a") + _p("b") + _p("c")).read_bytes()
    elif suffix == ".pptx":
        raw = _pptx(tmp_path / "blocks.pptx", {i: _slide(str(i)) for i in range(1, 4)}).read_bytes()
    elif suffix == ".xlsx":
        raw = _xlsx(tmp_path / "blocks.xlsx", {"Sheet": "".join(_row(i, _inline_cell(f"A{i}", str(i))) for i in (1, 3, 5))}).read_bytes()
    elif suffix == ".pdf":
        raw = _build_pdf([_text_stream([str(i)]) for i in range(3)])
    else:
        raw = {".txt": "a\n\nb\n\nc", ".md": "# a\n# b\n# c", ".html": "<p>a</p><p>b</p><p>c</p>"}[suffix].encode("utf-8")
    assert len(parsers.parse_document_bytes(raw, suffix)) == 3
    monkeypatch.setattr(parsers, "MAX_PARSE_BLOCKS", 2)
    with pytest.raises(parsers.UnsupportedFormat) as caught:
        parsers.parse_document_bytes(raw, suffix)
    assert "extracted_output" in str(caught.value.__cause__ or caught.value)
    assert parsers.parse_document_bytes(b"small valid import", ".txt")[0].text == "small valid import"


@pytest.mark.parametrize("suffix", [".docx", ".xlsx"])
def test_cell_budget_counts_actual_and_empty_cells(tmp_path, monkeypatch, suffix):
    if suffix == ".docx":
        raw = _docx(tmp_path / "cells.docx", _tbl([["a", "", "", "b"]])).read_bytes()
    else:
        raw = _xlsx(tmp_path / "cells.xlsx", {"Cells": _row(1, "".join(_inline_cell(f"{col}1", value)
            for col, value in zip("ABCD", ["a", "", "", "b"])))}).read_bytes()
    assert len(parsers.parse_document_bytes(raw, suffix)) == 1
    monkeypatch.setattr(parsers, "MAX_PARSE_CELLS", 3)
    with pytest.raises(parsers.UnsupportedFormat) as caught:
        parsers.parse_document_bytes(raw, suffix)
    assert "cell_count" in str(caught.value.__cause__ or caught.value)


def test_output_character_budget_rejects_before_final_block_is_constructed(monkeypatch):
    monkeypatch.setattr(parsers, "MAX_EXTRACTED_CHARS", 3)
    with pytest.raises(parsers.UnsupportedFormat, match="extracted_output"):
        parsers.parse_document_bytes(b"ab\n\ncd", ".txt")
