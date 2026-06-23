from __future__ import annotations

import re
import secrets
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
import httpx
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy import String, cast, func, or_, text
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.db import get_db, init_db
from app.jobs.ingest import run_ingest, score_and_store
from app.models import ClientProfile, DiavgeiaDecision, SystemEvent, Tender, TenderScore
from app.services.activity import log_event
from app.services.ai import AIService
from app.services.cpv_catalog import cpv_by_codes, cpv_categories, cpv_category_suggestions, cpv_search, cpv_prefix_for, cpv_prefixes_for_codes, cpv_tree_rows, cpv_tree_children, cpv_record, expand_cpv_codes_for_ingest, is_valid_cpv_code, cpv_covered_by_selected_parent_codes, cpv_catalog_size, cpv_ancestor_codes
from app.services.geography import any_region_match, preferred_region_matches, preferred_region_match_details, tender_region_text, expand_region_terms, nuts_options_grouped, selected_region_labels
from app.services.khmdhs_client import CONTRACT_TYPES, FRIENDLY_OPERATION_CONTEXT, KIMDIS_VIEWS, OPERATION_TYPES, KhmdhsClient, build_search_body, infer_resource_from_reference_number
from app.services.pdf import fetch_and_extract_pdf_text
from app.services.repository import upsert_tender
from app.services.timezone import format_local_date, format_local_datetime, format_kimdis_publication_datetime, now_local, now_utc, today_local
from app.services.text_normalizer import display_text, looks_like_replacement_garbage
from app.services.rescore import rescore_existing_tenders
from app.services.workflow import WORKFLOW_STATUSES, normalize_workflow_status, workflow_status_class, workflow_status_filter_values, workflow_status_label
from app.services.date_inputs import normalize_date_input
from app.services.diavgeia_enrichment import DiavgeiaClientError, find_and_store_related_diavgeia_decisions
from app.services.reports import (
    ReportFilters,
    make_csv_response,
    make_jsonl_response,
    make_markdown_response,
    make_pdf_response,
    profile_to_markdown,
    query_report_scores,
    report_to_markdown,
    report_summary,
    report_scope_label,
    report_period_label,
)

app = FastAPI(title='AI Tender Assistant', version='0.10.5')
templates = Jinja2Templates(directory='app/templates')
security = HTTPBasic(auto_error=False)

DEADLINE_FILTERS = {
    'all': 'Όλοι',
    'active': 'Ενεργά ή άγνωστη προθεσμία',
    'expires_3': 'Λήγουν σε 3 ημέρες',
    'expires_7': 'Λήγουν σε 7 ημέρες',
    'expired': 'Έχουν λήξει',
    'unknown': 'Άγνωστη προθεσμία',
}


@app.on_event('startup')
def startup() -> None:
    init_db()


def require_auth(credentials: Annotated[Optional[HTTPBasicCredentials], Depends(security)] = None) -> None:
    settings = get_settings()
    if not settings.admin_username and not settings.admin_password:
        return
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Authentication required',
            headers={'WWW-Authenticate': 'Basic'},
        )
    username_ok = secrets.compare_digest(credentials.username or '', settings.admin_username or '')
    password_ok = secrets.compare_digest(credentials.password or '', settings.admin_password or '')
    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Invalid credentials',
            headers={'WWW-Authenticate': 'Basic'},
        )


AuthDep = Depends(require_auth)
DbDep = Depends(get_db)


def _parse_int(value: str | int | None) -> Optional[int]:
    if value in (None, '', '0', 0):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_float(value: str | float | None) -> Optional[float]:
    if value in (None, ''):
        return None
    try:
        return float(str(value).replace(',', '.'))
    except ValueError:
        return None


def _split_lines(value: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """Normalize textarea/comma input or repeated checkbox form values into a clean list.

    Checkboxes/multi-select fields arrive from FastAPI as list[str]. Textareas arrive as str.
    The previous implementation only accepted str and crashed when multiple NUTS regions
    were checked in the profile form.
    """
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        cleaned: list[str] = []
        for item in value:
            if item is None:
                continue
            # Allow values that accidentally contain separators too.
            cleaned.extend(_split_lines(str(item)))
        # Preserve order and remove duplicates.
        seen: set[str] = set()
        unique: list[str] = []
        for item in cleaned:
            if item not in seen:
                seen.add(item)
                unique.append(item)
        return unique
    parts = re.split(r'[\n,;]+', str(value))
    return [part.strip() for part in parts if part.strip()]


def _list_to_text(values: list[str] | None) -> str:
    return '\n'.join(values or [])


def _safe_list(value) -> list:
    """Return a list for JSON/list fields that may be NULL/legacy strings."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return list(value)
    if isinstance(value, str):
        return [value] if value.strip() else []
    return []


def _safe_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _slugify(value: str) -> str:
    text = (value or '').strip().lower()
    text = re.sub(r'[^a-z0-9]+', '_', text)
    text = re.sub(r'_+', '_', text).strip('_')
    return text or f'profile_{secrets.token_hex(3)}'


def _today_iso() -> str:
    return today_local().isoformat()


def _days_ago_iso(days: int) -> str:
    return (today_local() - timedelta(days=days)).isoformat()




def _search_values_to_list(value: str | None) -> list[str]:
    return _split_lines(value)


def _search_cpv_values_for_kimdis(value: str | None) -> list[str]:
    return expand_cpv_codes_for_ingest(_search_values_to_list(value))


def _safe_int(value: str | int | None, default: int = 1, minimum: int = 1, maximum: int = 5) -> int:
    try:
        number = int(value or default)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _resource_label(resource: str) -> str:
    return OPERATION_TYPES.get(resource, {}).get('label', resource)


def source_name(tender_or_source: Tender | str | None) -> str:
    source = tender_or_source.source if hasattr(tender_or_source, 'source') else (tender_or_source or '')
    source = str(source)
    if source.startswith('khmdhs'):
        return 'ΚΗΜΔΗΣ'
    if source.startswith('diavgeia'):
        return 'Διαύγεια'
    return source or 'Άγνωστη πηγή'


def source_reference_label(tender_or_source: Tender | str | None) -> str:
    source = tender_or_source.source if hasattr(tender_or_source, 'source') else (tender_or_source or '')
    return 'ΑΔΑ' if str(source).startswith('diavgeia') else 'ΑΔΑΜ'


def _build_dashboard_url(
    min_score: int = 0,
    profile_id: str | int | None = '',
    deadline_filter: str = 'active',
    user_status: str = 'all',
    new_from_last_ingest: str = '',
    q: str = '',
) -> str:
    return (
        f'/?min_score={min_score}'
        f'&profile_id={profile_id or 0}'
        f'&deadline_filter={deadline_filter}'
        f'&user_status={user_status}'
        f'&new_from_last_ingest={new_from_last_ingest}'
        f'&q={q}'
    )


def deadline_badge(tender: Tender) -> dict[str, str]:
    if not tender.final_submission_date:
        return {'label': 'Άγνωστη προθεσμία', 'class': 'deadline-unknown'}
    now = now_utc()
    deadline = tender.final_submission_date
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    delta = deadline - now
    if delta.total_seconds() < 0:
        return {'label': 'Έληξε', 'class': 'deadline-expired'}
    days = delta.days
    if days == 0:
        return {'label': 'Λήγει σήμερα', 'class': 'deadline-soon'}
    if days <= 3:
        return {'label': f'Λήγει σε {days} ημέρες', 'class': 'deadline-soon'}
    return {'label': f'Λήγει σε {days} ημέρες', 'class': 'deadline-active'}



def date_info_for_tender(tender: Tender) -> dict[str, object]:
    """Human-friendly explanation of the date fields we have for a KIMDIS record."""
    return {
        'published': tender.published_date,
        'submission': tender.submission_date,
        'final_submission': tender.final_submission_date,
        'primary': tender.published_date or tender.submission_date,
        'primary_label': 'Δημοσίευση στο ΚΗΜΔΗΣ' if tender.published_date else ('Καταχώριση/υποβολή στο ΚΗΜΔΗΣ' if tender.submission_date else 'Ημερομηνία ΚΗΜΔΗΣ'),
        'has_separate_start': False,
        'start_note': 'Δεν δίνεται ξεχωριστή ημερομηνία έναρξης υποβολής στα αποθηκευμένα στοιχεία. Για συμμετοχή ελέγξτε το επίσημο PDF.',
    }

def source_resource(tender_or_source: Tender | str | None) -> str:
    source = tender_or_source.source if hasattr(tender_or_source, 'source') else (tender_or_source or '')
    mapping = {
        'khmdhs_notice': 'notice',
        'khmdhs_request': 'request',
        'khmdhs_auction': 'auction',
        'khmdhs_contract': 'contract',
        'khmdhs_payment': 'payment',
    }
    return mapping.get(str(source), 'notice')


def operation_context_for_tender(tender: Tender) -> dict[str, str]:
    return FRIENDLY_OPERATION_CONTEXT.get(source_resource(tender), FRIENDLY_OPERATION_CONTEXT['notice'])


def data_quality_badges(tender: Tender) -> list[dict[str, str]]:
    badges: list[dict[str, str]] = []
    if tender.attachment_url:
        badges.append({'label': 'PDF διαθέσιμο', 'class': 'deadline-active'})
        if tender.pdf_text and len(tender.pdf_text.strip()) > 300:
            badges.append({'label': 'Έχει γίνει ανάλυση PDF', 'class': 'deadline-active'})
        else:
            badges.append({'label': 'Δεν έχει γίνει ανάλυση PDF', 'class': 'deadline-unknown'})
    else:
        badges.append({'label': 'Χωρίς PDF link', 'class': 'deadline-unknown'})
    if tender.cpv_codes:
        badges.append({'label': f'{len(tender.cpv_codes)} CPV', 'class': 'deadline-active'})
    else:
        badges.append({'label': 'Χωρίς CPV', 'class': 'deadline-unknown'})
    if tender.final_submission_date:
        badges.append({'label': 'Έχει προθεσμία', 'class': 'deadline-active'})
    else:
        badges.append({'label': 'Άγνωστη προθεσμία', 'class': 'deadline-unknown'})
    if tender.cancelled:
        badges.append({'label': 'Ματαίωση/ακύρωση', 'class': 'deadline-expired'})
    return badges


def recommended_action_text(score: TenderScore) -> dict[str, str]:
    tender = score.tender
    resource = source_resource(tender)
    dl = deadline_badge(tender)
    if tender.cancelled:
        return {'label': 'Μη ενεργή πράξη', 'text': 'Η πράξη εμφανίζεται ματαιωμένη/ακυρωμένη. Χρήσιμη μόνο για ιστορικό έλεγχο.'}
    if resource != 'notice':
        return {'label': 'Ενημερωτικό / market intelligence', 'text': operation_context_for_tender(tender).get('usage', '')}
    if dl['class'] == 'deadline-expired':
        return {'label': 'Έχει λήξει', 'text': 'Δεν είναι άμεση ευκαιρία συμμετοχής. Κρατήστε το για ιστορικό/ανάλυση αγοράς.'}
    if score.score >= 75:
        return {'label': 'Υψηλή προτεραιότητα', 'text': 'Ανοίξτε άμεσα το PDF και ελέγξτε δικαιολογητικά, προθεσμία και δυνατότητα συμμετοχής.'}
    if score.score >= 55:
        return {'label': 'Χρειάζεται έλεγχος', 'text': 'Υπάρχει σχετικότητα με το προφίλ, αλλά θέλει ανθρώπινο έλεγχο πριν αποφασίσετε.'}
    return {'label': 'Χαμηλή προτεραιότητα', 'text': 'Κρατήστε το ως πιθανό αποτέλεσμα, αλλά δεν φαίνεται άμεσα δυνατό match.'}


def build_profile_summary(profile: ClientProfile | None) -> dict[str, object]:
    if profile is None:
        return {}
    cpv_entries = cpv_by_codes(profile.cpv_codes or [])
    return {
        'cpv_entries': cpv_entries,
        'cpv_known': len(cpv_entries),
        'cpv_total': len(profile.cpv_codes or []),
        'keywords': profile.keywords or [],
        'negative_keywords': profile.negative_keywords or [],
        'has_budget': profile.min_budget is not None or profile.max_budget is not None,
    }


def _profile_form_context(
    request: Request,
    profile: ClientProfile,
    mode: str,
    error: str | None = None,
    cpv_q: str = '',
    cpv_category: str = '',
) -> dict[str, object]:
    profile_codes = list(profile.cpv_codes or [])
    known_entries = cpv_by_codes(profile_codes)
    known_codes = {entry.code for entry in known_entries}
    unknown_codes = [code for code in profile_codes if code not in known_codes]
    cpv_results = cpv_search(cpv_q, limit=80, category=cpv_category) if (cpv_q or cpv_category) else []
    return {
        'request': request,
        'profile': profile,
        'mode': mode,
        'error': error,
        'cpv_suggestions': cpv_category_suggestions(),
        'cpv_categories': cpv_categories(),
        'cpv_q': cpv_q,
        'cpv_category': cpv_category,
        'cpv_results': cpv_results,
        # The full CPV tree is loaded lazily through /api/cpv/children.
        # Rendering ~9.5k rows inside the profile form makes the browser freeze.
        'cpv_tree_rows': [],
        'profile_cpv_codes': set(profile_codes),
        'profile_cpv_covered_codes': set(),
        'cpv_catalog_size': cpv_catalog_size(),
        'profile_cpv_entries': known_entries,
        'profile_unknown_cpv_codes': unknown_codes,
        'nuts_options_grouped': nuts_options_grouped(),
        'selected_region_labels': selected_region_labels(profile.preferred_regions or []),
    }


def dashboard_summary(db: Session, selected_profile_id: int | None = None) -> dict[str, object]:
    now = now_utc()
    threshold = get_settings().match_threshold
    base = db.query(TenderScore).join(Tender)
    if selected_profile_id:
        base = base.filter(TenderScore.profile_id == selected_profile_id)
    total_scores = base.count()
    visible_base = base.filter(~TenderScore.user_status.in_(workflow_status_filter_values('not_relevant')))
    active_clause = or_(Tender.final_submission_date.is_(None), Tender.final_submission_date >= now)

    # Client-facing dashboard counts should match the default report view:
    # actionable/open items only, not expired records that remain in the database.
    db_matches = visible_base.filter(TenderScore.score >= threshold).count()
    db_high = visible_base.filter(TenderScore.score >= 75).count()
    actionable_matches = visible_base.filter(TenderScore.score >= threshold, active_clause).count()
    actionable_high = visible_base.filter(TenderScore.score >= 75, active_clause).count()
    active = visible_base.filter(active_clause).count()
    soon = visible_base.filter(
        TenderScore.score >= threshold,
        Tender.final_submission_date >= now,
        Tender.final_submission_date <= now + timedelta(days=7),
    ).count()
    saved = base.filter(TenderScore.user_status.in_(workflow_status_filter_values('saved'))).count()
    reviewing = base.filter(TenderScore.user_status.in_(workflow_status_filter_values('reviewing'))).count()
    not_relevant = base.filter(TenderScore.user_status.in_(workflow_status_filter_values('not_relevant'))).count()
    pending_items = base.filter(TenderScore.user_status.in_(workflow_status_filter_values('new'))).count()
    latest_new = visible_base.filter(
        TenderScore.is_new_in_latest_ingest.is_(True),
        TenderScore.score >= threshold,
        active_clause,
    ).count()
    opportunities = visible_base.filter(Tender.source == 'khmdhs_notice', active_clause).count()
    last_event = db.query(SystemEvent).order_by(SystemEvent.created_at.desc()).first()
    last_ingest = latest_system_event(db, 'ingest')
    last_rescore = latest_system_event(db, 'rescore')
    return {
        'total_scores': total_scores,
        'matches': actionable_matches,
        'high': actionable_high,
        'db_matches': db_matches,
        'db_high': db_high,
        'expired_matches': max(db_matches - actionable_matches, 0),
        'active': active,
        'soon': soon,
        'saved': saved,
        'interested': saved,
        'reviewing': reviewing,
        'not_relevant': not_relevant,
        'new_items': latest_new,
        'pending_items': pending_items,
        'opportunities': opportunities,
        'match_threshold': threshold,
        'last_event': last_event,
        'last_ingest': last_ingest,
        'last_ingest_payload': _payload(last_ingest),
        'last_rescore': last_rescore,
    }



def latest_system_event(db: Session, event_type: str) -> SystemEvent | None:
    return (
        db.query(SystemEvent)
        .filter(SystemEvent.event_type == event_type)
        .order_by(SystemEvent.created_at.desc())
        .first()
    )


def _payload(event: SystemEvent | None) -> dict:
    return event.payload if event is not None and isinstance(event.payload, dict) else {}


def database_usage_summary(db: Session) -> dict[str, object]:
    """Small, read-only operational summary for the local maintenance page."""
    settings = get_settings()
    latest_ingest = latest_system_event(db, 'ingest')
    latest_rescore = latest_system_event(db, 'rescore')
    latest_ingest_payload = _payload(latest_ingest)

    db_size = 'Δεν είναι διαθέσιμο'
    try:
        if not settings.database_url.startswith('sqlite'):
            db_size = str(db.execute(text("select pg_size_pretty(pg_database_size(current_database()))")).scalar() or db_size)
    except Exception:
        db_size = 'Δεν είναι διαθέσιμο'

    source_counts = [
        {'source': source_name(source), 'count': count}
        for source, count in db.query(Tender.source, func.count(Tender.id)).group_by(Tender.source).order_by(Tender.source.asc()).all()
    ]
    status_counts = [
        {'status': workflow_status_label(status), 'count': count}
        for status, count in db.query(TenderScore.user_status, func.count(TenderScore.id)).group_by(TenderScore.user_status).order_by(TenderScore.user_status.asc()).all()
    ]
    now = now_utc()
    active_or_unknown = db.query(Tender).filter(or_(Tender.final_submission_date.is_(None), Tender.final_submission_date >= now)).count()
    expired = db.query(Tender).filter(Tender.final_submission_date < now).count()
    pdf_text_count = db.query(Tender).filter(Tender.pdf_text.isnot(None), Tender.pdf_text != '').count()

    return {
        'db_size': db_size,
        'profiles': db.query(ClientProfile).count(),
        'active_profiles': db.query(ClientProfile).filter(ClientProfile.is_active.is_(True)).count(),
        'tenders': db.query(Tender).count(),
        'scores': db.query(TenderScore).count(),
        'active_or_unknown': active_or_unknown,
        'expired': expired,
        'latest_new': db.query(TenderScore).filter(TenderScore.is_new_in_latest_ingest.is_(True)).count(),
        'pdf_text_count': pdf_text_count,
        'latest_ingest': latest_ingest,
        'latest_ingest_payload': latest_ingest_payload,
        'latest_rescore': latest_rescore,
        'source_counts': source_counts,
        'status_counts': status_counts,
        'schedule': f'{settings.schedule_hour:02d}:{settings.schedule_minute:02d} {settings.app_timezone}',
        'ingest_days_back': settings.ingest_days_back,
        'khmdhs_max_pages': settings.khmdhs_max_pages,
        'khmdhs_page_delay': settings.khmdhs_page_delay_seconds,
        'match_threshold': settings.match_threshold,
    }

def normalize_chain_items(chain_payload: object) -> list[dict[str, str]]:
    if not chain_payload:
        return []
    if isinstance(chain_payload, dict):
        # Common shapes: {'content': [...]}, {'items': [...]}, or object with lists per stage.
        for key in ('content', 'items', 'records', 'data'):
            if isinstance(chain_payload.get(key), list):
                chain_payload = chain_payload[key]
                break
        else:
            items = []
            for key, value in chain_payload.items():
                if isinstance(value, list):
                    for row in value:
                        if isinstance(row, dict):
                            row = {**row, '_stage': key}
                        items.append(row)
            chain_payload = items if items else [chain_payload]
    if not isinstance(chain_payload, list):
        return []
    out: list[dict[str, str]] = []
    for row in chain_payload[:30]:
        if not isinstance(row, dict):
            continue
        ref = str(row.get('referenceNumber') or row.get('adam') or row.get('ADAM') or row.get('refNo') or '')
        title = str(row.get('title') or row.get('subject') or row.get('description') or '')
        stage = str(row.get('_stage') or row.get('type') or row.get('actType') or row.get('documentType') or row.get('resource') or '')
        date_value = str(row.get('submissionDate') or row.get('publishedDate') or row.get('signedDate') or row.get('date') or '')
        out.append({'reference': ref, 'title': title, 'stage': stage, 'date': date_value})
    return out


templates.env.globals['deadline_badge'] = deadline_badge
templates.env.globals['workflow_statuses'] = WORKFLOW_STATUSES
templates.env.globals['workflow_status_label'] = workflow_status_label
templates.env.globals['workflow_status_class'] = workflow_status_class
templates.env.globals['normalize_workflow_status'] = normalize_workflow_status
templates.env.globals['deadline_filters'] = DEADLINE_FILTERS
templates.env.globals['operation_types'] = OPERATION_TYPES
templates.env.globals['friendly_operation_context'] = FRIENDLY_OPERATION_CONTEXT
templates.env.globals['kimdis_views'] = KIMDIS_VIEWS
templates.env.globals['contract_types'] = CONTRACT_TYPES
templates.env.globals['resource_label'] = _resource_label
templates.env.globals['source_name'] = source_name
templates.env.globals['source_reference_label'] = source_reference_label
templates.env.globals['source_resource'] = source_resource
templates.env.globals['operation_context_for_tender'] = operation_context_for_tender
templates.env.globals['data_quality_badges'] = data_quality_badges
templates.env.globals['date_info_for_tender'] = date_info_for_tender
templates.env.globals['recommended_action_text'] = recommended_action_text
templates.env.globals['tender_region_text'] = tender_region_text
templates.env.globals['preferred_region_matches'] = preferred_region_matches
templates.env.filters['list_to_text'] = _list_to_text
templates.env.filters['local_dt'] = format_local_datetime
templates.env.filters['local_date'] = format_local_date
templates.env.filters['kimdis_pub_dt'] = format_kimdis_publication_datetime
templates.env.filters['display_text'] = display_text
templates.env.filters['safe_list'] = _safe_list
templates.env.filters['safe_dict'] = _safe_dict
templates.env.globals['text_has_encoding_issue'] = looks_like_replacement_garbage
templates.env.globals['app_timezone'] = lambda: get_settings().app_timezone
templates.env.globals['now_local'] = now_local


@app.get('/health')
def health() -> dict:
    return {'status': 'ok'}


@app.get('/', response_class=HTMLResponse, dependencies=[AuthDep])
def dashboard(
    request: Request,
    db: Session = DbDep,
    min_score: Optional[int] = None,
    profile_id: str = '',
    deadline_filter: str = 'active',
    user_status: str = 'all',
    new_from_last_ingest: str = '',
    q: str = '',
    region: str = '',
    rescore_done: str = '',
    ingest_done: str = '',
    ingest_warning: str = '',
    profile_warning: str = '',
) -> HTMLResponse:
    settings = get_settings()
    profiles = db.query(ClientProfile).order_by(ClientProfile.is_active.desc(), ClientProfile.name.asc()).all()
    active_profiles_count = sum(1 for p in profiles if p.is_active)
    selected_profile_id = _parse_int(profile_id)
    # Profile-first dashboard: when no profile is explicitly selected, show the first active interest/profile.
    if selected_profile_id is None and profile_id in ('', None) and profiles:
        first_active = next((p for p in profiles if p.is_active), profiles[0])
        selected_profile_id = first_active.id
    selected_profile = db.query(ClientProfile).filter(ClientProfile.id == selected_profile_id).one_or_none() if selected_profile_id else None
    dashboard_mode_all = selected_profile_id is None
    normalized_filter_status = normalize_workflow_status(user_status) if user_status and user_status != 'all' else 'all'
    status_keeps_items_visible = normalized_filter_status in ('saved', 'reviewing', 'not_relevant')
    if min_score is None:
        # Saved/reviewing/rejected lists are workflow/bookmark lists, so score must not hide them.
        # The "Νέο" view is still part of the normal discovery flow, so it keeps the score filter.
        min_score = 0 if status_keeps_items_visible else int(settings.match_threshold)
    if status_keeps_items_visible and deadline_filter == 'active':
        # When the user asks for a manual workflow list, do not hide it just because
        # the deadline passed or is unknown. Explicit deadline filters still apply.
        deadline_filter = 'all'
    summary = dashboard_summary(db, selected_profile_id)
    profile_summary = build_profile_summary(selected_profile)
    query = (
        db.query(TenderScore)
        .options(joinedload(TenderScore.tender), joinedload(TenderScore.profile))
        .join(Tender)
    )
    if user_status == 'all':
        # Default flow should not keep showing items the user explicitly marked as irrelevant.
        query = query.filter(TenderScore.score >= min_score)
        query = query.filter(~TenderScore.user_status.in_(workflow_status_filter_values('not_relevant')))
    elif normalized_filter_status == 'new':
        # "Νέο" is not a bookmark/status archive; keep the same score threshold as the normal flow.
        query = query.filter(TenderScore.score >= min_score)
    if selected_profile_id:
        query = query.filter(TenderScore.profile_id == selected_profile_id)
    if new_from_last_ingest:
        query = query.filter(TenderScore.is_new_in_latest_ingest.is_(True))

    now = now_utc()
    if deadline_filter == 'active':
        query = query.filter(or_(Tender.final_submission_date.is_(None), Tender.final_submission_date >= now))
    elif deadline_filter == 'expires_3':
        query = query.filter(Tender.final_submission_date >= now, Tender.final_submission_date <= now + timedelta(days=3))
    elif deadline_filter == 'expires_7':
        query = query.filter(Tender.final_submission_date >= now, Tender.final_submission_date <= now + timedelta(days=7))
    elif deadline_filter == 'expired':
        query = query.filter(Tender.final_submission_date < now)
    elif deadline_filter == 'unknown':
        query = query.filter(Tender.final_submission_date.is_(None))

    if user_status and user_status != 'all':
        query = query.filter(TenderScore.user_status.in_(workflow_status_filter_values(user_status)))

    q_clean = q.strip()
    if q_clean:
        pattern = f'%{q_clean}%'
        query = query.filter(or_(Tender.title.ilike(pattern), Tender.organization_name.ilike(pattern), Tender.reference_number.ilike(pattern)))

    region_terms = expand_region_terms(region)
    if region_terms:
        region_clauses = []
        for term in region_terms:
            pattern = f'%{term}%'
            region_clauses.extend([
                Tender.organization_name.ilike(pattern),
                Tender.title.ilike(pattern),
                cast(Tender.raw, String).ilike(pattern),
            ])
        query = query.filter(or_(*region_clauses))

    scores = query.order_by(TenderScore.score.desc(), Tender.final_submission_date.asc().nullslast()).limit(300).all()
    return templates.TemplateResponse(
        'dashboard.html',
        {
            'request': request,
            'scores': scores,
            'profiles': profiles,
            'settings': settings,
            'min_score': min_score,
            'profile_id': selected_profile_id,
            'deadline_filter': deadline_filter,
            'user_status': user_status,
            'new_from_last_ingest': new_from_last_ingest,
            'q': q,
            'region': region,
            'summary': summary,
            'selected_profile': selected_profile,
            'dashboard_mode_all': dashboard_mode_all,
            'profile_summary': profile_summary,
            'nuts_options_grouped': nuts_options_grouped(),
            'rescore_done': rescore_done,
            'ingest_done': ingest_done,
            'ingest_warning': ingest_warning,
            'profile_warning': profile_warning,
            'active_profiles_count': active_profiles_count,
        },
    )


@app.post('/ingest/run', dependencies=[AuthDep])
def run_ingest_now(
    days: int = Form(3),
    profile_id: str = Form('0'),
    return_to: str = Form('/'),
) -> RedirectResponse:
    selected_profile_id = _parse_int(profile_id)
    # Manual ingest from the dashboard is profile-oriented: it uses only the selected profile.
    # The scheduled worker still calls run_ingest() without profile_id, so it covers all active profiles.
    result = run_ingest(days_back=days, send_email=False, profile_id=selected_profile_id)
    separator = '&' if '?' in return_to else '?'
    warnings = result.get('warnings') or []
    warning_q = f'&ingest_warning={warnings[0]}' if warnings else ''
    return RedirectResponse(url=f'{return_to}{separator}ingest_done={days}{warning_q}', status_code=status.HTTP_303_SEE_OTHER)


@app.post('/rescore/run', dependencies=[AuthDep])
def run_rescore_now(
    db: Session = DbDep,
    profile_id: str = Form('0'),
    return_to: str = Form('/'),
) -> RedirectResponse:
    selected_profile_id = _parse_int(profile_id)
    result = rescore_existing_tenders(db, profile_id=selected_profile_id)
    log_event(
        db,
        'rescore',
        'Ολοκληρώθηκε ανανέωση σχετικότητας',
        f"Ενημερώθηκαν {result['scores_updated']} αξιολογήσεις για {result['tenders']} αποθηκευμένες πράξεις και {result['profiles']} προφίλ.",
        {'profile_id': selected_profile_id, **result},
    )
    db.commit()
    separator = '&' if '?' in return_to else '?'
    return RedirectResponse(url=f'{return_to}{separator}rescore_done={result["scores_updated"]}', status_code=status.HTTP_303_SEE_OTHER)


@app.get('/kimdis', response_class=HTMLResponse, dependencies=[AuthDep])
def kimdis_search(
    request: Request,
    db: Session = DbDep,
    view: str = 'opportunities',
    resource: str = 'notice',
    title: str = '',
    reference_number: str = '',
    cpv_items: str = '',
    organizations: str = '',
    organization_contains: str = '',
    contract_type: str = '',
    procedure_type: str = '',
    date_from: str = '',
    date_to: str = '',
    total_cost_from: str = '',
    total_cost_to: str = '',
    final_date_from: str = '',
    final_date_to: str = '',
    active_only: str = '',
    max_pages: int = 1,
    search: str = '',
    profile_id: str = '',
):
    date_from = normalize_date_input(date_from)
    date_to = normalize_date_input(date_to)
    final_date_from = normalize_date_input(final_date_from)
    final_date_to = normalize_date_input(final_date_to)

    profiles = db.query(ClientProfile).order_by(ClientProfile.is_active.desc(), ClientProfile.name.asc()).all()
    active_profiles = [profile for profile in profiles if profile.is_active]
    selected_profile_id = _parse_int(profile_id)
    if selected_profile_id is None and active_profiles:
        selected_profile_id = active_profiles[0].id
    selected_profile = next((profile for profile in profiles if profile.id == selected_profile_id), None) if selected_profile_id else None

    # Avoid treating navigation parameters such as ?view=opportunities as a real
    # KIMDIS search. The user explicitly submits the form with search=1, while
    # direct URLs with actual filters still work for sharing/debugging.
    has_real_filter = any([
        title.strip(), reference_number.strip(), cpv_items.strip(), organizations.strip(),
        organization_contains.strip(), contract_type.strip(), procedure_type.strip(),
        date_from.strip(), date_to.strip(), total_cost_from.strip(), total_cost_to.strip(),
        final_date_from.strip(), final_date_to.strip(), active_only == 'on',
    ])
    has_query = search == '1' or has_real_filter
    error = None
    warnings: list[str] = []
    results = []
    searched = False
    if view not in KIMDIS_VIEWS:
        view = 'opportunities'
    if not has_query:
        # The ad hoc KIMDIS search starts empty. If no dates are sent, the official
        # API applies its own registration-date default window, while finalDate is
        # not considered unless the user explicitly asks for active notices.
        date_from = date_from or ''
        date_to = date_to or ''
        final_date_from = final_date_from or ''
        final_date_to = final_date_to or ''
        active_only = active_only or ''
    else:
        searched = True
        client = KhmdhsClient()
        max_pages_safe = _safe_int(max_pages)
        if reference_number.strip() and any([date_from.strip(), date_to.strip(), final_date_from.strip(), final_date_to.strip(), active_only == 'on']):
            warnings.append('Επειδή δώσατε ΑΔΑΜ, αγνοήθηκαν τα φίλτρα ημερομηνίας/ενεργών για να μη χαθεί ακριβής αναζήτηση.')
            date_from = ''
            date_to = ''
            final_date_from = ''
            final_date_to = ''
            active_only = ''
        if view == 'advanced':
            resources = list(OPERATION_TYPES.keys()) if resource == 'all' else [resource]
            if resource != 'all' and resource not in OPERATION_TYPES:
                resources = ['notice']
        else:
            resources = list(KIMDIS_VIEWS[view].get('resources') or ['notice'])
            resource = resources[0] if len(resources) == 1 else 'all'
        # Αν ο χρήστης δώσει ΑΔΑΜ, περιορίζουμε την αναζήτηση στο κατάλληλο endpoint.
        # Διαφορετικά π.χ. 26PROC... σε request/payment μπορεί να γυρίσει 400 λόγω schema validation.
        inferred_resource = infer_resource_from_reference_number(reference_number)
        if inferred_resource and inferred_resource in OPERATION_TYPES:
            if inferred_resource in resources:
                resources = [inferred_resource]
                resource = inferred_resource
            else:
                resources = []
                warnings.append('Ο ΑΔΑΜ φαίνεται να ανήκει σε διαφορετικό είδος πράξης από το επιλεγμένο view. Αλλάξτε view ή χρησιμοποιήστε Advanced αναζήτηση.')
        if active_only == 'on' and 'notice' in resources and not final_date_from:
            final_date_from = today_local().isoformat()
        resource_errors = []
        for res in resources:
            body = build_search_body(
                resource=res,
                title=title,
                reference_number=reference_number,
                cpv_items=_search_cpv_values_for_kimdis(cpv_items),
                organizations=_search_values_to_list(organizations),
                contract_type=contract_type,
                procedure_type=procedure_type,
                date_from=date_from,
                date_to=date_to,
                total_cost_from=total_cost_from,
                total_cost_to=total_cost_to,
                final_date_from=final_date_from,
                final_date_to=final_date_to,
                include_final_dates=(res == 'notice'),
            )
            # User friendly mode: final dates only make sense for notices.
            if res != 'notice' and (final_date_from or final_date_to or active_only == 'on'):
                warnings.append(f"Το φίλτρο καταληκτικής ημερομηνίας αγνοήθηκε για {OPERATION_TYPES.get(res, {}).get('label', res)}.")
            try:
                raw_records = client.search_resource(res, body, max_pages=max_pages_safe)
            except Exception as exc:
                resource_errors.append(f"{OPERATION_TYPES.get(res, {}).get('label', res)}: {exc}")
                continue
            for raw in raw_records:
                normalized = client.normalize_record(res, raw)
                if organization_contains.strip():
                    org = (normalized.get('organization_name') or '').lower()
                    if organization_contains.strip().lower() not in org:
                        continue
                existing = None
                if normalized.get('source_reference'):
                    existing = (
                        db.query(Tender)
                        .filter(Tender.source == normalized['source'], Tender.source_reference == str(normalized['source_reference']))
                        .one_or_none()
                    )
                saved_for_selected_profile = False
                if existing is not None and selected_profile_id is not None:
                    saved_for_selected_profile = (
                        db.query(TenderScore.id)
                        .filter(TenderScore.tender_id == existing.id, TenderScore.profile_id == selected_profile_id)
                        .first()
                        is not None
                    )
                # Lightweight explanation without storing/scoring.
                context = FRIENDLY_OPERATION_CONTEXT.get(res, FRIENDLY_OPERATION_CONTEXT['notice'])
                results.append({
                    'resource': res,
                    'raw': raw,
                    'normalized': normalized,
                    'saved_id': existing.id if existing else None,
                    'saved_for_selected_profile': saved_for_selected_profile,
                    'context': context,
                })
        results = results[:300]
        if resource_errors:
            error = 'Μερικά είδη ΚΗΜΔΗΣ δεν επέστρεψαν αποτελέσματα με αυτά τα φίλτρα: ' + ' | '.join(resource_errors[:3])
    return templates.TemplateResponse(
        'kimdis_search.html',
        {
            'request': request,
            'results': results,
            'searched': searched,
            'error': error,
            'warnings': list(dict.fromkeys(warnings))[:4],
            'view': view,
            'resource': resource,
            'title': title,
            'reference_number': reference_number,
            'cpv_items': cpv_items,
            'organizations': organizations,
            'organization_contains': organization_contains,
            'contract_type': contract_type,
            'procedure_type': procedure_type,
            'date_from': date_from,
            'date_to': date_to,
            'total_cost_from': total_cost_from,
            'total_cost_to': total_cost_to,
            'final_date_from': final_date_from,
            'final_date_to': final_date_to,
            'active_only': active_only,
            'max_pages': _safe_int(max_pages),
            'search': search,
            'profiles': profiles,
            'active_profiles': active_profiles,
            'selected_profile_id': selected_profile_id,
            'selected_profile': selected_profile,
            'view_meta': KIMDIS_VIEWS.get(view, KIMDIS_VIEWS['opportunities']),
            'cpv_suggestions': cpv_category_suggestions(),
        },
    )


@app.post('/kimdis/save', dependencies=[AuthDep])
def kimdis_save(
    resource: str = Form(...),
    reference_number: str = Form(...),
    profile_id: str = Form(...),
    return_to: str = Form('/kimdis'),
    db: Session = DbDep,
) -> RedirectResponse:
    if resource not in OPERATION_TYPES or not reference_number.strip():
        raise HTTPException(status_code=400, detail='Invalid KIMDIS resource/reference')
    selected_profile_id = _parse_int(profile_id)
    if selected_profile_id is None:
        raise HTTPException(status_code=400, detail='Πρέπει να επιλέξετε προφίλ αποθήκευσης/βαθμολόγησης.')
    profile = db.query(ClientProfile).filter(ClientProfile.id == selected_profile_id, ClientProfile.is_active.is_(True)).one_or_none()
    if profile is None:
        raise HTTPException(status_code=404, detail='Το επιλεγμένο προφίλ δεν βρέθηκε ή δεν είναι ενεργό.')

    client = KhmdhsClient()
    body = build_search_body(resource=resource, reference_number=reference_number.strip(), include_final_dates=(resource == 'notice'))
    records = client.search_resource(resource, body, max_pages=1)
    raw = next((r for r in records if str(r.get('referenceNumber')) == reference_number.strip()), records[0] if records else None)
    if raw is None:
        raise HTTPException(status_code=404, detail='KIMDIS record not found')
    tender = upsert_tender(db, client.normalize_record(resource, raw))
    score = score_and_store(db, tender, profile, AIService())
    score.user_status = 'saved'
    score.status_updated_at = now_utc()
    log_event(
        db,
        event_type='kimdis_save',
        title='Αποθηκεύτηκε πράξη από Γενική Αναζήτηση ΚΗΜΔΗΣ',
        message=f"{tender.reference_number or tender.source_reference} — {tender.title[:180]} — προφίλ: {profile.name}",
        payload={
            'resource': resource,
            'tender_id': tender.id,
            'profile_id': profile.id,
            'score_id': score.id,
        },
    )
    db.commit()
    return RedirectResponse(url=f'/tenders/{tender.id}?profile_id={profile.id}', status_code=status.HTTP_303_SEE_OTHER)

@app.post('/scores/{score_id}/workflow', dependencies=[AuthDep])
def update_score_workflow(
    score_id: int,
    user_status: str = Form(...),
    user_notes: Optional[str] = Form(None),
    return_to: str = Form('/'),
    db: Session = DbDep,
) -> RedirectResponse:
    score = db.query(TenderScore).filter(TenderScore.id == score_id).one_or_none()
    if score is None:
        raise HTTPException(status_code=404, detail='Score not found')
    normalized_status = normalize_workflow_status(user_status)
    if normalized_status not in WORKFLOW_STATUSES:
        raise HTTPException(status_code=400, detail='Invalid workflow status')
    score.user_status = normalized_status
    if user_notes is not None:
        score.user_notes = user_notes.strip() or None
    score.status_updated_at = now_utc()
    db.commit()
    return RedirectResponse(url=return_to or '/', status_code=status.HTTP_303_SEE_OTHER)


@app.post('/tenders/{tender_id}/delete', dependencies=[AuthDep])
def delete_tender(
    tender_id: int,
    return_to: str = Form('/'),
    db: Session = DbDep,
) -> RedirectResponse:
    tender = db.query(Tender).filter(Tender.id == tender_id).one_or_none()
    if tender is None:
        raise HTTPException(status_code=404, detail='Tender not found')

    reference = tender.reference_number or tender.source_reference
    title = tender.title or ''
    log_event(
        db,
        event_type='tender_deleted',
        title='Διαγράφηκε διαγωνισμός από τη βάση',
        message=f'{reference} — {title[:180]}',
        payload={
            'tender_id': tender.id,
            'source': tender.source,
            'source_reference': tender.source_reference,
            'reference_number': tender.reference_number,
        },
    )
    db.delete(tender)
    db.commit()

    safe_return = return_to if return_to and return_to.startswith('/') and not return_to.startswith('//') else '/'
    return RedirectResponse(url=safe_return, status_code=status.HTTP_303_SEE_OTHER)


@app.get('/tenders/{tender_id}', response_class=HTMLResponse, dependencies=[AuthDep])
def tender_detail(request: Request, tender_id: int, db: Session = DbDep, timeline: int = 0, profile_id: str = '', diavgeia_refreshed: str = '', diavgeia_error: str = '') -> HTMLResponse:
    tender = db.query(Tender).options(joinedload(Tender.scores).joinedload(TenderScore.profile)).filter(Tender.id == tender_id).one_or_none()
    if tender is None:
        raise HTTPException(status_code=404, detail='Tender not found')
    selected_profile_id = _parse_int(profile_id)
    raw_scores = [score for score in (tender.scores or []) if score.profile is not None]
    raw_scores.sort(key=lambda score: float(score.score or 0), reverse=True)
    if selected_profile_id:
        selected_scores = [score for score in raw_scores if score.profile_id == selected_profile_id]
        other_scores = [score for score in raw_scores if score.profile_id != selected_profile_id]
        display_scores = selected_scores + other_scores
    else:
        display_scores = raw_scores
    # Legacy rows from older versions may have NULL JSON fields. Normalize them for
    # rendering so the detail page never crashes on join/iteration in templates.
    for score in display_scores:
        score.reasons = _safe_list(score.reasons)
        score.matched_cpv = _safe_list(score.matched_cpv)
        score.matched_keywords = _safe_list(score.matched_keywords)
        score.missing_requirements = _safe_list(score.missing_requirements)
        score.user_status = normalize_workflow_status(score.user_status)
    tender.cpv_codes = _safe_list(tender.cpv_codes)
    tender.cpv_descriptions = _safe_dict(tender.cpv_descriptions)
    chain_items: list[dict[str, str]] = []
    chain_error = None
    timeline_checked = bool(timeline)
    if timeline_checked and tender.reference_number:
        try:
            chain_items = normalize_chain_items(KhmdhsClient().adam_chain(tender.reference_number))
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code == 429:
                chain_error = 'Το ΚΗΜΔΗΣ έβαλε προσωρινό όριο αιτημάτων. Δοκιμάστε ξανά σε λίγα λεπτά. Η πράξη και το PDF παραμένουν διαθέσιμα.'
            else:
                chain_error = 'Δεν μπόρεσε να ανακτηθεί η πορεία της υπόθεσης από το ΚΗΜΔΗΣ αυτή τη στιγμή.'
        except Exception:
            chain_error = 'Δεν μπόρεσε να ανακτηθεί η πορεία της υπόθεσης από το ΚΗΜΔΗΣ αυτή τη στιγμή.'
    diavgeia_decisions = (
        db.query(DiavgeiaDecision)
        .filter(DiavgeiaDecision.tender_id == tender.id)
        .order_by(DiavgeiaDecision.issue_date.desc(), DiavgeiaDecision.id.desc())
        .all()
    )
    diavgeia_message = ''
    if diavgeia_refreshed not in ('', None):
        try:
            refreshed_count = int(diavgeia_refreshed)
        except (TypeError, ValueError):
            refreshed_count = 0
        if refreshed_count == 0:
            diavgeia_message = 'Δεν βρέθηκε σχετική πράξη Διαύγειας με αναζήτηση ΑΔΑΜ/κωδικού. Αυτό δεν σημαίνει ότι δεν υπάρχει διοικητικό ιστορικό· σημαίνει ότι δεν βρέθηκε ασφαλές exact match για αυτόν τον διαγωνισμό.'
        elif refreshed_count == 1:
            diavgeia_message = 'Βρέθηκε και αποθηκεύτηκε 1 σχετική πράξη Διαύγειας ως επικουρική τεκμηρίωση.'
        else:
            diavgeia_message = f'Βρέθηκαν και αποθηκεύτηκαν {refreshed_count} σχετικές πράξεις Διαύγειας ως επικουρική τεκμηρίωση.'
    elif diavgeia_error:
        if diavgeia_error == 'no_reference':
            diavgeia_message = 'Δεν υπάρχει διαθέσιμος ΑΔΑΜ/κωδικός για ασφαλή αναζήτηση στη Διαύγεια.'
        else:
            diavgeia_message = 'Δεν μπόρεσε να ολοκληρωθεί η αναζήτηση στη Διαύγεια αυτή τη στιγμή.'
    return templates.TemplateResponse(
        'tender.html',
        {
            'request': request,
            'tender': tender,
            'answer': None,
            'chain_items': chain_items,
            'chain_error': chain_error,
            'timeline_checked': timeline_checked,
            'ai_enabled': bool(get_settings().openai_api_key),
            'selected_profile_id': selected_profile_id,
            'display_scores': display_scores,
            'diavgeia_decisions': diavgeia_decisions,
            'diavgeia_message': diavgeia_message,
        },
    )


@app.post('/tenders/{tender_id}/diavgeia-refresh', dependencies=[AuthDep])
def refresh_tender_diavgeia(
    tender_id: int,
    db: Session = DbDep,
    profile_id: str = Form(''),
) -> RedirectResponse:
    tender = db.query(Tender).filter(Tender.id == tender_id).one_or_none()
    if tender is None:
        raise HTTPException(status_code=404, detail='Tender not found')
    reference = (tender.reference_number or tender.source_reference or '').strip()
    base_return = f'/tenders/{tender.id}?profile_id={profile_id or ""}'
    if not reference:
        return RedirectResponse(url=f'{base_return}&diavgeia_error=no_reference', status_code=status.HTTP_303_SEE_OTHER)
    try:
        result = find_and_store_related_diavgeia_decisions(db, tender, size=10, hydrate=True)
        log_event(
            db,
            event_type='diavgeia_enrichment',
            title='Έγινε αναζήτηση στη Διαύγεια',
            message=f'{reference} — {result.stored} σχετικές πράξεις',
            payload={
                'tender_id': tender.id,
                'reference': reference,
                'total': result.total,
                'stored': result.stored,
                'created': result.created,
                'updated': result.updated,
                'strategy': 'adam_exact',
                'scope': 'evidence_only',
                'auto_saved_fallbacks': False,
            },
        )
        db.commit()
        return RedirectResponse(url=f'{base_return}&diavgeia_refreshed={result.stored}', status_code=status.HTTP_303_SEE_OTHER)
    except DiavgeiaClientError:
        db.rollback()
        return RedirectResponse(url=f'{base_return}&diavgeia_error=api', status_code=status.HTTP_303_SEE_OTHER)


@app.post('/tenders/{tender_id}/analyze-pdf', dependencies=[AuthDep])
def analyze_tender_pdf(tender_id: int, db: Session = DbDep) -> RedirectResponse:
    tender = db.query(Tender).options(joinedload(Tender.scores).joinedload(TenderScore.profile)).filter(Tender.id == tender_id).one_or_none()
    if tender is None:
        raise HTTPException(status_code=404, detail='Tender not found')
    if not tender.attachment_url:
        raise HTTPException(status_code=400, detail='Δεν υπάρχει διαθέσιμο PDF για αυτή την πράξη.')
    tender.pdf_text = fetch_and_extract_pdf_text(tender.attachment_url)
    ai = AIService()
    profiles = [score.profile for score in tender.scores if score.profile is not None]
    if not profiles:
        profiles = db.query(ClientProfile).filter(ClientProfile.is_active.is_(True)).all()
    for profile in profiles:
        score_and_store(db, tender, profile, ai)
    log_event(
        db,
        event_type='pdf_analyzed',
        title='Έγινε ανάλυση PDF',
        message=f'{tender.reference_number or tender.source_reference} — {tender.title[:160]}',
        payload={'tender_id': tender.id, 'reference_number': tender.reference_number},
    )
    db.commit()
    return RedirectResponse(url=f'/tenders/{tender.id}', status_code=status.HTTP_303_SEE_OTHER)


@app.post('/assistant', response_class=HTMLResponse, dependencies=[AuthDep])
def assistant_answer(
    request: Request,
    tender_id: int = Form(...),
    profile_id: int = Form(...),
    question: str = Form(...),
    db: Session = DbDep,
) -> HTMLResponse:
    tender = db.query(Tender).options(joinedload(Tender.scores).joinedload(TenderScore.profile)).filter(Tender.id == tender_id).one_or_none()
    profile = db.query(ClientProfile).filter(ClientProfile.id == profile_id).one_or_none()
    if tender is None or profile is None:
        raise HTTPException(status_code=404, detail='Tender/profile not found')
    answer = AIService().answer_question(tender, profile, question)
    display_scores = [score for score in (tender.scores or []) if score.profile is not None]
    display_scores.sort(key=lambda score: float(score.score or 0), reverse=True)
    return templates.TemplateResponse('tender.html', {'request': request, 'tender': tender, 'answer': answer, 'selected_profile_id': profile_id, 'chain_items': [], 'chain_error': None, 'timeline_checked': False, 'ai_enabled': bool(get_settings().openai_api_key), 'display_scores': display_scores})


@app.get('/profiles', response_class=HTMLResponse, dependencies=[AuthDep])
def profiles_list(request: Request, db: Session = DbDep, profile_warning: str = '') -> HTMLResponse:
    profiles = db.query(ClientProfile).order_by(ClientProfile.name.asc()).all()
    active_profiles_count = sum(1 for p in profiles if p.is_active)
    return templates.TemplateResponse('profiles.html', {'request': request, 'profiles': profiles, 'profile_warning': profile_warning, 'active_profiles_count': active_profiles_count})


@app.get('/profiles/new', response_class=HTMLResponse, dependencies=[AuthDep])
def profile_new(request: Request, cpv_q: str = '', cpv_category: str = '') -> HTMLResponse:
    profile = ClientProfile(slug='', name='', description='', cpv_codes=[], cpv_prefixes=[], keywords=[], negative_keywords=[], required_certificates=[], preferred_regions=[], rss_feeds=[], is_active=True)
    return templates.TemplateResponse('profile_form.html', _profile_form_context(request, profile, 'new', cpv_q=cpv_q, cpv_category=cpv_category))


@app.get('/profiles/{profile_id}/edit', response_class=HTMLResponse, dependencies=[AuthDep])
def profile_edit(request: Request, profile_id: int, db: Session = DbDep, cpv_q: str = '', cpv_category: str = '') -> HTMLResponse:
    profile = db.query(ClientProfile).filter(ClientProfile.id == profile_id).one_or_none()
    if profile is None:
        raise HTTPException(status_code=404, detail='Profile not found')
    return templates.TemplateResponse('profile_form.html', _profile_form_context(request, profile, 'edit', cpv_q=cpv_q, cpv_category=cpv_category))


def _save_profile_from_form(
    profile: ClientProfile,
    slug: str,
    name: str,
    description: str,
    cpv_codes: str,
    cpv_prefixes: str,
    keywords: str,
    negative_keywords: str,
    required_certificates: str,
    preferred_regions: str | list[str],
    min_budget: str,
    max_budget: str,
    rss_feeds: str,
    is_active: Optional[str],
) -> None:
    profile.name = name.strip()
    profile.slug = _slugify(slug or profile.name)
    profile.description = description.strip()
    profile.cpv_codes = _split_lines(cpv_codes)
    # Οι οικογένειες CPV υπολογίζονται από τους επιλεγμένους κωδικούς.
    # Δεν βασιζόμαστε στο hidden field/JS ώστε να είναι σωστό και σε manual POST.
    profile.cpv_prefixes = cpv_prefixes_for_codes(profile.cpv_codes)
    profile.keywords = _split_lines(keywords)
    profile.negative_keywords = _split_lines(negative_keywords)
    profile.required_certificates = _split_lines(required_certificates)
    profile.preferred_regions = _split_lines(preferred_regions)
    profile.min_budget = _parse_float(min_budget)
    profile.max_budget = _parse_float(max_budget)
    profile.rss_feeds = _split_lines(rss_feeds)
    profile.is_active = is_active == 'on'


@app.post('/profiles', response_class=HTMLResponse, dependencies=[AuthDep])
def profile_create(
    request: Request,
    slug: str = Form(''),
    name: str = Form(...),
    description: str = Form(''),
    cpv_codes: str = Form(''),
    cpv_prefixes: str = Form(''),
    keywords: str = Form(''),
    negative_keywords: str = Form(''),
    required_certificates: str = Form(''),
    preferred_regions: list[str] = Form([]),
    min_budget: str = Form(''),
    max_budget: str = Form(''),
    rss_feeds: str = Form(''),
    is_active: Optional[str] = Form(None),
    db: Session = DbDep,
):
    profile = ClientProfile(slug=slug.strip(), name=name.strip())
    _save_profile_from_form(profile, slug, name, description, cpv_codes, cpv_prefixes, keywords, negative_keywords, required_certificates, preferred_regions, min_budget, max_budget, rss_feeds, is_active)
    if not profile.name:
        return templates.TemplateResponse('profile_form.html', _profile_form_context(request, profile, 'new', 'Συμπληρώστε όνομα προφίλ.'))
    if db.query(ClientProfile).filter(ClientProfile.slug == profile.slug).first():
        return templates.TemplateResponse('profile_form.html', _profile_form_context(request, profile, 'new', 'Υπάρχει ήδη profile με αυτό το slug.'))
    db.add(profile)
    log_event(db, 'profile_created', 'Δημιουργήθηκε νέο προφίλ', f'{profile.name} ({profile.slug})', {'profile_slug': profile.slug})
    db.commit()
    return RedirectResponse(url='/profiles', status_code=status.HTTP_303_SEE_OTHER)


@app.post('/profiles/{profile_id}', response_class=HTMLResponse, dependencies=[AuthDep])
def profile_update(
    request: Request,
    profile_id: int,
    slug: str = Form(''),
    name: str = Form(...),
    description: str = Form(''),
    cpv_codes: str = Form(''),
    cpv_prefixes: str = Form(''),
    keywords: str = Form(''),
    negative_keywords: str = Form(''),
    required_certificates: str = Form(''),
    preferred_regions: list[str] = Form([]),
    min_budget: str = Form(''),
    max_budget: str = Form(''),
    rss_feeds: str = Form(''),
    is_active: Optional[str] = Form(None),
    rescore_after_save: Optional[str] = Form(None),
    db: Session = DbDep,
):
    profile = db.query(ClientProfile).filter(ClientProfile.id == profile_id).one_or_none()
    if profile is None:
        raise HTTPException(status_code=404, detail='Profile not found')
    old_slug = profile.slug
    _save_profile_from_form(profile, slug, name, description, cpv_codes, cpv_prefixes, keywords, negative_keywords, required_certificates, preferred_regions, min_budget, max_budget, rss_feeds, is_active)
    if not profile.name:
        return templates.TemplateResponse('profile_form.html', _profile_form_context(request, profile, 'edit', 'Συμπληρώστε όνομα προφίλ.'))
    duplicate = db.query(ClientProfile).filter(ClientProfile.slug == profile.slug, ClientProfile.id != profile.id).first()
    if duplicate:
        profile.slug = old_slug
        return templates.TemplateResponse('profile_form.html', _profile_form_context(request, profile, 'edit', 'Υπάρχει ήδη profile με αυτό το slug.'))
    log_event(db, 'profile_updated', 'Ενημερώθηκε προφίλ', f'{profile.name} ({profile.slug})', {'profile_id': profile.id})
    if rescore_after_save == 'on':
        db.flush()
        result = rescore_existing_tenders(db, profile_id=profile.id)
        log_event(
            db,
            'rescore',
            'Ολοκληρώθηκε ανανέωση σχετικότητας',
            f"Ενημερώθηκαν {result['scores_updated']} αξιολογήσεις για το προφίλ {profile.name}.",
            {'profile_id': profile.id, **result},
        )
    db.commit()
    return RedirectResponse(url='/profiles', status_code=status.HTTP_303_SEE_OTHER)


@app.post('/profiles/{profile_id}/toggle', dependencies=[AuthDep])
def profile_toggle(profile_id: int, db: Session = DbDep) -> RedirectResponse:
    profile = db.query(ClientProfile).filter(ClientProfile.id == profile_id).one_or_none()
    if profile is None:
        raise HTTPException(status_code=404, detail='Profile not found')
    profile.is_active = not profile.is_active
    log_event(db, 'profile_toggled', 'Άλλαξε κατάσταση προφίλ', f"{profile.name}: {'ενεργό' if profile.is_active else 'ανενεργό'}", {'profile_id': profile.id, 'is_active': profile.is_active})
    db.flush()
    active_count = db.query(ClientProfile).filter(ClientProfile.is_active.is_(True)).count()
    if active_count == 0:
        log_event(
            db,
            'profile_warning',
            'Δεν υπάρχει ενεργό προφίλ',
            'Η ημερήσια εισαγωγή δεν θα φέρνει αποτελέσματα μέχρι να ενεργοποιηθεί τουλάχιστον ένα προφίλ.',
            {'profile_id': profile.id},
        )
    db.commit()
    url = '/profiles?profile_warning=no_active_profiles' if active_count == 0 else '/profiles'
    return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)




@app.post('/profiles/{profile_id}/delete', dependencies=[AuthDep])
def profile_delete(profile_id: int, db: Session = DbDep) -> RedirectResponse:
    profile = db.query(ClientProfile).filter(ClientProfile.id == profile_id).one_or_none()
    if profile is None:
        raise HTTPException(status_code=404, detail='Profile not found')
    total_profiles = db.query(ClientProfile).count()
    if total_profiles <= 1:
        raise HTTPException(status_code=400, detail='Δεν μπορεί να διαγραφεί το τελευταίο προφίλ. Πρέπει να υπάρχει τουλάχιστον ένα προφίλ παρακολούθησης.')
    profile_name = profile.name
    profile_slug = profile.slug
    db.delete(profile)
    log_event(
        db,
        'profile_deleted',
        'Διαγράφηκε προφίλ',
        f'{profile_name} ({profile_slug})',
        {'profile_id': profile_id, 'profile_slug': profile_slug},
    )
    db.commit()
    return RedirectResponse(url='/profiles', status_code=status.HTTP_303_SEE_OTHER)


@app.get('/reports', response_class=HTMLResponse, dependencies=[AuthDep])
def reports_page(
    request: Request,
    db: Session = DbDep,
    date_from: str = '',
    date_to: str = '',
    profile_id: str = '',
    min_score: int = 55,
    scope: str = 'matches',
    active_only: str = 'on',
    q: str = '',
    region: str = '',
    rescore_done: str = '',
    ingest_done: str = '',
    ingest_warning: str = '',
) -> HTMLResponse:
    date_from = normalize_date_input(date_from)
    date_to = normalize_date_input(date_to)
    if scope == 'new':
        scope = 'latest_new'
    # Reports start without an implicit period. Empty dates mean "all stored KIMDIS records",
    # so the numbers are easier to compare with the dashboard unless the user narrows them.
    selected_profile_id = _parse_int(profile_id)
    profiles = db.query(ClientProfile).order_by(ClientProfile.is_active.desc(), ClientProfile.name.asc()).all()
    if selected_profile_id is None and profile_id in ('', None) and profiles:
        first_active = next((p for p in profiles if p.is_active), profiles[0])
        selected_profile_id = first_active.id
    profile = db.query(ClientProfile).filter(ClientProfile.id == selected_profile_id).one_or_none() if selected_profile_id else None
    filters = ReportFilters(
        date_from=date_from,
        date_to=date_to,
        profile_id=selected_profile_id,
        min_score=min_score,
        scope=scope,
        active_only=active_only == 'on',
        q=q,
        region=region,
    )
    report_scores = query_report_scores(db, filters)
    scores = report_scores[:100]
    summary = report_summary(report_scores)
    return templates.TemplateResponse(
        'reports.html',
        {
            'request': request,
            'profiles': profiles,
            'scores': scores,
            'date_from': date_from,
            'date_to': date_to,
            'profile_id': selected_profile_id,
            'min_score': min_score,
            'scope': scope,
            'active_only': active_only,
            'q': q,
            'region': region,
            'selected_profile': profile,
            'summary': summary,
            'report_total': len(report_scores),
            'scope_label': report_scope_label(scope),
            'period_label': report_period_label(filters),
            'deadline_scope_label': 'Μόνο ενεργά ή άγνωστης προθεσμίας' if active_only == 'on' else 'Όλα, και όσα έχουν λήξει',
            'nuts_options_grouped': nuts_options_grouped(),
            'rescore_done': rescore_done,
            'ingest_done': ingest_done,
            'ingest_warning': ingest_warning,
        },
    )


@app.get('/reports/export', dependencies=[AuthDep])
def reports_export(
    db: Session = DbDep,
    date_from: str = '',
    date_to: str = '',
    profile_id: str = '',
    min_score: int = 55,
    scope: str = 'matches',
    active_only: str = 'on',
    q: str = '',
    region: str = '',
    format: str = 'pdf',
):
    date_from = normalize_date_input(date_from)
    date_to = normalize_date_input(date_to)
    if scope == 'new':
        scope = 'latest_new'
    selected_profile_id = _parse_int(profile_id)
    if selected_profile_id is None and profile_id in ('', None):
        first_profile = db.query(ClientProfile).filter(ClientProfile.is_active.is_(True)).order_by(ClientProfile.name.asc()).first()
        selected_profile_id = first_profile.id if first_profile else None
    profile = db.query(ClientProfile).filter(ClientProfile.id == selected_profile_id).one_or_none() if selected_profile_id else None
    filters = ReportFilters(
        date_from=date_from,
        date_to=date_to,
        profile_id=selected_profile_id,
        min_score=min_score,
        scope=scope,
        active_only=active_only == 'on',
        q=q,
        region=region,
    )
    scores = query_report_scores(db, filters)
    if date_from or date_to:
        stem = f'tender_report_{date_from or "start"}_to_{date_to or "today"}'
    else:
        stem = 'tender_report_all_dates'
    if format == 'csv':
        return make_csv_response(scores, f'{stem}.csv')
    if format == 'jsonl':
        return make_jsonl_response(scores, f'{stem}.jsonl')
    md = report_to_markdown(scores, filters, profile)
    if format == 'md':
        return make_markdown_response(md, f'{stem}.md')
    return make_pdf_response('Αναφορά διαγωνισμών', md, f'{stem}.pdf')


@app.get('/profiles/{profile_id}/export', dependencies=[AuthDep])
def profile_export(profile_id: int, format: str = 'pdf', db: Session = DbDep):
    profile = db.query(ClientProfile).filter(ClientProfile.id == profile_id).one_or_none()
    if profile is None:
        raise HTTPException(status_code=404, detail='Profile not found')
    md = profile_to_markdown(profile)
    filename_stem = f'profile_{profile.slug or profile.id}'
    if format == 'md':
        return make_markdown_response(md, f'{filename_stem}.md')
    return make_pdf_response('Προφίλ παρακολούθησης', md, f'{filename_stem}.pdf')


# Ο standalone CPV helper αφαιρέθηκε από το UI.
# Η επιλογή CPV γίνεται πλέον αποκλειστικά μέσα από τη φόρμα προφίλ.


# API αναζήτησης CPV για μελλοντική χρήση/inline επιλογείς.


def _cpv_api_item_from_code(code: str) -> dict[str, object] | None:
    rec = cpv_record(code)
    if rec is None:
        return None
    return {
        'code': rec.code,
        'title': rec.title,
        'parent_code': rec.parent_code or '',
        'level': rec.level,
        'has_children': bool(cpv_tree_children(rec.code, limit=1)),
        'ancestors': cpv_ancestor_codes(rec.code),
        'category': f'{rec.root_code} — {rec.root_title}',
    }


@app.get('/api/cpv/search', dependencies=[AuthDep])
def api_cpv_search(q: str = '', category: str = '', limit: int = 50) -> list[dict[str, object]]:
    return [
        item for item in (
            _cpv_api_item_from_code(entry.code) for entry in cpv_search(q, limit=max(1, min(100, limit)), category=category)
        ) if item is not None
    ]


@app.get('/api/cpv/children', dependencies=[AuthDep])
def api_cpv_children(parent: str = '') -> list[dict[str, object]]:
    return [
        {
            'code': row.code,
            'title': row.title,
            'parent_code': row.parent_id or '',
            'level': row.level,
            'has_children': row.has_children,
            'ancestors': cpv_ancestor_codes(row.code),
        }
        for row in cpv_tree_children(parent or None)
    ]




@app.get('/maintenance', response_class=HTMLResponse, dependencies=[AuthDep])
def maintenance_page(request: Request, db: Session = DbDep) -> HTMLResponse:
    stats = database_usage_summary(db)
    return templates.TemplateResponse('maintenance.html', {'request': request, 'stats': stats})

@app.get('/activity', response_class=HTMLResponse, dependencies=[AuthDep])
def activity_log(request: Request, db: Session = DbDep) -> HTMLResponse:
    events = db.query(SystemEvent).order_by(SystemEvent.created_at.desc()).limit(200).all()
    return templates.TemplateResponse('activity.html', {'request': request, 'events': events})


@app.get('/api/tenders', dependencies=[AuthDep])
def api_tenders(db: Session = DbDep, min_score: int = 55) -> list[dict]:
    scores = (
        db.query(TenderScore)
        .options(joinedload(TenderScore.tender), joinedload(TenderScore.profile))
        .filter(TenderScore.score >= min_score)
        .order_by(TenderScore.score.desc())
        .limit(200)
        .all()
    )
    return [
        {
            'score': s.score,
            'recommended_action': s.recommended_action,
            'workflow_status': s.user_status,
            'profile': s.profile.name,
            'title': s.tender.title,
            'organization': s.tender.organization_name,
            'reference_number': s.tender.reference_number,
            'final_submission_date': format_local_datetime(s.tender.final_submission_date, include_tz=False) if s.tender.final_submission_date else None,
            'cpv_codes': s.tender.cpv_codes,
            'attachment_url': s.tender.attachment_url,
            'reasons': s.reasons,
        }
        for s in scores
    ]
