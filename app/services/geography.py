from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Any, Iterable

import yaml

from app.models import ClientProfile, Tender
from app.services.text_normalizer import normalize_greek_text

NUTS_CONFIG_PATH = os.path.join('config', 'regions_nuts.yml')


def _norm(value: object) -> str:
    text = normalize_greek_text(str(value or '')) or str(value or '')
    text = text.lower()
    text = re.sub(r'[-–—_/.,;:()\[\]{}"\']+', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _norm_code(value: object) -> str:
    text = str(value or '').upper().strip()
    text = re.sub(r'[^A-Z0-9]', '', text)
    return text


def _region_value(code: str, label: str) -> str:
    return f'{code} — {label}'


def _walk_regions(nodes: list[dict[str, Any]], parent_codes: list[str] | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for node in nodes or []:
        code = _norm_code(node.get('code'))
        label = str(node.get('label') or code)
        level = str(node.get('level') or '')
        parents = list(parent_codes or [])
        aliases = [str(a) for a in (node.get('aliases') or [])]
        item = {
            'code': code,
            'label': label,
            'level': level,
            'parents': parents,
            'aliases': aliases,
            'value': _region_value(code, label),
        }
        out.append(item)
        out.extend(_walk_regions(node.get('children') or [], parents + [code]))
    return out


@lru_cache(maxsize=1)
def nuts_regions() -> list[dict[str, Any]]:
    try:
        with open(NUTS_CONFIG_PATH, 'r', encoding='utf-8') as fh:
            data = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        data = {}
    return _walk_regions(data.get('regions') or [])


@lru_cache(maxsize=1)
def nuts_region_by_code() -> dict[str, dict[str, Any]]:
    return {item['code']: item for item in nuts_regions() if item.get('code')}


def nuts_options_grouped() -> dict[str, list[dict[str, Any]]]:
    labels = {
        'nuts1': 'NUTS 1 — Γεωγραφικές ομάδες',
        'nuts2': 'NUTS 2 — Περιφέρειες',
        'nuts3': 'NUTS 3 — Νομοί / Περιφερειακές Ενότητες',
    }
    groups: dict[str, list[dict[str, Any]]] = {label: [] for label in labels.values()}
    for item in nuts_regions():
        group = labels.get(item.get('level'), 'Άλλο')
        groups.setdefault(group, []).append(item)
    return groups


def region_display(value: str) -> str:
    code = extract_nuts_code(value)
    if code and code in nuts_region_by_code():
        item = nuts_region_by_code()[code]
        return _region_value(item['code'], item['label'])
    return str(value or '').strip()


def extract_nuts_code(value: object) -> str:
    text = str(value or '').upper()
    match = re.search(r'\bEL\d{1,3}\b', text)
    if match:
        return match.group(0)
    return ''


def _region_alias_terms(item: dict[str, Any]) -> set[str]:
    terms: set[str] = set()
    for value in [item.get('code'), item.get('label'), item.get('value'), *(item.get('aliases') or [])]:
        if value:
            terms.add(_norm(value))
            code = extract_nuts_code(value)
            if code:
                terms.add(_norm_code(code).lower())
    return {t for t in terms if t}


@lru_cache(maxsize=1)
def _code_descendants() -> dict[str, set[str]]:
    by_code = nuts_region_by_code()
    descendants: dict[str, set[str]] = {code: {code} for code in by_code}
    for code, item in by_code.items():
        for parent in item.get('parents') or []:
            descendants.setdefault(parent, {parent}).add(code)
    return descendants


def expand_region_terms(regions: Iterable[str] | str | None) -> list[str]:
    """Expand selected NUTS profile regions into searchable codes and labels.

    Input values are usually formatted as "EL63 — Δυτική Ελλάδα". We also accept
    legacy free-text values so old profiles continue to work.
    """
    if regions is None:
        return []
    if isinstance(regions, str):
        items = [p.strip() for p in re.split(r'[\n,;]+', regions) if p.strip()]
    else:
        items = [str(p).strip() for p in regions if str(p).strip()]

    by_code = nuts_region_by_code()
    descendants = _code_descendants()
    out: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        value = str(value or '').strip()
        if value and value not in seen:
            seen.add(value)
            out.append(value)

    for value in items:
        code = extract_nuts_code(value)
        if code and code in by_code:
            for related_code in sorted(descendants.get(code, {code})):
                item = by_code[related_code]
                add(item['code'])
                add(item['label'])
                add(item['value'])
                for alias in item.get('aliases') or []:
                    add(alias)
            # Do NOT add parent regions here. If a profile selects EL63 (Δυτική Ελλάδα),
            # matching EL6 (Κεντρική Ελλάδα) would also match sibling regions such as
            # EL61 (Θεσσαλία) or EL65 (Πελοπόννησος). We only accept the selected
            # region and its descendants to avoid false positives.
        else:
            # Legacy free-text support. Try to map by label/alias, otherwise keep as is.
            norm_value = _norm(value)
            matched_codes = [item['code'] for item in by_code.values() if norm_value in _region_alias_terms(item)]
            if matched_codes:
                for matched in matched_codes:
                    for term in expand_region_terms([matched]):
                        add(term)
            else:
                add(value)
    return out


def selected_region_labels(regions: Iterable[str] | None) -> list[str]:
    return [region_display(region) for region in (regions or [])]


def _collect_geo_values(value: Any, parent_key: str = '') -> list[str]:
    """Collect likely geographic values from KIMDIS raw payloads."""
    out: list[str] = []
    key_l = (parent_key or '').lower()
    geo_key = any(token in key_l for token in ('nuts', 'city', 'region', 'postal', 'country', 'municip', 'prefecture'))
    if isinstance(value, dict):
        for key, item in value.items():
            child_key = f'{parent_key}.{key}' if parent_key else str(key)
            out.extend(_collect_geo_values(item, child_key))
    elif isinstance(value, list):
        for item in value:
            out.extend(_collect_geo_values(item, parent_key))
    else:
        if geo_key and value not in (None, ''):
            out.append(str(value))
    return out


def tender_region_values(tender: Tender) -> list[str]:
    parts: list[str] = []
    if tender.organization_name:
        parts.append(tender.organization_name)
    raw = tender.raw or {}
    if isinstance(raw, dict):
        for key in ('nutsCity', 'nutsPostalCode'):
            if raw.get(key):
                parts.append(str(raw.get(key)))
        for key in ('nutsCode', 'nutsCountry'):
            item = raw.get(key)
            if isinstance(item, dict):
                parts.extend(str(item.get(k) or '') for k in ('key', 'value'))
        parts.extend(_collect_geo_values(raw))
    return [p for p in parts if p]


def tender_region_text(tender: Tender) -> str:
    return _norm(' '.join(tender_region_values(tender)))


def tender_nuts_codes(tender: Tender) -> set[str]:
    codes: set[str] = set()
    for value in tender_region_values(tender):
        code = extract_nuts_code(value)
        if code:
            codes.add(code)
    return codes


def preferred_region_match_details(tender: Tender, profile: ClientProfile) -> dict[str, list[str]]:
    """Return strong/weak profile-region matches.

    strong = direct structured NUTS match from KIMDIS values/codes.
    weak = text fallback from organization/raw fields. Weak matches are useful, but
    should be explained and scored lower to avoid false confidence.
    """
    region_blob = tender_region_text(tender)
    tender_codes = tender_nuts_codes(tender)
    strong: list[str] = []
    weak: list[str] = []
    by_code = nuts_region_by_code()
    descendants = _code_descendants()

    for region in profile.preferred_regions or []:
        display = region_display(region)
        code = extract_nuts_code(region)
        if code and code in by_code:
            allowed_codes = descendants.get(code, {code})
            if tender_codes.intersection(allowed_codes):
                if display not in strong:
                    strong.append(display)
                continue
            # Text fallback only after structured NUTS check failed.
            for term in expand_region_terms([region]):
                term_n = _norm(term)
                if term_n and term_n in region_blob:
                    if display not in weak:
                        weak.append(display)
                    break
        else:
            for term in expand_region_terms([region]):
                term_n = _norm(term)
                if term_n and term_n in region_blob:
                    if display not in weak:
                        weak.append(display)
                    break
    return {'strong': strong, 'weak': weak}


def preferred_region_matches(tender: Tender, profile: ClientProfile) -> list[str]:
    details = preferred_region_match_details(tender, profile)
    return [*details.get('strong', []), *details.get('weak', [])]


def any_region_match(tender: Tender, regions: Iterable[str]) -> bool:
    region_blob = tender_region_text(tender)
    tender_codes = tender_nuts_codes(tender)
    by_code = nuts_region_by_code()
    descendants = _code_descendants()
    for region in regions:
        code = extract_nuts_code(region)
        if code and code in by_code and tender_codes.intersection(descendants.get(code, {code})):
            return True
        for term in expand_region_terms([region]):
            term_n = _norm(term)
            if term_n and term_n in region_blob:
                return True
    return False
