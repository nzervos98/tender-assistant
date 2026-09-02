from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Any, Iterable

import yaml
from sqlalchemy import String, cast

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


def _coded_value_parts(value: Any) -> list[str]:
    """Flatten a KIMDIS key/value code without traversing unrelated fields."""
    if isinstance(value, dict):
        return [str(value.get(key)) for key in ('key', 'value') if value.get(key) not in (None, '')]
    if value in (None, ''):
        return []
    return [str(value)]


def tender_execution_region_values(tender: Tender) -> list[str]:
    """NUTS values for the contract performance location (`nutsCodes`)."""
    raw = tender.raw or {}
    if not isinstance(raw, dict):
        return []
    values: list[str] = []
    nuts_codes = raw.get('nutsCodes') or []
    if not isinstance(nuts_codes, list):
        nuts_codes = [nuts_codes]
    for entry in nuts_codes:
        if isinstance(entry, dict) and 'nutsCode' in entry:
            values.extend(_coded_value_parts(entry.get('nutsCode')))
        else:
            values.extend(_coded_value_parts(entry))
    return values


def tender_authority_region_values(tender: Tender) -> list[str]:
    """NUTS/address values for the contracting authority (`nutsCode`)."""
    raw = tender.raw or {}
    if not isinstance(raw, dict):
        return []
    values = _coded_value_parts(raw.get('nutsCode'))
    for key in ('nutsCity', 'nutsPostalCode'):
        if raw.get(key) not in (None, ''):
            values.append(str(raw[key]))
    values.extend(_coded_value_parts(raw.get('nutsCountry')))
    return values


def tender_region_values(tender: Tender) -> list[str]:
    """Backward-compatible alias: business region means place of performance."""
    return tender_execution_region_values(tender)


def tender_region_text(tender: Tender) -> str:
    return _norm(' '.join(tender_region_values(tender)))


def tender_authority_region_text(tender: Tender) -> str:
    return _norm(' '.join(tender_authority_region_values(tender)))


def tender_effective_region_text(tender: Tender) -> str:
    """Execution location, or authority location only when execution is absent."""
    return tender_region_text(tender) or tender_authority_region_text(tender)


def tender_nuts_codes(tender: Tender) -> set[str]:
    codes: set[str] = set()
    for value in tender_region_values(tender):
        code = extract_nuts_code(value)
        if code:
            codes.add(code)
    return codes


def tender_authority_nuts_codes(tender: Tender) -> set[str]:
    codes: set[str] = set()
    for value in tender_authority_region_values(tender):
        code = extract_nuts_code(value)
        if code:
            codes.add(code)
    return codes


def region_filter_expressions(region: str, *, authority: bool = False) -> list[Any]:
    """Build focused JSON filters for one of the two official KIMDIS NUTS meanings."""
    raw_path = Tender.raw['nutsCode'] if authority else Tender.raw['nutsCodes']
    target = cast(raw_path, String)
    return [target.ilike(f'%{term}%') for term in expand_region_terms(region) if term]


def preferred_region_match_details(tender: Tender, profile: ClientProfile) -> dict[str, list[str]]:
    """Return strong/weak profile-region matches.

    strong = direct structured NUTS match from KIMDIS values/codes.
    weak = text fallback from organization/raw fields. Weak matches are useful, but
    should be explained and scored lower to avoid false confidence.
    """
    execution_values = tender_execution_region_values(tender)
    region_blob = _norm(' '.join(execution_values))
    tender_codes = tender_nuts_codes(tender)
    # Authority location is only a fallback when KIMDIS provides no place of
    # performance. It must never override a declared `nutsCodes` location.
    fallback_to_authority = not execution_values
    if fallback_to_authority:
        region_blob = _norm(' '.join(tender_authority_region_values(tender)))
        tender_codes = tender_authority_nuts_codes(tender)
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
                matches = weak if fallback_to_authority else strong
                if display not in matches:
                    matches.append(display)
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
    execution_values = tender_execution_region_values(tender)
    region_blob = _norm(' '.join(execution_values))
    tender_codes = tender_nuts_codes(tender)
    if not execution_values:
        region_blob = _norm(' '.join(tender_authority_region_values(tender)))
        tender_codes = tender_authority_nuts_codes(tender)
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
