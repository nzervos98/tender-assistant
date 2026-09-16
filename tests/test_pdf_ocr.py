from io import BytesIO

import pytest
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from app.services import pdf


def _scanned_text_pdf(text: str) -> bytes:
    image = Image.new('RGB', (1600, 500), 'white')
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 64)
    draw.text((70, 180), text, fill='black', font=font)
    image_buffer = BytesIO()
    image.save(image_buffer, format='PNG')
    image_buffer.seek(0)

    pdf_buffer = BytesIO()
    document = canvas.Canvas(pdf_buffer, pagesize=(800, 250))
    document.drawImage(ImageReader(image_buffer), 0, 0, width=800, height=250)
    document.showPage()
    document.save()
    image.close()
    return pdf_buffer.getvalue()


def test_scanned_pdf_uses_real_ocr_when_no_text_layer():
    content = _scanned_text_pdf('TENDER REQUIREMENT ISO 27001')

    assert pdf.extract_text_from_pdf_bytes(content) == ''
    extracted = pdf.extract_text_from_scanned_pdf_bytes(
        content,
        max_pages=1,
        dpi=200,
        languages='eng',
        page_timeout_seconds=20,
    )

    assert 'TENDER' in extracted.upper()
    assert '27001' in extracted


def test_explicit_pdf_analysis_returns_friendly_error_when_ocr_finds_nothing(monkeypatch):
    monkeypatch.setattr(pdf, 'download_pdf', lambda _url: b'%PDF fake')
    monkeypatch.setattr(pdf, 'extract_text_from_pdf_bytes', lambda _content: '')
    monkeypatch.setattr(pdf, 'extract_text_from_scanned_pdf_bytes', lambda _content, **_kwargs: '')

    with pytest.raises(pdf.PdfTextExtractionError, match='OCR'):
        pdf.fetch_and_extract_pdf_text('https://example.test/scanned.pdf', strict=True)
