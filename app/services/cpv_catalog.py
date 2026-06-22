from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class CPVEntry:
    code: str
    title: str
    category: str
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class CPVTreeRow:
    id: str
    parent_id: str | None
    code: str
    title: str
    level: int
    has_children: bool


@dataclass(frozen=True)
class CPVRecord:
    code: str
    title: str
    parent_code: str | None
    level: int
    root_code: str
    root_title: str
    sort_order: int


# Πλήρης τοπικός CPV κατάλογος από το nested JSON tree του ΚΗΜΔΗΣ/ΕΣΗΔΗΣ.
# Περιέχει code, ελληνική περιγραφή, parent_code, level και σειρά εμφάνισης.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
FULL_CPV_CATALOG_PATH = PROJECT_ROOT / 'config' / 'cpv_catalog_full.json'


# Ελάχιστο fallback μόνο για περίπτωση που λείπει το JSON αρχείο σε custom install.
# Στο κανονικό Docker build χρησιμοποιείται το config/cpv_catalog_full.json.
_FALLBACK_RECORDS: tuple[dict[str, object], ...] = (
    {'code': '09000000-3', 'title': 'Πετρελαϊκά προϊόντα, καύσιμα, ηλεκτρική και άλλες πηγές ενέργειας', 'parent_code': None, 'level': 0, 'root_code': '09000000-3', 'root_title': 'Πετρελαϊκά προϊόντα, καύσιμα, ηλεκτρική και άλλες πηγές ενέργειας', 'sort_order': 0},
    {'code': '09135100-5', 'title': 'Πετρέλαιο θέρμανσης', 'parent_code': '09000000-3', 'level': 1, 'root_code': '09000000-3', 'root_title': 'Πετρελαϊκά προϊόντα, καύσιμα, ηλεκτρική και άλλες πηγές ενέργειας', 'sort_order': 1},
    {'code': '16000000-5', 'title': 'Γεωργικά μηχανήματα', 'parent_code': None, 'level': 0, 'root_code': '16000000-5', 'root_title': 'Γεωργικά μηχανήματα', 'sort_order': 2},
    {'code': '33000000-0', 'title': 'Ιατρικές συσκευές, φαρμακευτικά προϊόντα και προϊόντα ατομικής περιποίησης', 'parent_code': None, 'level': 0, 'root_code': '33000000-0', 'root_title': 'Ιατρικές συσκευές, φαρμακευτικά προϊόντα και προϊόντα ατομικής περιποίησης', 'sort_order': 3},
    {'code': '33100000-1', 'title': 'Ιατρικές συσκευές', 'parent_code': '33000000-0', 'level': 1, 'root_code': '33000000-0', 'root_title': 'Ιατρικές συσκευές, φαρμακευτικά προϊόντα και προϊόντα ατομικής περιποίησης', 'sort_order': 4},
    {'code': '33600000-6', 'title': 'Φαρμακευτικά προϊόντα', 'parent_code': '33000000-0', 'level': 1, 'root_code': '33000000-0', 'root_title': 'Ιατρικές συσκευές, φαρμακευτικά προϊόντα και προϊόντα ατομικής περιποίησης', 'sort_order': 5},
    {'code': '33700000-7', 'title': 'Προϊόντα ατομικής περιποίησης', 'parent_code': '33000000-0', 'level': 1, 'root_code': '33000000-0', 'root_title': 'Ιατρικές συσκευές, φαρμακευτικά προϊόντα και προϊόντα ατομικής περιποίησης', 'sort_order': 6},
    {'code': '33900000-9', 'title': 'Εξοπλισμός και προμήθειες νεκροψίας και νεκροτομείου', 'parent_code': '33000000-0', 'level': 1, 'root_code': '33000000-0', 'root_title': 'Ιατρικές συσκευές, φαρμακευτικά προϊόντα και προϊόντα ατομικής περιποίησης', 'sort_order': 7},
    {'code': '79340000-9', 'title': 'Υπηρεσίες διαφήμισης και μάρκετινγκ', 'parent_code': None, 'level': 0, 'root_code': '79340000-9', 'root_title': 'Υπηρεσίες διαφήμισης και μάρκετινγκ', 'sort_order': 8},
)

SUGGESTED_CATEGORIES: dict[str, tuple[str, ...]] = {
    'Media / Διαφήμιση': ('79340000-9', '79341000-6', '79341200-8', '79341400-0', '79342000-3', '79342100-4', '79342200-5', '79822500-7', '92111200-4', '92200000-3', '79952000-2'),
    'Εκτυπώσεις / Έντυπα': ('79800000-2', '79810000-5', '79820000-8', '79822500-7'),
    'Πληροφορική / Web': ('72000000-5', '72200000-7', '72212220-7', '72413000-8', '48000000-8', '72300000-8', '72320000-4', '72500000-0'),
    'Καθαρισμός': ('90910000-9', '90911200-8', '90911300-9', '90920000-2'),
    'Φύλαξη / Ασφάλεια': ('79710000-4', '79713000-5', '79714000-2'),
    'Τεχνικά έργα / Μελέτες': ('45000000-7', '45200000-9', '45233120-6', '71300000-1', '71320000-7', '71200000-0'),
    'Τρόφιμα / Υγεία': ('15000000-8', '15800000-6', '33600000-6', '33100000-1'),
    'Συμβουλευτικές / Εκπαίδευση': ('79400000-8', '79410000-1', '80500000-9', '80522000-9'),
}


def normalize(value: str | None) -> str:
    value = (value or '').lower()
    value = ''.join(ch for ch in unicodedata.normalize('NFD', value) if unicodedata.category(ch) != 'Mn')
    value = re.sub(r'[-–—_/.,;:()\[\]]+', ' ', value)
    value = re.sub(r'\s+', ' ', value).strip()
    return value


def _record_from_dict(item: dict[str, object]) -> CPVRecord:
    code = str(item.get('code') or '').strip()
    title = str(item.get('title') or '').strip()
    parent = item.get('parent_code')
    parent_code = str(parent).strip() if parent else None
    root_code = str(item.get('root_code') or code).strip()
    root_title = str(item.get('root_title') or title).strip()
    try:
        level = int(item.get('level') or 0)
    except (TypeError, ValueError):
        level = 0
    try:
        sort_order = int(item.get('sort_order') or 0)
    except (TypeError, ValueError):
        sort_order = 0
    return CPVRecord(code=code, title=title, parent_code=parent_code, level=level, root_code=root_code, root_title=root_title, sort_order=sort_order)


@lru_cache(maxsize=1)
def _records() -> tuple[CPVRecord, ...]:
    if FULL_CPV_CATALOG_PATH.exists():
        raw = json.loads(FULL_CPV_CATALOG_PATH.read_text(encoding='utf-8'))
    else:
        raw = list(_FALLBACK_RECORDS)
    records: list[CPVRecord] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        rec = _record_from_dict(item)
        if not rec.code or rec.code in seen:
            continue
        seen.add(rec.code)
        records.append(rec)
    records.sort(key=lambda rec: (rec.sort_order, _cpv_sort_key(rec.code)))
    return tuple(records)


@lru_cache(maxsize=1)
def _record_by_code() -> dict[str, CPVRecord]:
    return {rec.code: rec for rec in _records()}


@lru_cache(maxsize=1)
def _children_by_parent() -> dict[str | None, tuple[CPVRecord, ...]]:
    children: dict[str | None, list[CPVRecord]] = {}
    for rec in _records():
        children.setdefault(rec.parent_code, []).append(rec)
    return {parent: tuple(sorted(items, key=lambda rec: (rec.sort_order, _cpv_sort_key(rec.code)))) for parent, items in children.items()}


def _entry_from_record(rec: CPVRecord) -> CPVEntry:
    category = f'{rec.root_code} — {rec.root_title}' if rec.root_code and rec.root_title else 'Πλήρης κατάλογος CPV'
    tags: tuple[str, ...] = tuple({rec.root_title, rec.title} - {''})
    return CPVEntry(code=rec.code, title=rec.title, category=category, tags=tags)


def _combined_entries() -> list[CPVEntry]:
    return [_entry_from_record(rec) for rec in _records()]


def _entry_blob(entry: CPVEntry) -> str:
    return normalize(' '.join([entry.code, entry.title, entry.category, ' '.join(entry.tags)]))


def cpv_search(query: str = '', limit: int = 50, category: str = '') -> list[CPVEntry]:
    q = normalize(query)
    tokens = [token for token in q.split() if token]
    entries: Iterable[CPVEntry] = _combined_entries()
    if category:
        entries = [entry for entry in entries if entry.category == category or entry.category.startswith(category)]
    scored: list[tuple[int, int, CPVEntry]] = []
    order_by_code = {rec.code: rec.sort_order for rec in _records()}
    for entry in entries:
        blob = _entry_blob(entry)
        score = 0
        if q and q in blob:
            score += 10
        if tokens:
            score += sum(3 for token in tokens if token in blob)
        if q and entry.code.startswith(q):
            score += 20
        if not q:
            score = 1
        if score > 0:
            scored.append((score, order_by_code.get(entry.code, 999999), entry))
    scored.sort(key=lambda item: (-item[0], item[1], item[2].code))
    return [entry for _, _, entry in scored[:limit]]


def cpv_by_codes(codes: Iterable[str]) -> list[CPVEntry]:
    wanted = [code.strip() for code in codes if code and code.strip()]
    by_code = {entry.code: entry for entry in _combined_entries()}
    return [by_code[code] for code in wanted if code in by_code]


def cpv_categories() -> list[str]:
    return sorted({entry.category for entry in _combined_entries()})


def cpv_category_suggestions() -> dict[str, list[CPVEntry]]:
    by_code = {entry.code: entry for entry in _combined_entries()}
    return {category: [by_code[code] for code in codes if code in by_code] for category, codes in SUGGESTED_CATEGORIES.items()}


def _cpv_base(code: str) -> str:
    return (code or '').split('-')[0].strip()


def _cpv_sort_key(code: str) -> tuple[int, str]:
    base = _cpv_base(code)
    return (int(base) if base.isdigit() else 99999999, code)


def cpv_catalog_size() -> int:
    return len(_records())


def cpv_tree_rows() -> list[CPVTreeRow]:
    children = _children_by_parent()
    rows: list[CPVTreeRow] = []
    for rec in _records():
        rows.append(CPVTreeRow(
            id=rec.code,
            parent_id=rec.parent_code,
            code=rec.code,
            title=rec.title,
            level=rec.level,
            has_children=bool(children.get(rec.code)),
        ))
    return rows


def cpv_tree_children(parent_code: str | None = None, limit: int = 500) -> list[CPVTreeRow]:
    """Return direct CPV children for lazy UI loading.

    parent_code=None returns root rows only. This avoids rendering the full ~9.5k
    CPV catalog on profile pages.
    """
    parent = (parent_code or '').strip() or None
    children = _children_by_parent()
    rows: list[CPVTreeRow] = []
    for rec in children.get(parent, ())[:max(1, limit)]:
        rows.append(CPVTreeRow(
            id=rec.code,
            parent_id=rec.parent_code,
            code=rec.code,
            title=rec.title,
            level=rec.level,
            has_children=bool(children.get(rec.code)),
        ))
    return rows


def cpv_record(code: str) -> CPVRecord | None:
    return _record_by_code().get((code or '').strip())


CPV_CODE_RE = re.compile(r'^\d{8}-\d$')


def is_valid_cpv_code(code: str) -> bool:
    return bool(CPV_CODE_RE.match((code or '').strip()))


def cpv_prefix_for(code: str, digits: int | None = None) -> str:
    base = (code or '').split('-')[0].strip()
    if not (base.isdigit() and len(base) == 8):
        return ''
    if digits is not None:
        digits = max(2, min(6, digits))
        return base[:digits]
    trimmed = base.rstrip('0')
    if len(trimmed) < 2:
        return base[:2]
    if len(trimmed) > 6:
        return trimmed[:6]
    return trimmed


def cpv_prefixes_for_codes(codes: Iterable[str]) -> list[str]:
    prefixes: list[str] = []
    seen: set[str] = set()
    for code in codes or []:
        prefix = cpv_prefix_for(code)
        if prefix and prefix not in seen:
            seen.add(prefix)
            prefixes.append(prefix)
    return sorted(prefixes)


def cpv_ancestor_codes(code: str) -> list[str]:
    """Return parent, grandparent, ... for a CPV code using the full tree."""
    rec = _record_by_code().get((code or '').strip())
    ancestors: list[str] = []
    seen: set[str] = set()
    while rec and rec.parent_code and rec.parent_code not in seen:
        ancestors.append(rec.parent_code)
        seen.add(rec.parent_code)
        rec = _record_by_code().get(rec.parent_code)
    return ancestors


def cpv_selected_ancestor(code: str, selected_codes: Iterable[str]) -> str:
    selected = {str(item).strip() for item in selected_codes or [] if str(item).strip()}
    for ancestor in cpv_ancestor_codes(code):
        if ancestor in selected:
            return ancestor
    return ''


def cpv_descendant_codes(code: str) -> list[str]:
    """Return all known descendant CPVs for a selected CPV code.

    This now uses the full local CPV tree from config/cpv_catalog_full.json rather
    than a small hand-written helper list. It is used by KIMDIS ingest/search so a
    parent CPV can discover notices published under more specific child CPVs.
    """
    code = (code or '').strip()
    if not is_valid_cpv_code(code):
        return []
    children = _children_by_parent()
    descendants: list[str] = []

    def walk(parent: str) -> None:
        for child in children.get(parent, ()):  # type: ignore[arg-type]
            descendants.append(child.code)
            walk(child.code)

    walk(code)
    return descendants


def cpv_covered_by_selected_parent_codes(selected_codes: Iterable[str]) -> set[str]:
    """Codes covered by selected parent CPVs, excluding exact selected codes."""
    selected = {str(code).strip() for code in selected_codes or [] if str(code).strip()}
    covered: set[str] = set()
    for code in selected:
        covered.update(cpv_descendant_codes(code))
    return covered - selected


def expand_cpv_codes_for_ingest(codes: Iterable[str], include_descendants: bool = True) -> list[str]:
    """Return exact selected CPVs plus full known descendants for KIMDIS ingest.

    Exact selected CPVs are kept first. Descendants are appended after each
    selected CPV. Scoring still treats child-code notices as child/family matches
    unless the child CPV was explicitly selected in the profile.
    """
    result: list[str] = []
    seen: set[str] = set()

    def add(code: str) -> None:
        code = (code or '').strip()
        if code and code not in seen:
            seen.add(code)
            result.append(code)

    for code in codes or []:
        code = (code or '').strip()
        if not code:
            continue
        add(code)
        if include_descendants:
            for child in cpv_descendant_codes(code):
                add(child)
    return result


def cpv_family_record(code: str, target_level: int = 2) -> CPVRecord | None:
    """Return a practical CPV family for reporting.

    The full CPV tree can be very deep. For client reports we group detailed
    codes under a readable mid-level family, normally level 2. If the code is
    already broader than that, return the code itself.
    """
    rec = cpv_record(code)
    if rec is None:
        return None
    target_level = max(0, target_level)
    if rec.level <= target_level:
        return rec
    current = rec
    by_code = _record_by_code()
    while current.parent_code:
        parent = by_code.get(current.parent_code)
        if parent is None:
            break
        if parent.level <= target_level:
            return parent
        current = parent
    return current


def cpv_family_label(code: str, target_level: int = 2) -> str:
    rec = cpv_family_record(code, target_level=target_level)
    if rec is None:
        return (code or '').strip() or 'Χωρίς CPV'
    return f'{rec.code} — {rec.title}'
