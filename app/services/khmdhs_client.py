from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

import httpx

from app.config import get_settings
from app.services.timezone import app_tz
from app.services.text_normalizer import normalize_text_tree

logger = logging.getLogger(__name__)

# Defaults are read from .env through Settings. Keep these names only as
# documentation of the old behavior; runtime uses self.settings below.
PAGE_DELAY_SECONDS = 1.0
RATE_LIMIT_RETRIES = 4


OPERATION_TYPES: dict[str, dict[str, str]] = {
    'notice': {'label': 'Προσκλήσεις / Προκηρύξεις / Διακηρύξεις', 'path': 'notice', 'source': 'khmdhs_notice'},
    'request': {'label': 'Αιτήματα', 'path': 'request', 'source': 'khmdhs_request'},
    'auction': {'label': 'Αναθέσεις', 'path': 'auction', 'source': 'khmdhs_auction'},
    'contract': {'label': 'Συμβάσεις', 'path': 'contract', 'source': 'khmdhs_contract'},
    'payment': {'label': 'Εντολές πληρωμής', 'path': 'payment', 'source': 'khmdhs_payment'},
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
    'auction': {
        'friendly_label': 'Ανάθεση που έγινε',
        'short': 'Πράξη ανάθεσης. Συνήθως δεν είναι νέα ευκαιρία, αλλά δείχνει ποιος πήρε τη δουλειά.',
        'usage': 'Χρήσιμο για έρευνα αγοράς, ανταγωνιστές και φορείς που αγοράζουν παρόμοιες υπηρεσίες.',
    },
    'contract': {
        'friendly_label': 'Υπογεγραμμένη σύμβαση',
        'short': 'Τελικό συμβατικό αντικείμενο μετά από ανάθεση/διαγωνισμό.',
        'usage': 'Χρήσιμο για ιστορικό ποσών, αναδόχων και επαναλαμβανόμενων αναγκών.',
    },
    'payment': {
        'friendly_label': 'Πληρωμή / ιστορικό δαπάνης',
        'short': 'Εντολή πληρωμής. Η διαδικασία είναι συνήθως ολοκληρωμένη.',
        'usage': 'Χρήσιμο για να δείτε ποιοι φορείς πληρώνουν για παρόμοια αντικείμενα.',
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
    'market': {
        'label': 'Έρευνα αγοράς',
        'resources': ['auction', 'contract', 'payment'],
        'description': 'Αναθέσεις, συμβάσεις και πληρωμές. Δεν είναι συνήθως νέες ευκαιρίες, αλλά δείχνουν ποιος πήρε τι, από ποιον φορέα και με τι ποσό.',
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
    is_modified: bool = False,
    include_final_dates: bool = True,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {}
    # Κάθε ΚΗΜΔΗΣ endpoint έχει ελαφρώς διαφορετικό request schema.
    # Τα notice/auction/contract δέχονται isModified, ενώ request/payment
    # το απορρίπτουν με 400 Invalid request payload.
    if resource in {'notice', 'auction', 'contract'}:
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
    cost_from = _as_number(total_cost_from)
    cost_to = _as_number(total_cost_to)
    if cost_from is not None:
        body['totalCostFrom'] = cost_from
    if cost_to is not None:
        body['totalCostTo'] = cost_to
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

    def search_resource(self, resource: str, body: Dict[str, Any], max_pages: Optional[int] = None) -> List[Dict[str, Any]]:
        if resource not in OPERATION_TYPES:
            raise ValueError(f'Unsupported KIMDIS resource: {resource}')
        path = OPERATION_TYPES[resource]['path']
        max_pages = max_pages or self.settings.khmdhs_max_pages
        self.last_rate_limited = False
        self.last_hit_max_pages = False
        self.last_pages_fetched = 0
        self.last_rate_limit_hits = 0
        records: List[Dict[str, Any]] = []
        with httpx.Client(timeout=self.settings.khmdhs_timeout_seconds, headers={'Accept': 'application/json'}) as client:
            for page in range(max_pages):
                self.last_pages_fetched = page + 1
                url = f'{self.base_url}/khmdhs-opendata/{path}?page={page}'
                response = None
                max_retries = max(0, int(self.settings.khmdhs_rate_limit_retries))
                base_delay = max(1.0, float(self.settings.khmdhs_rate_limit_base_delay_seconds))
                for attempt in range(max_retries + 1):
                    response = client.post(url, json=body)
                    response.encoding = 'utf-8'
                    if response.status_code != 429:
                        break
                    self.last_rate_limit_hits += 1
                    if attempt < max_retries:
                        fallback_wait = base_delay * (2 ** attempt)
                        wait_seconds = _retry_after_seconds(response, fallback_wait)
                        logger.warning(
                            'KIMDIS rate limit hit on %s page %s. Retrying in %.1fs (%s/%s)',
                            resource,
                            page,
                            wait_seconds,
                            attempt + 1,
                            max_retries,
                        )
                        time.sleep(wait_seconds)
                    else:
                        self.last_rate_limited = True
                        logger.warning('KIMDIS rate limit hit on %s page %s after %s retries', resource, page, max_retries)
                if response is None:
                    break
                if response.status_code == 429:
                    break
                if response.status_code == 404:
                    # KIMDIS returns 404 with {"message": "No ... found for the given criteria"}
                    # when a valid search has no results. Treat that as an empty page,
                    # not as an application error. Other 404 shapes still stop the loop
                    # defensively with an empty result set, because the requested endpoint
                    # may simply have no content for the selected filters/date range.
                    try:
                        detail_json = response.json()
                    except ValueError:
                        detail_json = {}
                    message = str(detail_json.get('message') or response.text or '').lower()
                    if 'no ' in message and 'found' in message:
                        logger.info('KIMDIS returned no %s records for page %s and selected criteria', resource, page)
                        break
                    logger.warning('KIMDIS returned 404 for %s page %s: %s', resource, page, response.text[:300])
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
                if payload.get('last', True):
                    break
                page_delay = max(0.0, float(self.settings.khmdhs_page_delay_seconds))
                if page_delay > 0:
                    time.sleep(page_delay)
            else:
                self.last_hit_max_pages = True
                logger.warning('KIMDIS search for %s reached max_pages=%s before API last page', resource, max_pages)
        return records

    def search_notices(
        self,
        date_from: str,
        date_to: str,
        cpv_items: Optional[Iterable[str]] = None,
        max_pages: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        body = build_search_body(resource='notice', date_from=date_from, date_to=date_to, cpv_items=cpv_items, is_modified=False)
        return self.search_resource('notice', body, max_pages=max_pages)

    def attachment_url(self, reference_number: str, resource: str = 'notice') -> str:
        path = OPERATION_TYPES.get(resource, OPERATION_TYPES['notice'])['path']
        return f'{self.base_url}/khmdhs-opendata/{path}/attachment/{reference_number}'

    def adam_chain(self, reference_number: str) -> Dict[str, Any] | List[Dict[str, Any]]:
        """Return connected KIMDIS acts for an ADAM/reference number.

        The Open Data API may return either a list or an object depending on the
        record type. The UI handles both shapes defensively.
        """
        ref = (reference_number or '').strip()
        if not ref:
            return []
        with httpx.Client(timeout=self.settings.khmdhs_timeout_seconds, headers={'Accept': 'application/json'}) as client:
            url = f'{self.base_url}/khmdhs-opendata/adamChain/{ref}'
            response = client.get(url)
            response.encoding = 'utf-8'
            if response.status_code == 404:
                return []
            response.raise_for_status()
            return normalize_text_tree(response.json())

    def normalize_record(self, resource: str, record: Dict[str, Any]) -> Dict[str, Any]:
        record = normalize_text_tree(record)
        if resource not in OPERATION_TYPES:
            resource = 'notice'
        cpv_codes, cpv_descriptions = extract_cpvs(record)
        reference_number = record.get('referenceNumber') or record.get('adam') or record.get('ADAM')
        organization_key, organization_name = _organization(record)
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
            'total_cost_without_vat': _first_value(record.get('totalCostWithoutVAT'), record.get('budget'), record.get('costWithoutVAT')),
            'total_cost_with_vat': record.get('totalCostWithVAT'),
            'contract_type': _kv_value(_first_value(record.get('contractType'), record.get('contractTypes'))),
            'procedure_type': _kv_value(_first_value(record.get('procedureType'), record.get('typeOfProcedure'), record.get('awardProcedure'))),
            'cpv_codes': cpv_codes,
            'cpv_descriptions': cpv_descriptions,
            'url': public_url,
            'attachment_url': attachment_url,
            'raw': record,
            'cancelled': bool(record.get('cancelled', False)),
        }

    def normalize_notice(self, notice: Dict[str, Any]) -> Dict[str, Any]:
        return self.normalize_record('notice', notice)
