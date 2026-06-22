from __future__ import annotations

import re
from datetime import date


def normalize_date_input(value: str | None) -> str:
    """Normalize date fields typed in Greek UI forms.

    Accepts ISO dates (YYYY-MM-DD) and common Greek display dates
    (ηη/μμ/εεεε, ηη-μμ-εεεε, ηη.μμ.εεεε). Invalid values are
    preserved so the downstream API/report behaviour remains explicit.
    """
    text = (value or '').strip()
    if not text:
        return ''
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        pass
    m = re.fullmatch(r'(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})', text)
    if not m:
        return text
    day, month, year = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return text
