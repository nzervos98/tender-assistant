from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

import feedparser

from app.services.timezone import app_tz


def _parse_date(entry: Any) -> Optional[datetime]:
    parsed = getattr(entry, 'published_parsed', None) or getattr(entry, 'updated_parsed', None)
    if not parsed:
        return None
    return datetime(*parsed[:6], tzinfo=app_tz()).astimezone(timezone.utc)


def normalize_rss_entry(entry: Any, feed_url: str) -> Dict[str, Any]:
    title = getattr(entry, 'title', '') or '(χωρίς τίτλο)'
    link = getattr(entry, 'link', '')
    source_reference = getattr(entry, 'id', '') or getattr(entry, 'guid', '') or link
    if not source_reference:
        source_reference = hashlib.sha1(f'{title}|{feed_url}'.encode('utf-8')).hexdigest()
    summary = getattr(entry, 'summary', '') or getattr(entry, 'description', '') or ''
    return {
        'source': 'diavgeia_rss',
        'source_reference': source_reference,
        'reference_number': None,
        'title': title,
        'organization_key': None,
        'organization_name': feed_url,
        'submission_date': _parse_date(entry),
        'final_submission_date': None,
        'published_date': _parse_date(entry),
        'total_cost_without_vat': None,
        'total_cost_with_vat': None,
        'contract_type': None,
        'procedure_type': None,
        'cpv_codes': [],
        'cpv_descriptions': {},
        'url': link,
        'attachment_url': None,
        'pdf_text': summary,
        'raw': dict(entry),
        'cancelled': False,
    }


def fetch_rss_entries(feed_urls: Iterable[str]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for feed_url in feed_urls:
        if not feed_url:
            continue
        parsed = feedparser.parse(feed_url)
        for entry in parsed.entries:
            items.append(normalize_rss_entry(entry, feed_url))
    return items
