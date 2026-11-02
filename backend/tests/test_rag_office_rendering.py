"""Optional local Office rendering. Synthetic files only; no model calls."""
from io import BytesIO
import os
from pathlib import Path
import zipfile
import pytest
from backend.knowe_rag.office_rendering import _validate_package, convert_office_to_pdf, OfficeRenderingUnavailable, office_rendering_available
from backend.knowe_rag.rendering import render_page


def test_active_and_external_office_packages_fail_before_automation():
    for filename, content in [("word/vbaProject.bin", b"macro"), ("word/_rels/document.xml.rels", b'<Relationships><Relationship TargetMode="External" Target="https://example.invalid/private"/></Relationships>')]:
        data = BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr(filename, content)
        with pytest.raises(OfficeRenderingUnavailable):
            _validate_package(data.getvalue())


@pytest.mark.skipif(os.environ.get("STUDYCREW_OFFICE_RENDER_TEST") != "1", reason="Explicit local native Office fixture probe only")
@pytest.mark.parametrize("suffix", [".docx", ".pptx", ".xlsx"])
def test_real_native_office_fixture_to_pdf_page_image(suffix, tmp_path):
    if not office_rendering_available(suffix):
        pytest.skip("Native Office not installed")
    path = tmp_path / ("synthetic" + suffix)
    if suffix == ".docx":
        from docx import Document
        document = Document()
        document.add_paragraph("Synthetic chart evidence: A=12, B=27")
        document.add_table(rows=2, cols=2).cell(0, 0).text = "A:12"
        document.save(path)
    elif suffix == ".pptx":
        from pptx import Presentation
        from pptx.chart.data import CategoryChartData
        from pptx.enum.chart import XL_CHART_TYPE
        from pptx.util import Inches
        document = Presentation()
        slide = document.slides.add_slide(document.slide_layouts[6])
        data = CategoryChartData(); data.categories = ["A", "B"]; data.add_series("Synthetic minutes", [12, 27])
        slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(1), Inches(7), Inches(4), data)
        document.save(path)
    else:
        from openpyxl import Workbook
        from openpyxl.chart import BarChart, Reference
        document = Workbook(); sheet = document.active
        sheet.append(["Label", "Minutes"]); sheet.append(["A", 12]); sheet.append(["B", 27])
        chart = BarChart(); chart.add_data(Reference(sheet, min_col=2, min_row=1, max_row=3), titles_from_data=True)
        sheet.add_chart(chart, "D2"); document.save(path)
    pdf = convert_office_to_pdf(path.read_bytes(), suffix)
    page = render_page(pdf, 1)
    assert pdf.startswith(b"%PDF-") and page.image.startswith(b"\x89PNG\r\n\x1a\n")
    assert page.width > 0 and page.height > 0
