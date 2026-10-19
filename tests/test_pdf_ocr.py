from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from app.services import pdf


PUBLIC_DNS_RESULT = [
    (2, 1, 6, '', ('1.1.1.1', 443)),
]


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


def test_pdf_url_validation_allows_only_configured_https_origin(monkeypatch):
    monkeypatch.setattr(
        pdf,
        'get_settings',
        lambda: SimpleNamespace(khmdhs_base_url='https://cerpp.eprocurement.gov.gr'),
    )
    monkeypatch.setattr(pdf.socket, 'getaddrinfo', lambda *_args, **_kwargs: PUBLIC_DNS_RESULT)

    assert pdf._validate_pdf_url('https://cerpp.eprocurement.gov.gr/files/tender.pdf').startswith('https://')
    with pytest.raises(ValueError, match='HTTPS'):
        pdf._validate_pdf_url('http://cerpp.eprocurement.gov.gr/files/tender.pdf')
    with pytest.raises(ValueError, match='not allowed'):
        pdf._validate_pdf_url('https://example.test/tender.pdf')
    with pytest.raises(ValueError, match='standard HTTPS port'):
        pdf._validate_pdf_url('https://cerpp.eprocurement.gov.gr:8443/tender.pdf')


def test_pdf_url_validation_rejects_private_dns_destination(monkeypatch):
    monkeypatch.setattr(
        pdf,
        'get_settings',
        lambda: SimpleNamespace(khmdhs_base_url='https://cerpp.eprocurement.gov.gr'),
    )
    monkeypatch.setattr(
        pdf.socket,
        'getaddrinfo',
        lambda *_args, **_kwargs: [(2, 1, 6, '', ('127.0.0.1', 443))],
    )

    with pytest.raises(ValueError, match='non-public'):
        pdf._validate_pdf_url('https://cerpp.eprocurement.gov.gr/tender.pdf')
