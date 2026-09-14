from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

import httpx

from app.config import get_settings
from app.services.api_sync import ApiCheckpointStore, query_fingerprint
from app.services.timezone import app_tz
from app.services.text_normalizer import normalize_text_tree

logger = logging.getLogger(__name__)

class AdaptiveRateLimiter:
    """Single-worker request pacer that slows down on 429 and recovers gradually."""

    def __init__(self, requests_per_minute: int) -> None:
        rpm = max(1, min(int(requests_per_minute), 300))
        self.base_interval = 60.0 / rpm
        self.interval = self.base_interval
        self.last_request_at: float | None = None

    def wait(self) -> None:
        now = time.monotonic()
        if self.last_request_at is not None:
            remaining = self.interval - (now - self.last_request_at)
            if remaining > 0:
                time.sleep(remaining)
        self.last_request_at = time.monotonic()

    def success(self) -> None:
        self.interval = max(self.base_interval, self.interval * 0.90)

    def penalize(self, retry_after: float) -> None:
        self.interval = min(10.0, max(self.interval * 2.0, retry_after))


OPERATION_TYPES: dict[str, dict[str, str]] = {
    'notice': {'label': 'Προσκλήσεις / Προκηρύξεις / Διακηρύξεις', 'path': 'notice', 'source': 'khmdhs_notice'},
    'request': {'label': 'Αιτήματα', 'path': 'request', 'source': 'khmdhs_request'},
}


FRIENDLY_OPERATION_CONTEXT: dict[str, dict[str, str]] = {
    'notice': {
        'friendly_label': 'Ευκαιρία συμμετοχής',
        'short': 'Διακήρυξη/πρόσκληση στην οποία μπορεί δυνητικά να συμμετάσχει η επιχείρηση.',
        'usage': 'Δώστε προτεραιότητα σε ενεργές πράξεις με υψηλό score και κοντινή προθεσμία.',
    },
    'request': {
        'friendly_label': 'Πρώιμο σήμα',
        'short': 'Αίτημα ή προπαρασκευαστική πράξη που δείχνει πιθανή μελλοντική ανάγκη.',
        'usage': 'Χρήσιμο για παρακολούθηση φορέων πριν βγει διακήρυξη ή ανάθεση.',
    },
}

KIMDIS_VIEWS: dict[str, dict[str, object]] = {
    'opportunities': {
        'label': 'Ευκαιρίες συμμετοχής',
        'resources': ['notice'],
        'description': 'Διακηρύξεις και προσκλήσεις στις οποίες μπορεί δυνητικά να συμμετάσχει η επιχείρηση. Αυτό είναι το βασικό καθημερινό view.',
    },
    'signals': {
        'label': 'Πρώιμα σήματα',
        'resources': ['request'],
        'description': 'Αιτήματα που δείχνουν πιθανή μελλοντική ανάγκη ή δαπάνη. Δεν είναι πάντα ανοιχτοί διαγωνισμοί.',
    },
    'advanced': {
        'label': 'Advanced αναζήτηση',
        'resources': [],
        'description': 'Τεχνική αναζήτηση ανά είδος πράξης ΚΗΜΔΗΣ για πιο ειδικές περιπτώσεις.',
    },
}

CONTRACT_TYPES = {
    '': 'Όλοι',
    '9': 'Υπηρεσίες',
    '10': 'Έργα',
    '12': 'Μελέτες',
    '13': 'Προμήθειες',
    '14': 'Τεχνικές ή λοιπές συναφείς υπηρεσίες',
}


def _as_utc(dt: datetime) -> datetime:
    # Το ΚΗΜΔΗΣ συνήθως επιστρέφει datetime χωρίς timezone.
    # Τα θεωρούμε ώρα Ελλάδας και τα αποθηκεύουμε σε UTC, ώστε στο UI
    # να εμφανίζονται ξανά σωστά σε Europe/Athens.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=app_tz())
    return dt.astimezone(timezone.utc)


def parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, date):
        return _as_utc(datetime.combine(value, datetime.min.time()))
    text = str(value).strip()
    if not text:
        return None
    # ΚΗΜΔΗΣ επιστρέφει π.χ. 2025-01-21T11:21:09.222823 ή ημερομηνίες χωρίς ώρα.
    for candidate in (text, text.replace('Z', '+00:00'), text.replace(' ', 'T')):
        try:
            return _as_utc(datetime.fromisoformat(candidate))
        except ValueError:
            continue
    return None


def _kv_value(data: Any) -> Optional[str]:
    if isinstance(data, dict):
        return data.get('value') or data.get('key')
    if isinstance(data, list):
        values = [_kv_value(item) for item in data]
        return '; '.join(value for value in values if value) or None
    if data is None:
        return None
    return str(data)


def _kv_key(data: Any) -> Optional[str]:
    if isinstance(data, dict):
        return data.get('key')
    if data is None:
        return None
    return str(data)


def extract_cpvs(record: Dict[str, Any]) -> tuple[List[str], Dict[str, str]]:
    codes: List[str] = []
    descriptions: Dict[str, str] = {}
    for obj in record.get('objectDetails') or record.get('objectDetailsList') or []:
        for cpv in obj.get('cpvs') or []:
            if isinstance(cpv, dict):
                code = cpv.get('key')
                desc = cpv.get('value')
            else:
                code, desc = str(cpv), ''
            if code and code not in codes:
                codes.append(code)
            if code and desc:
                descriptions[code] = desc
    return codes, descriptions


def _first_value(*values: Any) -> Any:
    for value in values:
        if value not in (None, '', []):
            return value
    return None


def _as_number(value: str) -> int | float | None:
    text = (value or '').strip().replace(',', '.')
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return int(number) if number.is_integer() else number


def _final_date_start(value: str) -> str:
    text = (value or '').strip()
    if not text:
        return ''
    return f'{text} 00:00' if len(text) == 10 else text


def _final_date_end(value: str) -> str:
    text = (value or '').strip()
    if not text:
        return ''
    return f'{text} 23:59' if len(text) == 10 else text


def _retry_after_seconds(response: httpx.Response, fallback: float) -> float:
    """Return a safe wait time after a KIMDIS 429 response.

    KIMDIS usually does not send Retry-After, but when it does we respect it.
    The fallback is intentionally capped so a manual ingest does not appear hung
    forever, while still giving the public endpoint a real cooldown.
    """
    header = (response.headers.get('Retry-After') or '').strip()
    if header:
        try:
            return max(1.0, min(float(header), 120.0))
        except ValueError:
            pass
    return max(1.0, min(fallback, 120.0))


def infer_resource_from_reference_number(reference_number: str) -> str | None:
    ref = (reference_number or '').strip().upper()
    if 'REQ' in ref:
        return 'request'
    if 'PROC' in ref:
        return 'notice'
    if 'AWRD' in ref:
        return 'auction'
    if 'SYMV' in ref:
        return 'contract'
    if 'PAY' in ref:
        return 'payment'
    return None


def _organization(record: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    # Most opendata resources include organization. Some examples include
    # contractingData.unitsOperator, so we keep that as fallback.
    organization = record.get('organization') or {}
    if not organization:
        contracting = record.get('contractingData') or {}
        organization = contracting.get('unitsOperator') or contracting.get('operator') or {}
    return _kv_key(organization), _kv_value(organization)


def _nested_value(record: Dict[str, Any], *paths: str) -> Any:
    for path in paths:
        current: Any = record
        for part in path.split('.'):
            if isinstance(current, dict):
                current = current.get(part)
            else:
                current = None
                break
        if current not in (None, '', [], {}):
            return current
    return None


def _contractor(record: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    candidates = _nested_value(
        record,
        'contractors',
        'contractor',
        'contractingData.contractors',
        'contractingData.contractor',
        'economicOperators',
    )
    if not isinstance(candidates, list):
        candidates = [candidates] if candidates else []
    names: list[str] = []
    vats: list[str] = []
    for candidate in candidates:
        if isinstance(candidate, dict):
            name = _first_value(candidate.get('name'), candidate.get('value'), candidate.get('contractorName'), candidate.get('economicOperatorName'))
            vat = _first_value(candidate.get('vatNumber'), candidate.get('vat'), candidate.get('taxId'), candidate.get('afm'))
        else:
            name, vat = candidate, None
        if name and str(name).strip() not in names:
            names.append(str(name).strip())
        if vat and str(vat).strip() not in vats:
            vats.append(str(vat).strip())
    direct_name = _nested_value(record, 'contractorName')
    direct_vat = _nested_value(record, 'vatNumber', 'contractorVatNumber')
    if direct_name and str(direct_name).strip() not in names:
        names.append(str(direct_name).strip())
    if direct_vat and str(direct_vat).strip() not in vats:
        vats.append(str(direct_vat).strip())
    return ('; '.join(names) or None, '; '.join(vats) or None)


def build_search_body(
    *,
    resource: str = 'notice',
    title: str = '',
    reference_number: str = '',
    cpv_items: Optional[Iterable[str]] = None,
    organizations: Optional[Iterable[str]] = None,
    contract_type: str = '',
    procedure_type: str = '',
    date_from: str = '',
    date_to: str = '',
    total_cost_from: str = '',
    total_cost_to: str = '',
    final_date_from: str = '',
    final_date_to: str = '',
    cancel_date_from: str = '',
    cancel_date_to: str = '',
    signer: str = '',
    aaht: str = '',
    public_funding_ref_num: str = '',
    vat_number: str = '',
    contractor_name: str = '',
    estimated_total_cost_from: str = '',
    estimated_total_cost_to: str = '',
    is_modified: bool | None = False,
    is_initial: bool | None = None,
    is_approved: bool | None = None,
    is_approval: bool | None = None,
    include_final_dates: bool = True,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {}
    # Κάθε ΚΗΜΔΗΣ endpoint έχει ελαφρώς διαφορετικό request schema.
    # Τα notice/auction/contract δέχονται isModified, ενώ request/payment
    # το απορρίπτουν με 400 Invalid request payload.
    if resource in {'notice', 'auction', 'contract'} and is_modified is not None:
        body['isModified'] = is_modified
    if title.strip():
        body['title'] = title.strip()[:100]
    if reference_number.strip():
        body['referenceNumber'] = reference_number.strip()
    cpv_list = [x.strip() for x in (cpv_items or []) if x and x.strip()]
    if cpv_list:
        body['cpvItems'] = cpv_list
    org_list = [x.strip() for x in (organizations or []) if x and x.strip()]
    if org_list:
        # The documentation alternates between organization and organizations.
        # Existing examples use organizations; the API currently accepts that form.
        body['organizations'] = org_list
    if contract_type.strip():
        body['contractType'] = contract_type.strip()
    if procedure_type.strip() and resource in {'notice', 'auction', 'contract'}:
        body['procedureType'] = procedure_type.strip()
    if date_from.strip():
        body['dateFrom'] = date_from.strip()
    if date_to.strip():
        body['dateTo'] = date_to.strip()
    if cancel_date_from.strip():
        body['cancelDateFrom'] = cancel_date_from.strip()
    if cancel_date_to.strip():
        body['cancelDateTo'] = cancel_date_to.strip()
    if signer.strip():
        body['signer'] = signer.strip()
    if aaht.strip() and resource in {'notice', 'auction', 'contract'}:
        body['aaht'] = aaht.strip()
    if public_funding_ref_num.strip() and resource in {'notice', 'contract', 'payment'}:
        body['publicFundingRefNum'] = public_funding_ref_num.strip()
    if vat_number.strip() and resource in {'auction', 'contract', 'payment'}:
        body['vatNumber'] = vat_number.strip()
    if contractor_name.strip() and resource in {'auction', 'contract', 'payment'}:
        body['contractorName'] = contractor_name.strip()[:255]
    cost_from = _as_number(total_cost_from)
    cost_to = _as_number(total_cost_to)
    if cost_from is not None:
        body['totalCostFrom'] = cost_from
    if cost_to is not None:
        body['totalCostTo'] = cost_to
    estimated_from = _as_number(estimated_total_cost_from)
    estimated_to = _as_number(estimated_total_cost_to)
    if estimated_from is not None and resource in {'auction', 'contract'}:
        body['estTotalCostFrom'] = estimated_from
    if estimated_to is not None and resource in {'auction', 'contract'}:
        body['estTotalCostTo'] = estimated_to
    if resource == 'request':
        if is_initial is not None:
            body['isInitial'] = is_initial
        if is_approved is not None:
            body['isApproved'] = is_approved
        if is_approval is not None:
            body['isApproval'] = is_approval
    if include_final_dates and resource == 'notice':
        # Το documentation του notice δείχνει finalDateFrom/finalDateTo σε μορφή
        # YYYY-MM-DD HH:mm. Αν ο χρήστης δώσει απλή ημερομηνία, τη μετατρέπουμε
        # σε αρχή/τέλος ημέρας για να αποφεύγονται 400 Bad Request.
        if final_date_from.strip():
            body['finalDateFrom'] = _final_date_start(final_date_from)
        if final_date_to.strip():
            body['finalDateTo'] = _final_date_end(final_date_to)
    return body


class KhmdhsClient:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.base_url = self.settings.khmdhs_base_url.rstrip('/')
        self.last_rate_limited = False
        self.last_hit_max_pages = False
        self.last_pages_fetched = 0
        self.last_rate_limit_hits = 0
        self.last_transient_error = False
        self.last_transport_error_count = 0
        self.last_cache_hit = False
        self.last_resumed_from_page = 0
        self.rate_limiter = AdaptiveRateLimiter(self.settings.khmdhs_requests_per_minute)

    def search_resource(
        self,
        resource: str,
        body: Dict[str, Any],
        max_pages: Optional[int] = None,
        *,
        checkpoint_store: ApiCheckpointStore | None = None,
        stream_key: str | None = None,
    ) -> List[Dict[str, Any]]:
        if resource not in OPERATION_TYPES:
            raise ValueError(f'Unsupported KIMDIS resource: {resource}')
        path = OPERATION_TYPES[resource]['path']
        max_pages = max_pages or self.settings.khmdhs_max_pages
        self.last_rate_limited = False
        self.last_hit_max_pages = False
        self.last_pages_fetched = 0
        self.last_rate_limit_hits = 0
        self.last_transient_error = False
        self.last_transport_error_count = 0
        self.last_cache_hit = False
        self.last_resumed_from_page = 0
        records: List[Dict[str, Any]] = []
        start_page = 0
        fingerprint = query_fingerprint(resource, body)
        if checkpoint_store is not None and stream_key:
            prepared = checkpoint_store.prepare(stream_key, resource, fingerprint, body)
            records = prepared.records
            start_page = prepared.next_page
            self.last_cache_hit = prepared.cache_hit
            self.last_resumed_from_page = start_page if prepared.resumed else 0
            if prepared.cache_hit:
                return records
        completed = False
        try:
            with httpx.Client(timeout=self.settings.khmdhs_timeout_seconds, headers={'Accept': 'application/json'}) as client:
                for page in range(start_page, start_page + max_pages):
                    self.rate_limiter.wait()
                    self.last_pages_fetched += 1
                    url = f'{self.base_url}/khmdhs-opendata/{path}?page={page}'
                    response = None
                    max_retries = max(0, int(self.settings.khmdhs_rate_limit_retries))
                    base_delay = max(1.0, float(self.settings.khmdhs_rate_limit_base_delay_seconds))
                    transport_retries = max(0, int(self.settings.khmdhs_transport_retries))
                    transport_base_delay = max(0.1, float(self.settings.khmdhs_transport_base_delay_seconds))
                    rate_attempt = 0
                    transport_attempt = 0
                    while True:
                        try:
                            response = client.post(url, json=body)
                        except httpx.RequestError as exc:
                            self.last_transport_error_count += 1
                            if transport_attempt < transport_retries:
                                wait_seconds = min(30.0, transport_base_delay * (2 ** transport_attempt))
                                transport_attempt += 1
                                logger.warning(
                                    'KIMDIS temporary %s on %s page %s. Retrying in %.1fs (%s/%s)',
                                    type(exc).__name__,
                                    resource,
                                    page,
                                    wait_seconds,
                                    transport_attempt,
                                    transport_retries,
                                )
                                time.sleep(wait_seconds)
                                continue
                            self.last_transient_error = True
                            logger.warning(
                                'KIMDIS temporary %s on %s page %s after %s retries; deferring to continuation',
                                type(exc).__name__,
                                resource,
                                page,
                                transport_retries,
                            )
                            response = None
                            break
                        response.encoding = 'utf-8'
                        if response.status_code != 429:
                            self.rate_limiter.success()
                            break
                        self.last_rate_limit_hits += 1
                        if rate_attempt < max_retries:
                            fallback_wait = base_delay * (2 ** rate_attempt)
                            wait_seconds = _retry_after_seconds(response, fallback_wait)
                            self.rate_limiter.penalize(wait_seconds)
                            rate_attempt += 1
                            logger.warning(
                                'KIMDIS rate limit hit on %s page %s. Retrying in %.1fs (%s/%s)',
                                resource,
                                page,
                                wait_seconds,
                                rate_attempt,
                                max_retries,
                            )
                            time.sleep(wait_seconds)
                        else:
                            self.last_rate_limited = True
                            logger.warning('KIMDIS rate limit hit on %s page %s after %s retries', resource, page, max_retries)
                    if response is None or response.status_code == 429:
                        break
                    if response.status_code == 404:
                        completed = True
                        break
                    try:
                        response.raise_for_status()
                    except httpx.HTTPStatusError as exc:
                        detail = response.text[:500]
                        raise httpx.HTTPStatusError(
                            f"{exc}. Response body: {detail}",
                            request=exc.request,
                            response=exc.response,
                        ) from exc
                    payload = normalize_text_tree(response.json())
                    content = payload.get('content') or []
                    records.extend(content)
                    total_pages = payload.get('totalPages')
                    if checkpoint_store is not None and stream_key:
                        checkpoint_store.save_page(
                            stream_key,
                            next_page=page + 1,
                            total_pages=int(total_pages) if total_pages is not None else None,
                            records=records,
                        )
                    if payload.get('last', True):
                        completed = True
                        break
                else:
                    self.last_hit_max_pages = True
                    logger.warning('KIMDIS search for %s reached max_pages=%s before API last page', resource, max_pages)
        except Exception as exc:
            if checkpoint_store is not None and stream_key:
                checkpoint_store.fail(stream_key, f'{type(exc).__name__}: {exc}')
            raise

        if checkpoint_store is not None and stream_key:
            if completed:
                checkpoint_store.complete(stream_key, records)
            else:
                if self.last_rate_limited:
                    reason = 'rate_limited'
                elif self.last_transient_error:
                    reason = 'temporary_transport_error'
                else:
                    reason = 'max_pages_or_incomplete'
                checkpoint_store.fail(stream_key, reason)
        return records

    def search_notices(
        self,
        date_from: str,
        date_to: str,
        cpv_items: Optional[Iterable[str]] = None,
        max_pages: Optional[int] = None,
        *,
        checkpoint_store: ApiCheckpointStore | None = None,
        stream_key: str | None = None,
    ) -> List[Dict[str, Any]]:
        body = build_search_body(resource='notice', date_from=date_from, date_to=date_to, cpv_items=cpv_items, is_modified=False)
        return self.search_resource(
            'notice', body, max_pages=max_pages,
            checkpoint_store=checkpoint_store, stream_key=stream_key,
        )

    def search_cancelled_notices(
        self,
        cancel_date_from: str,
        cancel_date_to: str,
        cpv_items: Optional[Iterable[str]] = None,
        max_pages: Optional[int] = None,
        *,
        checkpoint_store: ApiCheckpointStore | None = None,
        stream_key: str | None = None,
    ) -> List[Dict[str, Any]]:
        body = build_search_body(
            resource='notice',
            cpv_items=cpv_items,
            cancel_date_from=cancel_date_from,
            cancel_date_to=cancel_date_to,
            is_modified=False,
        )
        return self.search_resource(
            'notice', body, max_pages=max_pages,
            checkpoint_store=checkpoint_store, stream_key=stream_key,
        )

    def pde_chain(self, pde_number: str) -> Dict[str, Any]:
        value = (pde_number or '').strip()
        if not value:
            return {}
        with httpx.Client(timeout=self.settings.khmdhs_timeout_seconds, headers={'Accept': 'application/json'}) as client:
            response = client.get(f'{self.base_url}/khmdhs-opendata/pde', params={'pdeNumber': value})
            if response.status_code == 404:
                return {}
            response.raise_for_status()
            payload = normalize_text_tree(response.json())
            return payload if isinstance(payload, dict) else {}

    def attachment_url(self, reference_number: str, resource: str = 'notice') -> str:
        path = OPERATION_TYPES.get(resource, OPERATION_TYPES['notice'])['path']
        return f'{self.base_url}/khmdhs-opendata/{path}/attachment/{reference_number}'

    def adam_chain(
        self,
        reference_number: str,
        *,
        timeout_seconds: float = 10.0,
    ) -> Dict[str, Any] | List[Dict[str, Any]]:
        """Return connected KIMDIS acts for an ADAM/reference number.

        The Open Data API may return either a list or an object depending on the
        record type. The UI handles both shapes defensively.
        """
        ref = (reference_number or '').strip()
        if not ref:
            return []
        # adamChain is an auxiliary UI lookup and must never hold the whole tender
        # detail page for the much larger ingestion timeout. Some PROC references
        # are known to stall while the linked REQ responds immediately.
        timeout = max(2.0, min(float(timeout_seconds), float(self.settings.khmdhs_timeout_seconds)))
        with httpx.Client(timeout=timeout, headers={'Accept': 'application/json'}) as client:
            url = f'{self.base_url}/khmdhs-opendata/adamChain/{ref}'
            response = client.get(url)
            response.encoding = 'utf-8'
            if response.status_code == 404:
                return []
            response.raise_for_status()
            return normalize_text_tree(response.json())

    def request_by_reference(
        self,
        reference_number: str,
        *,
        timeout_seconds: float = 10.0,
    ) -> Dict[str, Any]:
        """Fetch one request record without the ingestion retry/backoff loop.

        Request records expose the surrounding lifecycle references directly and
        are more reliable than adamChain for the common PROC -> approved REQ path.
        """
        ref = (reference_number or '').strip()
        if not ref:
            return {}
        timeout = max(2.0, min(float(timeout_seconds), float(self.settings.khmdhs_timeout_seconds)))
        with httpx.Client(timeout=timeout, headers={'Accept': 'application/json'}) as client:
            response = client.post(
                f'{self.base_url}/khmdhs-opendata/request?page=0',
                json={'referenceNumber': ref},
            )
            if response.status_code == 404:
                return {}
            response.raise_for_status()
            payload = normalize_text_tree(response.json())
            if not isinstance(payload, dict):
                return {}
            content = payload.get('content') or []
            if not isinstance(content, list):
                return {}
            exact = next(
                (row for row in content if isinstance(row, dict) and str(row.get('referenceNumber') or '').strip() == ref),
                None,
            )
            return exact or (content[0] if content and isinstance(content[0], dict) else {})

    def normalize_record(self, resource: str, record: Dict[str, Any]) -> Dict[str, Any]:
        record = normalize_text_tree(record)
        if resource not in OPERATION_TYPES:
            resource = 'notice'
        cpv_codes, cpv_descriptions = extract_cpvs(record)
        reference_number = record.get('referenceNumber') or record.get('adam') or record.get('ADAM')
        organization_key, organization_name = _organization(record)
        contractor_name, contractor_vat_number = _contractor(record)
        total_without_vat = _first_value(record.get('totalCostWithoutVAT'), record.get('budget'), record.get('costWithoutVAT'))
        estimated_total = _first_value(
            record.get('estTotalCostWithoutVAT'),
            record.get('estimatedTotalCostWithoutVAT'),
            record.get('estimatedTotalCost'),
            record.get('estTotalCost'),
            total_without_vat if resource in {'request', 'notice'} else None,
        )
        attachment_url = self.attachment_url(reference_number, resource) if reference_number else None
        public_url = f'https://cerpp.eprocurement.gov.gr/khmdhs/search?referenceNumber={reference_number}' if reference_number else None
        return {
            'source': OPERATION_TYPES[resource]['source'],
            'source_reference': reference_number or record.get('id') or record.get('title'),
            'reference_number': reference_number,
            'title': record.get('title') or record.get('subject') or '(χωρίς τίτλο)',
            'organization_key': organization_key,
            'organization_name': organization_name,
            'submission_date': parse_dt(record.get('submissionDate')),
            'final_submission_date': parse_dt(record.get('finalSubmissionDate')),
            'published_date': parse_dt(_first_value(record.get('publishedDate'), record.get('signedDate'), record.get('lastUpdateDate'))),
            'total_cost_without_vat': total_without_vat,
            'total_cost_with_vat': record.get('totalCostWithVAT'),
            'contract_type': _kv_value(_first_value(record.get('contractType'), record.get('contractTypes'))),
            'procedure_type': _kv_value(_first_value(record.get('procedureType'), record.get('typeOfProcedure'), record.get('awardProcedure'))),
            'cpv_codes': cpv_codes,
            'cpv_descriptions': cpv_descriptions,
            'url': public_url,
            'attachment_url': attachment_url,
            'raw': record,
            'cancelled': bool(record.get('cancelled', False)),
            'contractor_name': contractor_name,
            'contractor_vat_number': contractor_vat_number,
            'aaht': _first_value(record.get('aaht'), _nested_value(record, 'organization.aaht')),
            'public_funding_ref_num': _first_value(record.get('publicFundingRefNum'), record.get('pdeNumber')),
            'estimated_total_cost': estimated_total,
            'contract_value': total_without_vat if resource in {'auction', 'contract'} else None,
            'payment_amount': total_without_vat if resource == 'payment' else None,
            'protocol_number': record.get('protocolNumber'),
            'approval_ada': record.get('approvalADA'),
            'previous_reference_number': _first_value(record.get('previousReferenceNumber'), record.get('previousRequestReferenceNumber')),
            'cancellation_date': parse_dt(record.get('cancellationDate')),
            'cancellation_reason': record.get('cancellationReason'),
            'cancellation_ada': record.get('cancellationADA'),
            'is_modified': bool(record.get('isModified', False)),
        }

    def normalize_notice(self, notice: Dict[str, Any]) -> Dict[str, Any]:
        return self.normalize_record('notice', notice)
