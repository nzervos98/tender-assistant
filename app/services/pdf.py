from __future__ import annotations

import logging
from io import BytesIO
from typing import Optional

import httpx
from pypdf import PdfReader

from app.config import get_settings
from app.services.text_normalizer import normalize_greek_text

logger = logging.getLogger(__name__)


def download_pdf(url: str) -> bytes:
    settings = get_settings()
    with httpx.Client(timeout=settings.khmdhs_timeout_seconds) as client:
        with client.stream('GET', url, headers={'Accept': 'application/pdf'}) as response:
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


def fetch_and_extract_pdf_text(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    try:
        content = download_pdf(url)
        text = extract_text_from_pdf_bytes(content)
        text = normalize_greek_text(text) or text
        return text[:120_000] if text else None
    except Exception as exc:  # noqa: BLE001
        logger.warning('PDF download/extract failed for %s: %s', url, exc)
        return None
