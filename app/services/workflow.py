from __future__ import annotations

from collections import OrderedDict

# User-facing workflow is intentionally small.
# Older statuses remain supported as aliases so existing databases do not lose bookmarks.
WORKFLOW_STATUSES: "OrderedDict[str, str]" = OrderedDict([
    ('new', 'Χωρίς ενέργεια'),
    ('saved', 'Αποθηκευμένο'),
    ('reviewing', 'Σε έλεγχο'),
    ('not_relevant', 'Δεν αφορά'),
])

LEGACY_STATUS_ALIASES = {
    'interested': 'saved',
    'will_bid': 'saved',
    'not_interested': 'not_relevant',
    'rejected': 'not_relevant',
}

# Reverse mapping for query filters. "new" means no manual action yet; the import badge is separate.
STATUS_FILTER_ALIASES = {
    'saved': ['saved', 'interested', 'will_bid'],
    'not_relevant': ['not_relevant', 'not_interested', 'rejected'],
    'reviewing': ['reviewing'],
    'new': ['new'],
}


def normalize_workflow_status(value: str | None) -> str:
    raw = (value or 'new').strip()
    if raw in WORKFLOW_STATUSES:
        return raw
    return LEGACY_STATUS_ALIASES.get(raw, 'new')


def workflow_status_label(value: str | None) -> str:
    key = normalize_workflow_status(value)
    return WORKFLOW_STATUSES.get(key, WORKFLOW_STATUSES['new'])


def workflow_status_class(value: str | None) -> str:
    return normalize_workflow_status(value)


def workflow_status_filter_values(value: str | None) -> list[str]:
    key = normalize_workflow_status(value)
    return STATUS_FILTER_ALIASES.get(key, [key])
