from __future__ import annotations

import ipaddress
import logging
import socket
from io import BytesIO
from typing import Optional
from urllib.parse import urlsplit

import httpx
import pypdfium2 as pdfium
import pytesseract
from PIL import ImageOps
from pypdf import PdfReader

from app.config import get_settings
from app.services.text_normalizer import normalize_greek_text

logger = logging.getLogger(__name__)


class PdfTextExtractionError(RuntimeError):
    """User-safe failure raised by an explicit PDF analysis request."""


def _validate_pdf_url(url: str) -> str:
    """Allow HTTPS downloads only from the configured KIMDIS origin.

    The hostname allowlist is the primary SSRF boundary. DNS validation also
    prevents a compromised/misconfigured hostname from resolving to a local or
    otherwise non-public address.
    """
    parsed = urlsplit((url or '').strip())
    if parsed.scheme.lower() != 'https' or not parsed.hostname:
        raise ValueError('PDF URL must use HTTPS')
    if parsed.username or parsed.password:
        raise ValueError('PDF URL must not contain credentials')
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError('PDF URL contains an invalid port') from exc
    if port not in (None, 443):
        raise ValueError('PDF URL must use the standard HTTPS port')

    configured_origin = urlsplit(get_settings().khmdhs_base_url)
    allowed_host = (configured_origin.hostname or '').lower().rstrip('.')
    requested_host = parsed.hostname.lower().rstrip('.')
    if not allowed_host or requested_host != allowed_host:
        raise ValueError('PDF URL host is not allowed')

    try:
        addresses = socket.getaddrinfo(requested_host, port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError('PDF URL host could not be resolved') from exc
    if not addresses:
        raise ValueError('PDF URL host did not resolve to an address')
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise ValueError('PDF URL resolves to a non-public address')
    return parsed.geturl()


def download_pdf(url: str) -> bytes:
    settings = get_settings()
    validated_url = _validate_pdf_url(url)
    with httpx.Client(timeout=settings.khmdhs_timeout_seconds, follow_redirects=False) as client:
        with client.stream('GET', validated_url, headers={'Accept': 'application/pdf'}) as response:
            if response.is_redirect:
                raise ValueError('PDF redirects are not allowed')
            response.raise_for_status()
            content_type = (response.headers.get('content-type') or '').lower()
            if content_type and 'pdf' not in content_type and 'octet-stream' not in content_type:
                raise ValueError(f'Unexpected PDF content type: {content_type}')
            content_length = response.headers.get('content-length')
            if content_length and int(content_length) > settings.pdf_max_bytes:
                raise ValueError('PDF exceeds configured maximum size')
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > settings.pdf_max_bytes:
                    raise ValueError('PDF exceeds configured maximum size')
                chunks.append(chunk)
            content = b''.join(chunks)
            if content and not content.lstrip().startswith(b'%PDF'):
                raise ValueError('Downloaded attachment is not a PDF')
            return content


def extract_text_from_pdf_bytes(content: bytes, max_pages: int = 30) -> str:
    reader = PdfReader(BytesIO(content))
    parts: list[str] = []
    for i, page in enumerate(reader.pages[:max_pages]):
        try:
            text = page.extract_text() or ''
        except Exception as exc:  # noqa: BLE001
            logger.warning('Could not extract page %s from PDF: %s', i, exc)
            text = ''
        if text.strip():
            parts.append(text.strip())
    return '\n\n'.join(parts)


def extract_text_from_scanned_pdf_bytes(
    content: bytes,
    *,
    max_pages: int,
    dpi: int,
    languages: str,
    page_timeout_seconds: int,
) -> str:
    """Render scanned PDF pages and run bounded Greek/English OCR."""
    document = pdfium.PdfDocument(content)
    parts: list[str] = []
    try:
        page_count = min(len(document), max(1, int(max_pages)))
        scale = max(1.0, min(float(dpi), 300.0)) / 72.0
        for index in range(page_count):
            page = document[index]
            bitmap = None
            image = None
            grayscale = None
            processed = None
            try:
                bitmap = page.render(scale=scale)
                image = bitmap.to_pil()
                grayscale = ImageOps.grayscale(image)
                processed = ImageOps.autocontrast(grayscale)
                text = pytesseract.image_to_string(
                    processed,
                    lang=languages or 'ell+eng',
                    config='--oem 3 --psm 3',
                    timeout=max(1, int(page_timeout_seconds)),
                )
                if text.strip():
                    parts.append(text.strip())
            except RuntimeError as exc:
                raise PdfTextExtractionError(
                    f'Το OCR ξεπέρασε το χρονικό όριο στη σελίδα {index + 1}. '
                    'Δοκιμάστε μικρότερο αρχείο ή ανοίξτε το επίσημο PDF.'
                ) from exc
            finally:
                if processed is not None:
                    processed.close()
                if grayscale is not None:
                    grayscale.close()
                if image is not None:
                    image.close()
                if bitmap is not None:
                    bitmap.close()
                page.close()
    finally:
        document.close()
    return '\n\n'.join(parts)


def fetch_and_extract_pdf_text(url: Optional[str], *, strict: bool = False) -> Optional[str]:
    if not url:
        return None
    try:
        settings = get_settings()
        content = download_pdf(url)
        try:
            text = extract_text_from_pdf_bytes(content)
        except Exception as exc:  # noqa: BLE001
            logger.warning('Embedded PDF text extraction failed; trying OCR for %s: %s', url, exc)
            text = ''
        if len(text.strip()) < 80 and settings.pdf_ocr_enabled:
            logger.info('PDF has no usable text layer; starting OCR fallback for %s', url)
            ocr_text = extract_text_from_scanned_pdf_bytes(
                content,
                max_pages=settings.pdf_ocr_max_pages,
                dpi=settings.pdf_ocr_dpi,
                languages=settings.pdf_ocr_languages,
                page_timeout_seconds=settings.pdf_ocr_page_timeout_seconds,
            )
            if len(ocr_text.strip()) > len(text.strip()):
                text = ocr_text
        text = normalize_greek_text(text) or text
        if text and text.strip():
            return text[:120_000]
        message = (
            'Το PDF δεν περιέχει αναγνώσιμο κείμενο και το OCR δεν μπόρεσε '
            'να αναγνωρίσει το σαρωμένο περιεχόμενο.'
        )
        if strict:
            raise PdfTextExtractionError(message)
        logger.warning('%s URL: %s', message, url)
        return None
    except PdfTextExtractionError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning('PDF download/extract failed for %s: %s', url, exc)
        if strict:
            raise PdfTextExtractionError(
                'Δεν ήταν δυνατή η λήψη ή η ανάγνωση του PDF. Δοκιμάστε ξανά ή ανοίξτε το επίσημο αρχείο.'
            ) from exc
        return None
