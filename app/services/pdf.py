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
        response = client.get(url, headers={'Accept': 'application/pdf'})
        response.raise_for_status()
        return response.content


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
