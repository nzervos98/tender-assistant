from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any, Mapping, Optional

import httpx

from app.config import get_settings
from app.services.timezone import today_local


class DiavgeiaClientError(RuntimeError):
    """Raised when the Diavgeia OpenData API cannot be reached or parsed."""


@dataclass(frozen=True)
class DiavgeiaDecisionSummary:
    ada: str
    subject: str
    organization: str = ''
    organization_uid: str = ''
    decision_type: str = ''
    decision_type_uid: str = ''
    issue_date: str = ''
    submission_timestamp: str = ''
    status: str = ''
    url: str = ''
    api_url: str = ''
    raw: Mapping[str, Any] | None = None


def _first_text(value: Any, *keys: str) -> str:
    """Return the first non-empty textual value from nested dict-ish objects.

    Diavgeia responses have changed shape across versions and content negotiation
    can also alter naming. Keeping this tolerant lets the first integration step
    inspect real responses without coupling to one exact payload variant.
    """
    if value is None:
        return ''
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, Mapping):
        if keys:
            for key in keys:
                if key in value:
                    found = _first_text(value.get(key))
                    if found:
                        return found
        for key in ('label', 'name', 'uid', 'id', 'latinName'):
            if key in value:
                found = _first_text(value.get(key))
                if found:
                    return found
    return ''


def _dig(mapping: Mapping[str, Any], *paths: str) -> str:
    for path in paths:
        cur: Any = mapping
        ok = True
        for part in path.split('.'):
            if isinstance(cur, Mapping) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok:
            found = _first_text(cur)
            if found:
                return found
    return ''


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def extract_decisions(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Extract decisions from known Diavgeia search response shapes."""
    candidates: list[Any] = [
        payload.get('decisions'),
        payload.get('decision'),
        payload.get('results'),
    ]
    nested = payload.get('decisionSearchResult')
    if isinstance(nested, Mapping):
        candidates.extend([nested.get('decisions'), nested.get('decision'), nested.get('results')])
    response = payload.get('response')
    if isinstance(response, Mapping):
        candidates.extend([response.get('docs'), response.get('decisions'), response.get('results')])

    for candidate in candidates:
        items = _as_list(candidate)
        mapped = [item for item in items if isinstance(item, Mapping)]
        if mapped:
            return mapped
    return []


def extract_total(payload: Mapping[str, Any], fallback: int = 0) -> int:
    values = [
        payload.get('total'),
        _dig(payload, 'info.total'),
        _dig(payload, 'decisionSearchResult.info.total'),
        _dig(payload, 'response.numFound'),
    ]
    for value in values:
        try:
            if value not in (None, ''):
                return int(value)
        except (TypeError, ValueError):
            continue
    return fallback



def _unwrap_decision(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the most likely decision object from wrapper payloads."""
    for key in ('decision', 'decisionData'):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            return nested
    return value


def _normalize_diavgeia_datetime(value: str, *, date_only: bool = False) -> str:
    """Normalize common Diavgeia date formats to readable local strings.

    The current public search API often returns Unix timestamps in milliseconds
    as strings. Older/export formats can use dd/MM/yyyy HH:mm:ss. Keeping this
    local and tolerant makes the probe output much easier to inspect.
    """
    text = (value or '').strip()
    if not text:
        return ''

    digits = text.replace('.', '', 1)
    if digits.isdigit():
        try:
            number = float(text)
            if number > 10_000_000_000:  # epoch milliseconds
                number = number / 1000.0
            if number > 1_000_000_000:  # epoch seconds
                dt = datetime.fromtimestamp(number, tz=timezone.utc).astimezone(ZoneInfo('Europe/Athens'))
                return dt.date().isoformat() if date_only else dt.replace(microsecond=0).isoformat()
        except (OverflowError, ValueError, OSError):
            pass

    for fmt in ('%d/%m/%Y %H:%M:%S', '%d/%m/%Y %H:%M', '%d/%m/%Y'):
        try:
            dt = datetime.strptime(text, fmt).replace(tzinfo=ZoneInfo('Europe/Athens'))
            return dt.date().isoformat() if date_only else dt.replace(microsecond=0).isoformat()
        except ValueError:
            continue

    return text


def _merge_prefer_detail(search_item: Mapping[str, Any], detail_item: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Merge search and detail payloads, preferring non-empty detail values."""
    if not detail_item:
        return search_item
    detail = dict(_unwrap_decision(detail_item))
    merged = dict(search_item)
    for key, value in detail.items():
        if value not in (None, '', [], {}):
            merged[key] = value
    return merged


def normalize_decision(decision: Mapping[str, Any]) -> DiavgeiaDecisionSummary:
    decision = _unwrap_decision(decision)
    ada = _dig(decision, 'ada', 'ADA', 'iun', 'id')
    organization_value = decision.get('organization') or decision.get('organizationLabel') or decision.get('org') or {}
    decision_type_value = decision.get('decisionType') or decision.get('type') or {}
    source_url = _dig(decision, 'url', 'documentUrl', 'document.url', 'decisionDocumentUrl')
    api_url = source_url if '/luminapi/' in source_url else (f'https://diavgeia.gov.gr/luminapi/api/decisions/{ada}' if ada else '')
    view_url = f'https://diavgeia.gov.gr/decision/view/{ada}' if ada else source_url
    issue_date_raw = _dig(decision, 'issueDate', 'issue_date')
    submission_raw = _dig(decision, 'submissionTimestamp', 'submission_timestamp', 'submissionDate')
    return DiavgeiaDecisionSummary(
        ada=ada,
        subject=_dig(decision, 'subject', 'title') or '(χωρίς θέμα)',
        organization=_first_text(
            organization_value,
            'label',
            'name',
        ) or _dig(decision, 'organizationName', 'organizationLabel', 'organization.label', 'organization.name'),
        organization_uid=_dig(decision, 'organizationUid', 'organization.uid', 'org', 'organizationId'),
        decision_type=_first_text(decision_type_value, 'label', 'name') or _dig(decision, 'decisionTypeLabel', 'typeLabel', 'type.label'),
        decision_type_uid=_dig(decision, 'decisionTypeUid', 'decisionType.uid', 'type', 'type.uid', 'decisionTypeId'),
        issue_date=_normalize_diavgeia_datetime(issue_date_raw, date_only=True),
        submission_timestamp=_normalize_diavgeia_datetime(submission_raw),
        status=_dig(decision, 'status'),
        url=view_url,
        api_url=api_url,
        raw=decision,
    )

def decisions_to_public_dicts(decisions: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for decision in decisions:
        normalized = normalize_decision(decision)
        result.append({
            'ada': normalized.ada,
            'subject': normalized.subject,
            'organization': normalized.organization,
            'organization_uid': normalized.organization_uid,
            'decision_type': normalized.decision_type,
            'decision_type_uid': normalized.decision_type_uid,
            'issue_date': normalized.issue_date,
            'submission_timestamp': normalized.submission_timestamp,
            'status': normalized.status,
            'url': normalized.url,
            'api_url': normalized.api_url,
        })
    return result


def hydrate_decisions(client: 'DiavgeiaClient', decisions: list[Mapping[str, Any]], *, max_items: int | None = None) -> list[Mapping[str, Any]]:
    """Fetch detail payloads for search results and merge them for richer metadata.

    Search results can omit organization/type metadata. Hydration is intentionally
    opt-in because it performs one extra API call per returned decision.
    """
    hydrated: list[Mapping[str, Any]] = []
    limit = len(decisions) if max_items is None else min(len(decisions), max_items)
    for idx, decision in enumerate(decisions):
        if idx >= limit:
            hydrated.append(decision)
            continue
        ada = normalize_decision(decision).ada
        if not ada:
            hydrated.append(decision)
            continue
        try:
            detail = client.get_decision(ada)
        except DiavgeiaClientError:
            hydrated.append(decision)
            continue
        hydrated.append(_merge_prefer_detail(decision, detail))
    return hydrated


class DiavgeiaClient:
    """Small read-only client for Diavgeia OpenData.

    This intentionally covers only the safe discovery operations we need for the
    integration branch: search, fetch one decision, version log, types and terms.
    It does not implement publication/editing endpoints.
    """

    def __init__(self, base_url: str | None = None, timeout_seconds: int | None = None, http_client: httpx.Client | None = None):
        settings = get_settings()
        self.base_url = (base_url or settings.diavgeia_base_url).rstrip('/')
        self.timeout_seconds = timeout_seconds or settings.diavgeia_timeout_seconds
        self._client = http_client

    def _get(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        close_client = False
        client = self._client
        if client is None:
            client = httpx.Client(timeout=self.timeout_seconds, headers={'Accept': 'application/json'})
            close_client = True
        try:
            response = client.get(
                f'{self.base_url}{path}',
                params={k: v for k, v in (params or {}).items() if v not in (None, '')},
                headers={'Accept': 'application/json'},
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise DiavgeiaClientError(f'Unexpected Diavgeia response type: {type(data).__name__}')
            return data
        except httpx.HTTPError as exc:
            raise DiavgeiaClientError(f'Diavgeia API request failed: {exc}') from exc
        except ValueError as exc:
            raise DiavgeiaClientError('Diavgeia API did not return JSON. Check DIAVGEIA_BASE_URL/Accept headers.') from exc
        finally:
            if close_client:
                client.close()

    def search(
        self,
        *,
        ada: str | None = None,
        subject: str | None = None,
        protocol: str | None = None,
        term: str | None = None,
        org: str | None = None,
        unit: str | None = None,
        signer: str | None = None,
        decision_type: str | None = None,
        tag: str | None = None,
        from_date: str | date | None = None,
        to_date: str | date | None = None,
        from_issue_date: str | date | None = None,
        to_issue_date: str | date | None = None,
        status: str = 'all',
        page: int = 0,
        size: int | None = None,
        sort: str = 'recent',
    ) -> dict[str, Any]:
        settings = get_settings()
        return self._get('/search', {
            'ada': ada,
            'subject': subject,
            'protocol': protocol,
            'term': term,
            'org': org,
            'unit': unit,
            'signer': signer,
            'type': decision_type,
            'tag': tag,
            'from_date': _date_to_api(from_date),
            'to_date': _date_to_api(to_date),
            'from_issue_date': _date_to_api(from_issue_date),
            'to_issue_date': _date_to_api(to_issue_date),
            'status': status,
            'page': page,
            'size': size or settings.diavgeia_default_page_size,
            'sort': sort,
        })

    def advanced_search(self, q: str, *, page: int = 0, size: int | None = None) -> dict[str, Any]:
        settings = get_settings()
        return self._get('/search/advanced', {'q': q, 'page': page, 'size': size or settings.diavgeia_default_page_size})

    def search_by_adam(self, adam: str, *, days_back: int | None = None, page: int = 0, size: int | None = None) -> dict[str, Any]:
        from_date = None
        to_date = None
        if days_back:
            today = today_local()
            from_date = today - timedelta(days=max(1, days_back))
            to_date = today
        return self.search(term=adam, from_date=from_date, to_date=to_date, page=page, size=size, sort='recent')

    def get_decision(self, ada: str) -> dict[str, Any]:
        return self._get(f'/decisions/{ada}/')

    def get_decision_version_log(self, ada: str) -> dict[str, Any]:
        return self._get(f'/decisions/{ada}/versionlog')

    def get_decision_types(self) -> dict[str, Any]:
        return self._get('/types')

    def get_decision_type_details(self, decision_type: str) -> dict[str, Any]:
        return self._get(f'/types/{decision_type}/details')

    def get_common_search_terms(self) -> dict[str, Any]:
        return self._get('/search/terms/common')


def _date_to_api(value: str | date | None) -> str | None:
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)
