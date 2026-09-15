from __future__ import annotations

import logging
import math
import re
import secrets
import time
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Optional
from urllib.parse import parse_qs, urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
import httpx
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from sqlalchemy import case, func, or_, text
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.db import SessionLocal, get_db, init_db, schema_revision
from app.jobs.ingest import score_and_store
from app.models import AppUser, BackgroundJob, ClientProfile, DiavgeiaDecision, SystemEvent, Tender, TenderChange, TenderScore
from app.services.activity import log_event
from app.services.auth import CSRF_COOKIE, SESSION_COOKIE, hash_password, make_csrf_token, make_session_token, parse_session_token, password_meets_policy, verify_csrf_token, verify_password
from app.services.cpv_catalog import cpv_by_codes, cpv_categories, cpv_category_suggestions, cpv_search, cpv_prefixes_for_codes, cpv_tree_children, cpv_record, expand_cpv_codes_for_ingest, cpv_covered_by_selected_parent_codes, cpv_catalog_size, cpv_ancestor_codes, is_valid_cpv_code
from app.services.geography import any_region_match, preferred_region_matches, preferred_region_match_details, tender_region_text, tender_execution_region_values, tender_authority_region_values, region_filter_expressions, nuts_options_grouped, selected_region_labels
from app.services.khmdhs_client import CONTRACT_TYPES, FRIENDLY_OPERATION_CONTEXT, KIMDIS_VIEWS, OPERATION_TYPES, KhmdhsClient, build_search_body, infer_resource_from_reference_number
from app.services.repository import upsert_tender
from app.services.timezone import format_local_date, format_local_datetime, format_kimdis_publication_datetime, local_day_end, local_day_start, now_local, now_utc, today_local
from app.services.text_normalizer import display_text, looks_like_replacement_garbage
from app.services.workflow import WORKFLOW_STATUSES, normalize_workflow_status, workflow_status_class, workflow_status_filter_values, workflow_status_label
from app.services.date_inputs import display_date_input, normalize_date_input
from app.services.diavgeia_enrichment import DiavgeiaClientError, find_and_store_related_diavgeia_decisions
from app.services.job_queue import enqueue_job
from app.services.opportunity_status import actionable_tender_clause, expired_tender_clause, tender_lifecycle_label
from app.services.scoring import classify_cpv_match, display_scoring_reason
from app.services.reports import (
    ReportFilters,
    make_csv_response,
    make_jsonl_response,
    make_markdown_response,
    make_pdf_response,
    make_pdf_urls_response,
    profile_to_markdown,
    query_report_scores,
    report_to_markdown,
    report_summary,
    report_scope_label,
    report_period_label,
    report_match_label,
)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    bootstrap_admin_user()
    yield


app = FastAPI(title='Tender Assistant', version='0.11.0', lifespan=lifespan)
templates = Jinja2Templates(directory='app/templates')
security = HTTPBasic(auto_error=False)
logger = logging.getLogger(__name__)
_LOGIN_FAILURES: dict[str, list[float]] = {}
SAFE_METHODS = {'GET', 'HEAD', 'OPTIONS', 'TRACE'}

DEADLINE_FILTERS = {
    'all': 'Όλοι',
    'active': 'Ενεργά ή άγνωστη προθεσμία',
    'expired': 'Έχουν λήξει',
    'cancelled': 'Ακυρωμένα / ματαιωμένα',
    'unknown': 'Άγνωστη προθεσμία',
}

DASHBOARD_PAGE_SIZE = 20
DASHBOARD_MATCH_TYPES = {'all', 'exact_full', 'exact_partial', 'broad', 'none'}
EXPECTED_SCHEMA_REVISION = '0004_score_match_category'


def _session_secret() -> str:
    settings = get_settings()
    if settings.session_secret_key:
        return settings.session_secret_key
    production_like = settings.app_env.strip().lower() in {'prod', 'production'}
    if settings.require_session_secret or production_like:
        raise RuntimeError('SESSION_SECRET_KEY is required when APP_ENV=production or REQUIRE_SESSION_SECRET=true.')
    legacy_secret = settings.bootstrap_admin_password or settings.admin_password
    if legacy_secret:
        logger.warning('Using admin password as session secret. Set SESSION_SECRET_KEY explicitly.')
        return legacy_secret
    logger.warning('Using development session secret. Set SESSION_SECRET_KEY before exposing the app.')
    return 'dev-session-secret-change-me'


def _is_production_like() -> bool:
    return get_settings().app_env.strip().lower() in {'prod', 'production'}


def _session_cookie_secure() -> bool:
    settings = get_settings()
    return settings.session_cookie_secure or _is_production_like()


def _csrf_seed(request: Request) -> str:
    seed = getattr(request.state, 'csrf_seed', '') or request.cookies.get(CSRF_COOKIE, '')
    if not seed:
        seed = secrets.token_urlsafe(24)
        request.state.csrf_seed = seed
    return seed


@pass_context
def csrf_token(context) -> str:
    request = context.get('request')
    if request is None:
        return ''
    return make_csrf_token(_csrf_seed(request), _session_secret())


templates.env.globals['csrf_token'] = csrf_token


async def _csrf_token_from_request(request: Request) -> str:
    token = request.headers.get('x-csrf-token') or ''
    if token:
        return token
    content_type = request.headers.get('content-type', '')
    if 'application/x-www-form-urlencoded' in content_type:
        body = await request.body()
        values = parse_qs(body.decode('utf-8', errors='ignore'), keep_blank_values=True)
        return values.get('csrf_token', [''])[0]
    form = await request.form()
    value = form.get('csrf_token', '')
    return str(value or '')


@app.middleware('http')
async def browser_security_middleware(request: Request, call_next):
    settings = get_settings()
    if settings.csrf_protection_enabled and request.method.upper() not in SAFE_METHODS:
        token = await _csrf_token_from_request(request)
        if not verify_csrf_token(token, _session_secret()):
            return PlainTextResponse('Invalid CSRF token.', status_code=status.HTTP_403_FORBIDDEN)
    response = await call_next(request)
    seed = getattr(request.state, 'csrf_seed', '') or request.cookies.get(CSRF_COOKIE, '')
    if seed:
        response.set_cookie(
            CSRF_COOKIE,
            seed,
            httponly=True,
            samesite='lax',
            secure=_session_cookie_secure(),
            max_age=settings.session_max_age_seconds,
        )
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    response.headers.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
    response.headers.setdefault(
        'Content-Security-Policy',
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'self'",
    )
    if _is_production_like():
        response.headers.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
    return response


def _login_rate_limit_key(request: Request, username: str) -> str:
    client_host = request.client.host if request.client else 'unknown'
    return f'{client_host}:{username.strip().lower()}'


def _login_is_rate_limited(request: Request, username: str) -> bool:
    settings = get_settings()
    now = time.monotonic()
    window = max(1, settings.login_rate_limit_window_seconds)
    key = _login_rate_limit_key(request, username)
    attempts = [item for item in _LOGIN_FAILURES.get(key, []) if now - item <= window]
    _LOGIN_FAILURES[key] = attempts
    return len(attempts) >= max(1, settings.login_rate_limit_attempts)


def _record_login_failure(request: Request, username: str) -> None:
    _LOGIN_FAILURES.setdefault(_login_rate_limit_key(request, username), []).append(time.monotonic())


def _clear_login_failures(request: Request, username: str) -> None:
    _LOGIN_FAILURES.pop(_login_rate_limit_key(request, username), None)


def bootstrap_admin_user() -> None:
    from app.db import session_scope

    settings = get_settings()
    username = (settings.bootstrap_admin_username or settings.admin_username or '').strip()
    password = settings.bootstrap_admin_password or settings.admin_password or ''
    if not username or not password:
        return
    if not password_meets_policy(password, settings.min_password_length):
        raise RuntimeError(f'Bootstrap admin password must be at least {settings.min_password_length} characters.')
    with session_scope() as db:
        if db.query(AppUser).count() > 0:
            return
        user = AppUser(
            username=username,
            password_hash=hash_password(password),
            full_name=username,
            email=settings.bootstrap_admin_email,
            role='admin',
            is_active=True,
        )
        db.add(user)
        db.flush()
        db.query(ClientProfile).filter(ClientProfile.owner_user_id.is_(None)).update({ClientProfile.owner_user_id: user.id})
        logger.info('Bootstrapped admin user %s', username)


def _visible_profiles_query(db: Session, user: AppUser):
    query = db.query(ClientProfile)
    if not user.is_admin:
        query = query.filter(ClientProfile.owner_user_id == user.id)
    return query


def _visible_profile_ids(db: Session, user: AppUser) -> list[int]:
    return [row[0] for row in _visible_profiles_query(db, user).with_entities(ClientProfile.id).all()]


def _get_visible_profile(db: Session, user: AppUser, profile_id: int | None, active_only: bool = False) -> ClientProfile | None:
    if profile_id is None:
        return None
    query = _visible_profiles_query(db, user).filter(ClientProfile.id == profile_id)
    if active_only:
        query = query.filter(ClientProfile.is_active.is_(True))
    return query.one_or_none()


def _default_dashboard_profile_id(user: AppUser, profiles: list[ClientProfile], profile_id: str | int | None) -> int | None:
    selected_profile_id = _parse_int(profile_id)
    if selected_profile_id is None and profile_id in ('', None) and profiles and not user.is_admin:
        first_active = next((p for p in profiles if p.is_active), profiles[0])
        return first_active.id
    return selected_profile_id


def _filter_scores_for_user(query, user: AppUser):
    if user.is_admin:
        return query
    return query.join(ClientProfile, TenderScore.profile_id == ClientProfile.id).filter(ClientProfile.owner_user_id == user.id)


def _visible_tender_score_query(db: Session, user: AppUser, tender_id: int, profile_id: int | None = None):
    query = db.query(TenderScore).join(ClientProfile).filter(TenderScore.tender_id == tender_id)
    if profile_id:
        query = query.filter(TenderScore.profile_id == profile_id)
    if not user.is_admin:
        query = query.filter(ClientProfile.owner_user_id == user.id)
    return query


def _visible_jobs_query(db: Session, user: AppUser):
    query = db.query(BackgroundJob)
    if user.is_admin:
        return query
    owned_profile_ids = _visible_profile_ids(db, user)
    clauses = [BackgroundJob.requested_by_user_id == user.id]
    if owned_profile_ids:
        clauses.append(BackgroundJob.profile_id.in_(owned_profile_ids))
    return query.filter(or_(*clauses))


def require_auth(
    request: Request,
    credentials: Annotated[Optional[HTTPBasicCredentials], Depends(security)] = None,
    db: Session = Depends(get_db),
) -> AppUser:
    settings = get_settings()
    token_user_id = parse_session_token(
        request.cookies.get(SESSION_COOKIE),
        _session_secret(),
        max_age_seconds=settings.session_max_age_seconds,
    )
    if token_user_id is not None:
        user = db.query(AppUser).filter(AppUser.id == token_user_id, AppUser.is_active.is_(True)).one_or_none()
        if user is not None:
            request.state.current_user = user
            return user

    users_exist = db.query(AppUser).count() > 0
    if settings.allow_local_admin_fallback and not users_exist and not settings.admin_username and not settings.admin_password:
        user = AppUser(id=0, username='local', full_name='Local admin', role='admin', is_active=True, password_hash='')
        request.state.current_user = user
        return user
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            detail='Authentication required',
            headers={'Location': '/login'},
        )

    if _login_is_rate_limited(request, credentials.username or ''):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail='Too many authentication attempts')
    user = db.query(AppUser).filter(AppUser.username == (credentials.username or '').strip(), AppUser.is_active.is_(True)).one_or_none()
    if user is not None and verify_password(credentials.password or '', user.password_hash):
        _clear_login_failures(request, credentials.username or '')
        request.state.current_user = user
        return user

    username_ok = secrets.compare_digest(credentials.username or '', settings.admin_username or '')
    password_ok = secrets.compare_digest(credentials.password or '', settings.admin_password or '')
    if not users_exist and settings.admin_username and settings.admin_password and username_ok and password_ok:
        _clear_login_failures(request, credentials.username or '')
        fallback = AppUser(id=0, username=settings.admin_username, full_name='Legacy admin', role='admin', is_active=True, password_hash='')
        request.state.current_user = fallback
        return fallback

    _record_login_failure(request, credentials.username or '')
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail='Invalid credentials',
        headers={'WWW-Authenticate': 'Basic'},
    )


def require_admin(request: Request, user: AppUser = Depends(require_auth)) -> AppUser:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail='Admin access required')
    request.state.current_user = user
    return user


@app.get('/login', response_class=HTMLResponse)
def login_page(request: Request, error: str = '') -> HTMLResponse:
    return templates.TemplateResponse('login.html', {'request': request, 'error': error})


@app.post('/login')
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if _login_is_rate_limited(request, username):
        return RedirectResponse(url='/login?error=rate_limited', status_code=status.HTTP_303_SEE_OTHER)
    user = db.query(AppUser).filter(AppUser.username == username.strip(), AppUser.is_active.is_(True)).one_or_none()
    if user is None or not verify_password(password, user.password_hash):
        _record_login_failure(request, username)
        return RedirectResponse(url='/login?error=1', status_code=status.HTTP_303_SEE_OTHER)
    _clear_login_failures(request, username)
    response = RedirectResponse(url='/admin' if user.is_admin else '/', status_code=status.HTTP_303_SEE_OTHER)
    max_age = get_settings().session_max_age_seconds
    response.set_cookie(
        SESSION_COOKIE,
        make_session_token(user.id, _session_secret()),
        httponly=True,
        samesite='lax',
        max_age=max_age,
        secure=_session_cookie_secure(),
    )
    return response


@app.post('/logout')
def logout() -> RedirectResponse:
    response = RedirectResponse(url='/login', status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get('/admin', response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def admin_page(
    request: Request,
    db: Session = Depends(get_db),
    ingest_done: str = '',
    rescore_done: str = '',
    ingest_warning: str = '',
    job_id: str = '',
    job_created: str = '',
) -> HTMLResponse:
    return templates.TemplateResponse(
        'admin.html',
        {
            'request': request,
            'overview': admin_overview_summary(db),
            'ingest_done': ingest_done,
            'rescore_done': rescore_done,
            'ingest_warning': ingest_warning,
            'job_id': job_id,
            'job_created': job_created,
        },
    )


@app.get('/admin/users', response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def admin_users_page(request: Request, db: Session = Depends(get_db), created: str = '', error: str = '') -> HTMLResponse:
    users = db.query(AppUser).order_by(AppUser.username.asc()).all()
    return templates.TemplateResponse(
        'admin_users.html',
        {
            'request': request,
            'users': users,
            'created': created,
            'error': error,
            'min_password_length': get_settings().min_password_length,
        },
    )


@app.post('/admin/users', dependencies=[Depends(require_admin)])
def admin_users_create(
    username: str = Form(...),
    password: str = Form(...),
    full_name: str = Form(''),
    email: str = Form(''),
    role: str = Form('user'),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    username = username.strip()
    role = role if role in ('admin', 'user') else 'user'
    if not username or not password:
        return RedirectResponse(url='/admin/users?error=missing', status_code=status.HTTP_303_SEE_OTHER)
    if not password_meets_policy(password, get_settings().min_password_length):
        return RedirectResponse(url='/admin/users?error=weak_password', status_code=status.HTTP_303_SEE_OTHER)
    if db.query(AppUser).filter(AppUser.username == username).first():
        return RedirectResponse(url='/admin/users?error=duplicate', status_code=status.HTTP_303_SEE_OTHER)
    user = AppUser(
        username=username,
        password_hash=hash_password(password),
        full_name=full_name.strip() or username,
        email=email.strip() or None,
        role=role,
        is_active=True,
    )
    db.add(user)
    db.flush()
    log_event(db, 'user_created', 'Created user', f'{user.username} ({user.role})', {'user_id': user.id, 'role': user.role})
    db.commit()
    return RedirectResponse(url='/admin/users?created=1', status_code=status.HTTP_303_SEE_OTHER)


def current_user_from_request(request: Request) -> AppUser:
    user = getattr(request.state, 'current_user', None)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Authentication required',
        )
    return user


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


def _safe_return_url(value: str | None, default: str = '/') -> str:
    if not value or not value.startswith('/') or value.startswith('//'):
        return default
    if '\r' in value or '\n' in value:
        return default
    return value


def _dashboard_query_url(request: Request, **updates: object) -> str:
    params = dict(request.query_params)
    for key, value in updates.items():
        if value is None or value == '':
            params.pop(key, None)
        else:
            params[key] = str(value)
    query = urlencode(params)
    return f'/?{query}' if query else '/'


def _dashboard_date(value: str | None) -> tuple[str, date | None]:
    normalized = normalize_date_input(value)
    try:
        return normalized, date.fromisoformat(normalized) if normalized else None
    except ValueError:
        return '', None


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
    if tender.cancelled:
        return {'label': 'Ακυρώθηκε / ματαιώθηκε', 'class': 'deadline-expired'}
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
    if tender.is_modified:
        badges.append({'label': 'Τροποποιημένη πράξη', 'class': 'reviewing'})
    return badges


def recommended_action_text(score: TenderScore) -> dict[str, str]:
    tender = score.tender
    resource = source_resource(tender)
    dl = deadline_badge(tender)
    if tender.cancelled:
        return {'label': 'Μη ενεργή πράξη', 'text': 'Η πράξη εμφανίζεται ματαιωμένη/ακυρωμένη. Χρήσιμη μόνο για ιστορικό έλεγχο.'}
    if resource != 'notice':
        return {'label': 'Ιστορική πράξη (legacy)', 'text': 'Παλαιότερη εγγραφή που παραμένει μόνο για συμβατότητα δεδομένων.'}
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
    region_labels = selected_region_labels(profile.preferred_regions or [])
    budget_parts = []
    if profile.min_budget is not None:
        budget_parts.append(f"from {profile.min_budget:g}")
    if profile.max_budget is not None:
        budget_parts.append(f"to {profile.max_budget:g}")
    return {
        'cpv_entries': cpv_entries,
        'cpv_known': len(cpv_entries),
        'cpv_total': len(profile.cpv_codes or []),
        'region_labels': region_labels,
        'region_total': len(region_labels),
        'has_budget': profile.min_budget is not None or profile.max_budget is not None,
        'budget_label': ' - '.join(budget_parts) if budget_parts else '',
        'certificate_total': len(profile.required_certificates or []),
    }


def _profile_form_context(
    request: Request,
    profile: ClientProfile,
    mode: str,
    error: str | None = None,
    cpv_q: str = '',
    cpv_category: str = '',
    owners: list[AppUser] | None = None,
    form_values: dict[str, str] | None = None,
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
        'owners': owners or [],
        'form_values': form_values or {},
    }


def _validate_profile_values(
    cpv_codes: str,
    min_budget: str,
    max_budget: str,
    is_active: Optional[str],
) -> list[str]:
    errors: list[str] = []
    codes = _split_lines(cpv_codes)
    malformed = [code for code in codes if not is_valid_cpv_code(code)]
    unknown = [code for code in codes if is_valid_cpv_code(code) and cpv_record(code) is None]
    if malformed:
        errors.append('Μη έγκυρη μορφή CPV: ' + ', '.join(malformed[:5]) + '.')
    if unknown:
        errors.append('Οι ακόλουθοι CPV δεν υπάρχουν στον κατάλογο: ' + ', '.join(unknown[:5]) + '.')
    if is_active == 'on' and not codes:
        errors.append('Ένα ενεργό προφίλ πρέπει να έχει τουλάχιστον έναν CPV.')

    parsed_budgets: dict[str, float | None] = {}
    for key, label, raw in (
        ('min', 'ελάχιστο budget', min_budget),
        ('max', 'μέγιστο budget', max_budget),
    ):
        value = _parse_float(raw)
        if str(raw or '').strip() and (value is None or not math.isfinite(value)):
            errors.append(f'Το {label} πρέπει να είναι έγκυρος αριθμός.')
            parsed_budgets[key] = None
        elif value is not None and value < 0:
            errors.append(f'Το {label} δεν μπορεί να είναι αρνητικό.')
            parsed_budgets[key] = value
        else:
            parsed_budgets[key] = value
    minimum = parsed_budgets.get('min')
    maximum = parsed_budgets.get('max')
    if minimum is not None and maximum is not None and minimum > maximum:
        errors.append('Το ελάχιστο budget δεν μπορεί να είναι μεγαλύτερο από το μέγιστο.')
    return errors


def _profile_form_values(min_budget: str, max_budget: str) -> dict[str, str]:
    return {'min_budget': min_budget, 'max_budget': max_budget}


def _profile_scoring_signature(profile: ClientProfile) -> tuple[object, ...]:
    """Fields whose changes require recalculating the profile's stored scores."""
    return (
        tuple(profile.cpv_codes or []),
        tuple(profile.cpv_prefixes or []),
        tuple(profile.preferred_regions or []),
        profile.min_budget,
        profile.max_budget,
        tuple(profile.required_certificates or []),
        bool(profile.is_active),
    )


def dashboard_summary(db: Session, selected_profile_id: int | None = None, user: AppUser | None = None) -> dict[str, object]:
    now = now_utc()
    threshold = get_settings().match_threshold
    base = db.query(TenderScore).join(Tender)
    if selected_profile_id:
        base = base.filter(TenderScore.profile_id == selected_profile_id)
    elif user is not None and not user.is_admin:
        base = base.join(ClientProfile, TenderScore.profile_id == ClientProfile.id).filter(ClientProfile.owner_user_id == user.id)
    total_scores = base.count()
    visible_base = base.filter(~TenderScore.user_status.in_(workflow_status_filter_values('not_relevant')))
    active_clause = actionable_tender_clause(now)
    expired_clause = expired_tender_clause(now)
    cancelled_clause = Tender.cancelled.is_(True)

    # Client-facing dashboard counts should match the default report view:
    # actionable/open items only, not expired records that remain in the database.
    db_matches = visible_base.filter(TenderScore.score >= threshold).count()
    db_high = visible_base.filter(TenderScore.score >= 75).count()
    actionable_matches = visible_base.filter(TenderScore.score >= threshold, active_clause).count()
    actionable_high = visible_base.filter(TenderScore.score >= 75, active_clause).count()
    active = visible_base.filter(active_clause).count()
    soon = visible_base.filter(
        TenderScore.score >= threshold,
        Tender.cancelled.is_(False),
        Tender.final_submission_date >= now,
        Tender.final_submission_date <= now + timedelta(days=7),
    ).count()
    saved = base.filter(TenderScore.user_status.in_(workflow_status_filter_values('saved'))).count()
    reviewing = base.filter(TenderScore.user_status.in_(workflow_status_filter_values('reviewing'))).count()
    not_relevant = base.filter(TenderScore.user_status.in_(workflow_status_filter_values('not_relevant'))).count()
    pending_items = base.filter(TenderScore.user_status.in_(workflow_status_filter_values('new'))).count()
    latest_new = visible_base.filter(
        TenderScore.is_new_in_latest_ingest.is_(True),
        active_clause,
    ).count()
    opportunities = visible_base.filter(Tender.source == 'khmdhs_notice', active_clause).count()
    expired_matches = visible_base.filter(TenderScore.score >= threshold, expired_clause).count()
    cancelled_matches = visible_base.filter(TenderScore.score >= threshold, cancelled_clause).count()
    last_event = latest_system_event_for_scope(db, selected_profile_id=selected_profile_id, user=user)
    last_ingest = latest_system_event_for_scope(
        db,
        event_type='ingest',
        selected_profile_id=selected_profile_id,
        user=user,
    )
    last_ingest_payload = _payload(last_ingest)
    last_rescore = latest_system_event(db, 'rescore')
    return {
        'total_scores': total_scores,
        'matches': actionable_matches,
        'high': actionable_high,
        'db_matches': db_matches,
        'db_high': db_high,
        'expired_matches': expired_matches,
        'cancelled_matches': cancelled_matches,
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
        'last_ingest_payload': last_ingest_payload,
        'last_ingest_profile_payload': _profile_ingest_payload(last_ingest_payload, selected_profile_id),
        'last_rescore': last_rescore,
    }



def latest_system_event(db: Session, event_type: str) -> SystemEvent | None:
    return (
        db.query(SystemEvent)
        .filter(SystemEvent.event_type == event_type)
        .order_by(SystemEvent.created_at.desc())
        .first()
    )


def _event_profile_ids(event: SystemEvent) -> set[int]:
    payload = _payload(event)
    profile_ids: set[int] = set()
    profile_id = _parse_int(payload.get('profile_id'))
    if profile_id is not None:
        profile_ids.add(profile_id)
    per_profile = payload.get('per_profile')
    if isinstance(per_profile, dict):
        for value in per_profile:
            parsed = _parse_int(value)
            if parsed is not None:
                profile_ids.add(parsed)
    return profile_ids


def latest_system_event_for_scope(
    db: Session,
    *,
    event_type: str | None = None,
    selected_profile_id: int | None = None,
    user: AppUser | None = None,
) -> SystemEvent | None:
    """Return the latest event that can be proven to belong to this UI scope.

    Admin/global views retain the operational global event. Profile and non-admin
    views fail closed: legacy/global payloads without profile ownership evidence
    must not be presented as if they belonged to a new customer.
    """
    query = db.query(SystemEvent)
    if event_type:
        query = query.filter(SystemEvent.event_type == event_type)
    query = query.order_by(SystemEvent.created_at.desc(), SystemEvent.id.desc())

    if selected_profile_id is None and (user is None or user.is_admin):
        return query.first()

    target_profile_ids: set[int] = set()
    if selected_profile_id is not None:
        target_profile_ids.add(selected_profile_id)
    elif user is not None:
        target_profile_ids.update(_visible_profile_ids(db, user))

    if not target_profile_ids and user is None:
        return None

    for event in query.limit(500).all():
        payload = _payload(event)
        payload_user_id = _parse_int(payload.get('user_id'))
        requested_by_user_id = _parse_int(payload.get('requested_by_user_id'))
        if user is not None and user.id in (payload_user_id, requested_by_user_id):
            return event
        if target_profile_ids.intersection(_event_profile_ids(event)):
            return event
    return None


def _payload(event: SystemEvent | None) -> dict:
    return event.payload if event is not None and isinstance(event.payload, dict) else {}


def _profile_ingest_payload(payload: dict, selected_profile_id: int | None) -> dict:
    if selected_profile_id is None:
        return payload
    per_profile = payload.get('per_profile') if isinstance(payload, dict) else None
    if not isinstance(per_profile, dict):
        payload_profile_id = _parse_int(payload.get('profile_id')) if isinstance(payload, dict) else None
        if payload_profile_id == selected_profile_id:
            return payload
        return {
            'profile_id': selected_profile_id,
            'tenders': 0,
            'new_tenders': 0,
            'scores': 0,
            'matches': 0,
        }
    profile_payload = per_profile.get(str(selected_profile_id))
    if isinstance(profile_payload, dict):
        return profile_payload
    return {
        'profile_id': selected_profile_id,
        'tenders': 0,
        'new_tenders': 0,
        'scores': 0,
        'matches': 0,
    }


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
    active_or_unknown = db.query(Tender).filter(actionable_tender_clause(now)).count()
    expired = db.query(Tender).filter(expired_tender_clause(now)).count()
    cancelled = db.query(Tender).filter(Tender.cancelled.is_(True)).count()
    pdf_text_count = db.query(Tender).filter(Tender.pdf_text.isnot(None), Tender.pdf_text != '').count()

    return {
        'db_size': db_size,
        'profiles': db.query(ClientProfile).count(),
        'active_profiles': db.query(ClientProfile).filter(ClientProfile.is_active.is_(True)).count(),
        'tenders': db.query(Tender).count(),
        'scores': db.query(TenderScore).count(),
        'active_or_unknown': active_or_unknown,
        'expired': expired,
        'cancelled': cancelled,
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
        'khmdhs_requests_per_minute': settings.khmdhs_requests_per_minute,
        'khmdhs_query_cache_hours': settings.khmdhs_query_cache_hours,
        'khmdhs_sync_overlap_days': settings.khmdhs_sync_overlap_days,
        'khmdhs_continuation_delay_seconds': settings.khmdhs_continuation_delay_seconds,
        'khmdhs_continuation_max_attempts': settings.khmdhs_continuation_max_attempts,
        'match_threshold': settings.match_threshold,
    }


def admin_overview_summary(db: Session) -> dict[str, object]:
    stats = database_usage_summary(db)
    users = db.query(AppUser).order_by(AppUser.is_active.desc(), AppUser.username.asc()).all()
    profiles = db.query(ClientProfile).options(joinedload(ClientProfile.owner)).order_by(ClientProfile.is_active.desc(), ClientProfile.name.asc()).all()
    active_profiles = [profile for profile in profiles if profile.is_active]
    profiles_without_owner = [profile for profile in profiles if profile.owner_user_id is None]
    active_profiles_without_cpv = [profile for profile in active_profiles if not (profile.cpv_codes or [])]
    inactive_profiles = [profile for profile in profiles if not profile.is_active]
    reviewing_count = db.query(TenderScore).filter(TenderScore.user_status.in_(workflow_status_filter_values('reviewing'))).count()
    saved_count = db.query(TenderScore).filter(TenderScore.user_status.in_(workflow_status_filter_values('saved'))).count()
    high_priority_count = (
        db.query(TenderScore)
        .join(Tender)
        .filter(
            TenderScore.score >= 75,
            ~TenderScore.user_status.in_(workflow_status_filter_values('not_relevant')),
            or_(Tender.final_submission_date.is_(None), Tender.final_submission_date >= now_utc()),
        )
        .count()
    )
    user_rows = []
    for user in users:
        owned_profiles = [profile for profile in profiles if profile.owner_user_id == user.id]
        user_rows.append({
            'user': user,
            'profiles': len(owned_profiles),
            'active_profiles': sum(1 for profile in owned_profiles if profile.is_active),
        })
    recent_events = db.query(SystemEvent).order_by(SystemEvent.created_at.desc()).limit(8).all()
    warning_events = (
        db.query(SystemEvent)
        .filter(SystemEvent.event_type.in_(['kimdis_rate_limit', 'profile_warning', 'ingest_error', 'tender_deleted', 'user_created']))
        .order_by(SystemEvent.created_at.desc())
        .limit(8)
        .all()
    )
    return {
        **stats,
        'users': users,
        'users_total': len(users),
        'active_users': sum(1 for user in users if user.is_active),
        'admin_users': sum(1 for user in users if user.is_admin),
        'user_rows': user_rows,
        'profiles_without_owner': profiles_without_owner,
        'active_profiles_without_cpv': active_profiles_without_cpv,
        'inactive_profiles': inactive_profiles,
        'reviewing_count': reviewing_count,
        'saved_count': saved_count,
        'high_priority_count': high_priority_count,
        'recent_events': recent_events,
        'warning_events': warning_events,
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
                        elif str(row or '').strip():
                            # The live adamChain endpoint commonly returns arrays
                            # of ADAM strings, not record objects.
                            row = {'referenceNumber': str(row).strip(), '_stage': key}
                        items.append(row)
            chain_payload = items if items else [chain_payload]
    if not isinstance(chain_payload, list):
        return []
    out: list[dict[str, str]] = []
    stage_labels = {
        'requests': 'Αρχικό αίτημα',
        'approvedRequests': 'Εγκεκριμένο αίτημα',
        'notices': 'Διακήρυξη / πρόσκληση',
        'auctions': 'Ανάθεση',
        'contracts': 'Σύμβαση',
        'payments': 'Πληρωμή',
    }
    seen: set[tuple[str, str]] = set()
    for row in chain_payload[:30]:
        if not isinstance(row, dict):
            continue
        ref = str(row.get('referenceNumber') or row.get('adam') or row.get('ADAM') or row.get('refNo') or row.get('code') or '').strip()
        title = str(row.get('title') or row.get('subject') or row.get('description') or '')
        raw_stage = str(row.get('_stage') or row.get('type') or row.get('actType') or row.get('documentType') or row.get('resource') or '')
        stage = stage_labels.get(raw_stage, raw_stage)
        date_value = str(row.get('submissionDate') or row.get('publishedDate') or row.get('signedDate') or row.get('date') or '')
        identity = (ref, stage)
        if not ref or identity in seen:
            continue
        seen.add(identity)
        out.append({'reference': ref, 'title': title, 'stage': stage, 'date': date_value})
    return out


def _stored_chain_items_from_raw(raw: object) -> list[dict[str, str]]:
    raw = raw if isinstance(raw, dict) else {}
    payload: dict[str, list[object]] = {
        'requests': [], 'approvedRequests': [], 'notices': [],
        'auctions': [], 'contracts': [], 'payments': [],
    }
    for source_key, stage in (
        ('requests', 'requests'),
        ('approvedRequests', 'approvedRequests'),
        ('notices', 'notices'),
        ('noticeRefNo', 'notices'),
        ('auctionRefNo', 'auctions'),
        ('contractRefNo', 'contracts'),
        ('paymentRefNo', 'payments'),
    ):
        value = raw.get(source_key)
        if isinstance(value, list) and value:
            payload[stage].extend(value)
    previous_request = raw.get('previousRequestReferenceNumber')
    if previous_request:
        payload['requests'].append(previous_request)
    related_notice = raw.get('relatedNoticeADAM')
    if related_notice:
        payload['notices'].append(related_notice)
    return normalize_chain_items(payload)


def stored_tender_chain_items(tender: Tender) -> list[dict[str, str]]:
    """Extract immediately connected ADAMs already present in stored KIMDIS raw data."""
    return _stored_chain_items_from_raw(tender.raw)


def _adam_chain_seed(tender: Tender, stored_items: list[dict[str, str]]) -> str:
    own_reference = (tender.reference_number or '').strip()
    if 'REQ' in own_reference.upper():
        return own_reference
    # KIMDIS is substantially more reliable when adamChain starts from a request.
    approved = next(
        (item['reference'] for item in stored_items if 'REQ' in item['reference'].upper() and item['stage'] == 'Εγκεκριμένο αίτημα'),
        '',
    )
    request = next((item['reference'] for item in stored_items if 'REQ' in item['reference'].upper()), '')
    return approved or request or own_reference


def _merge_chain_items(*groups: list[dict[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            reference = item.get('reference', '')
            if not reference or reference in seen:
                continue
            seen.add(reference)
            out.append(item)
    stage_order = {
        'Αρχικό αίτημα': 0, 'Αίτημα': 1, 'Εγκεκριμένο αίτημα': 2,
        'Διακήρυξη / πρόσκληση': 3, 'Ανάθεση': 4, 'Σύμβαση': 5, 'Πληρωμή': 6,
    }
    out.sort(key=lambda item: stage_order.get(item.get('stage', ''), 99))
    return out


def _split_cpv_preview(
    tender_cpvs: object,
    matched_cpvs: object,
    *,
    preview_limit: int = 5,
) -> tuple[list[str], list[str]]:
    """Keep matched CPVs visible and move every other CPV into the disclosure."""
    all_codes = list(dict.fromkeys(
        str(code).strip() for code in _safe_list(tender_cpvs) if str(code).strip()
    ))
    all_code_set = set(all_codes)
    matched = list(dict.fromkeys(
        str(code).strip()
        for code in _safe_list(matched_cpvs)
        if str(code).strip() in all_code_set
    ))
    preview = matched[:max(0, preview_limit)]
    preview_set = set(preview)
    matched_set = set(matched)
    overflow = [
        *matched[len(preview):],
        *(code for code in all_codes if code not in preview_set and code not in matched_set),
    ]
    return preview, overflow


def load_tender_chain(tender: Tender, client: KhmdhsClient | None = None) -> tuple[list[dict[str, str]], str | None]:
    """Load a KIMDIS chain quickly, retaining stored links as a safe fallback."""
    stored_items = stored_tender_chain_items(tender)
    seed = _adam_chain_seed(tender, stored_items)
    if not seed:
        return stored_items, None
    api = client or KhmdhsClient()
    try:
        if 'REQ' in seed.upper():
            request_record = api.request_by_reference(seed, timeout_seconds=4)
            if not request_record:
                return stored_items, None if stored_items else 'Δεν βρέθηκε η πορεία στο ΚΗΜΔΗΣ.'
            request_items = _stored_chain_items_from_raw(request_record)
            stored_seed = next(
                (item for item in stored_items if item.get('reference') == seed),
                None,
            )
            is_approved = bool(request_record.get('approved')) or bool(
                stored_seed and stored_seed.get('stage') == 'Εγκεκριμένο αίτημα'
            )
            seed_item = [{
                'reference': seed,
                'title': str(request_record.get('title') or ''),
                'stage': 'Εγκεκριμένο αίτημα' if is_approved else 'Αίτημα',
                'date': str(request_record.get('submissionDate') or ''),
            }]
            return _merge_chain_items(request_items, seed_item, stored_items), None
        remote_items = normalize_chain_items(api.adam_chain(seed, timeout_seconds=4))
        return _merge_chain_items(remote_items, stored_items), None
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        if stored_items:
            return stored_items, None
        if status_code == 429:
            message = 'Η πλήρης πορεία δεν είναι προσωρινά διαθέσιμη από το ΚΗΜΔΗΣ.'
        else:
            message = 'Δεν ανακτήθηκε η πλήρης πορεία από το ΚΗΜΔΗΣ. Εμφανίζονται οι ήδη αποθηκευμένες συνδέσεις.'
        return stored_items, message
    except httpx.TimeoutException:
        return stored_items, None if stored_items else 'Το ΚΗΜΔΗΣ καθυστέρησε να απαντήσει.'
    except Exception:
        return stored_items, None if stored_items else 'Δεν ανακτήθηκε η πορεία από το ΚΗΜΔΗΣ.'


templates.env.globals['deadline_badge'] = deadline_badge
templates.env.globals['workflow_statuses'] = WORKFLOW_STATUSES
templates.env.globals['workflow_status_label'] = workflow_status_label
templates.env.globals['workflow_status_class'] = workflow_status_class
templates.env.globals['normalize_workflow_status'] = normalize_workflow_status
templates.env.globals['deadline_filters'] = DEADLINE_FILTERS
templates.env.globals['report_match_label'] = report_match_label
templates.env.globals['display_scoring_reason'] = display_scoring_reason
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
templates.env.globals['tender_execution_region_values'] = tender_execution_region_values
templates.env.globals['tender_authority_region_values'] = tender_authority_region_values
templates.env.globals['preferred_region_matches'] = preferred_region_matches
templates.env.filters['list_to_text'] = _list_to_text
templates.env.filters['local_dt'] = format_local_datetime
templates.env.filters['local_date'] = format_local_date
templates.env.filters['kimdis_pub_dt'] = format_kimdis_publication_datetime
templates.env.filters['display_text'] = display_text
templates.env.filters['safe_list'] = _safe_list
templates.env.filters['safe_dict'] = _safe_dict
templates.env.filters['date_input'] = display_date_input
templates.env.globals['text_has_encoding_issue'] = looks_like_replacement_garbage
templates.env.globals['app_timezone'] = lambda: get_settings().app_timezone
templates.env.globals['now_local'] = now_local


@app.get('/health')
def health(db: Session = Depends(get_db)):
    """Readiness-oriented health response used by operators and containers."""
    checks: dict[str, object] = {'database': 'error', 'schema_revision': None}
    http_status = status.HTTP_200_OK
    try:
        db.execute(text('SELECT 1'))
        checks['database'] = 'ok'
        checks['schema_revision'] = schema_revision()
        if checks['schema_revision'] != EXPECTED_SCHEMA_REVISION:
            checks['migrations'] = 'pending'
            http_status = status.HTTP_503_SERVICE_UNAVAILABLE
        else:
            checks['migrations'] = 'ok'
        latest_ingest = latest_system_event(db, 'ingest')
        latest_job = db.query(BackgroundJob).order_by(BackgroundJob.created_at.desc()).first()
        checks['last_ingest_at'] = latest_ingest.created_at.isoformat() if latest_ingest and latest_ingest.created_at else None
        checks['latest_job'] = {
            'id': latest_job.id,
            'type': latest_job.job_type,
            'status': latest_job.status,
            'heartbeat_at': latest_job.heartbeat_at.isoformat() if latest_job and latest_job.heartbeat_at else None,
        } if latest_job else None
    except Exception as exc:  # noqa: BLE001
        checks['error'] = f'{type(exc).__name__}: {exc}'
        http_status = status.HTTP_503_SERVICE_UNAVAILABLE
    payload = {'status': 'ok' if http_status == 200 else 'not_ready', **checks}
    return JSONResponse(payload, status_code=http_status)


@app.get('/', response_class=HTMLResponse, dependencies=[AuthDep])
def dashboard(
    request: Request,
    db: Session = DbDep,
    min_score: Optional[int] = None,
    profile_id: str = '',
    deadline_filter: str = 'active',
    deadline_from: str = '',
    deadline_to: str = '',
    match_type: str = 'all',
    page: int = 1,
    user_status: str = 'all',
    new_from_last_ingest: str = '',
    q: str = '',
    region: str = '',
    authority_region: str = '',
    rescore_done: str = '',
    ingest_done: str = '',
    ingest_warning: str = '',
    profile_warning: str = '',
    job_id: str = '',
    job_created: str = '',
    profile_created: str = '',
) -> HTMLResponse:
    current_user = current_user_from_request(request)
    settings = get_settings()
    profiles = _visible_profiles_query(db, current_user).order_by(ClientProfile.is_active.desc(), ClientProfile.name.asc()).all()
    active_profiles_count = sum(1 for p in profiles if p.is_active)
    selected_profile_id = _default_dashboard_profile_id(current_user, profiles, profile_id)
    selected_profile = _get_visible_profile(db, current_user, selected_profile_id) if selected_profile_id else None
    if selected_profile_id and selected_profile is None:
        raise HTTPException(status_code=404, detail='Profile not found')
    dashboard_mode_all = selected_profile_id is None
    match_type = match_type if match_type in DASHBOARD_MATCH_TYPES else 'all'
    deadline_from, deadline_from_date = _dashboard_date(deadline_from)
    deadline_to, deadline_to_date = _dashboard_date(deadline_to)
    normalized_filter_status = normalize_workflow_status(user_status) if user_status and user_status != 'all' else 'all'
    status_keeps_items_visible = normalized_filter_status in ('saved', 'reviewing', 'not_relevant')
    if min_score is None:
        # Every stored automatic row is a genuine CPV match. The default "Όλα"
        # view must therefore keep broad matches visible even when optional
        # profile criteria lower them below the old threshold of 55.
        min_score = 0
    if status_keeps_items_visible and deadline_filter == 'active':
        # When the user asks for a manual workflow list, do not hide it just because
        # the deadline passed or is unknown. Explicit deadline filters still apply.
        deadline_filter = 'all'
    summary = dashboard_summary(db, selected_profile_id, current_user)
    profile_summary = build_profile_summary(selected_profile)
    query = (
        db.query(TenderScore)
        .options(joinedload(TenderScore.tender), joinedload(TenderScore.profile).joinedload(ClientProfile.owner))
        .join(Tender)
    )
    if user_status == 'all':
        # Default flow should not keep showing items the user explicitly marked as irrelevant.
        # The score threshold is applied after CPV classification, so the dedicated
        # exact/child tabs can never lose genuine matches because another criterion
        # (for example budget) lowered their numeric score.
        query = query.filter(~TenderScore.user_status.in_(workflow_status_filter_values('not_relevant')))
    if selected_profile_id:
        query = query.filter(TenderScore.profile_id == selected_profile_id)
    else:
        query = _filter_scores_for_user(query, current_user)
    if new_from_last_ingest:
        query = query.filter(TenderScore.is_new_in_latest_ingest.is_(True))

    now = now_utc()
    if deadline_filter == 'active':
        query = query.filter(actionable_tender_clause(now))
    elif deadline_filter == 'expires_3':
        query = query.filter(Tender.cancelled.is_(False), Tender.final_submission_date >= now, Tender.final_submission_date <= now + timedelta(days=3))
    elif deadline_filter == 'expires_7':
        query = query.filter(Tender.cancelled.is_(False), Tender.final_submission_date >= now, Tender.final_submission_date <= now + timedelta(days=7))
    elif deadline_filter == 'expired':
        query = query.filter(expired_tender_clause(now))
    elif deadline_filter == 'cancelled':
        query = query.filter(Tender.cancelled.is_(True))
    elif deadline_filter == 'unknown':
        query = query.filter(Tender.cancelled.is_(False), Tender.final_submission_date.is_(None))

    if deadline_from_date is not None:
        query = query.filter(Tender.final_submission_date >= local_day_start(deadline_from_date))
    if deadline_to_date is not None:
        query = query.filter(Tender.final_submission_date <= local_day_end(deadline_to_date))

    if user_status and user_status != 'all':
        query = query.filter(TenderScore.user_status.in_(workflow_status_filter_values(user_status)))

    q_clean = q.strip()
    if q_clean:
        pattern = f'%{q_clean}%'
        query = query.filter(or_(Tender.title.ilike(pattern), Tender.organization_name.ilike(pattern), Tender.reference_number.ilike(pattern)))

    execution_clauses = region_filter_expressions(region)
    if execution_clauses:
        query = query.filter(or_(*execution_clauses))
    authority_clauses = region_filter_expressions(authority_region, authority=True)
    if authority_clauses:
        query = query.filter(or_(*authority_clauses))

    score_threshold_applies = user_status == 'all' or normalized_filter_status == 'new'
    category_rows = (
        query.with_entities(TenderScore.cpv_match_type, func.count(TenderScore.id))
        .group_by(TenderScore.cpv_match_type)
        .all()
    )
    match_counts = {'exact_full': 0, 'exact_partial': 0, 'broad': 0, 'none': 0}
    for category, count in category_rows:
        if category in match_counts:
            match_counts[category] = count

    all_query = query
    if score_threshold_applies:
        all_query = all_query.filter(TenderScore.score >= min_score)
    match_counts['all'] = all_query.count()

    page_query = query
    if match_type != 'all':
        page_query = page_query.filter(TenderScore.cpv_match_type == match_type)
    elif score_threshold_applies:
        page_query = page_query.filter(TenderScore.score >= min_score)
    total_results = page_query.count()
    total_pages = max(1, math.ceil(total_results / DASHBOARD_PAGE_SIZE))
    page = max(1, min(page, total_pages))
    start_index = (page - 1) * DASHBOARD_PAGE_SIZE
    category_order = case(
        (TenderScore.cpv_match_type == 'exact_full', 0),
        (TenderScore.cpv_match_type == 'exact_partial', 1),
        (TenderScore.cpv_match_type == 'broad', 2),
        else_=3,
    )
    scores = (
        page_query.order_by(category_order, TenderScore.score.desc(), Tender.final_submission_date.asc().nullslast())
        .offset(start_index)
        .limit(DASHBOARD_PAGE_SIZE)
        .all()
    )
    for score_row in scores:
        score_row.matched_cpv = _safe_list(score_row.matched_cpv)
        score_row.cpv_match = classify_cpv_match(score_row.tender, score_row.profile)
        score_row.preview_cpvs, score_row.overflow_cpvs = _split_cpv_preview(
            score_row.tender.cpv_codes,
            score_row.matched_cpv,
        )

    page_exact_full = [score for score in scores if score.cpv_match.is_full_exact]
    page_exact_partial = [score for score in scores if score.cpv_match.kind == 'exact' and not score.cpv_match.is_full_exact]
    page_broad = [score for score in scores if score.cpv_match.kind == 'broad']
    page_without_cpv_match = [score for score in scores if score.cpv_match.kind == 'none']
    score_groups = []
    if page_exact_full or page_exact_partial:
        exact_subgroups = []
        if page_exact_full:
            exact_subgroups.append({'key': 'exact-full', 'title': 'Ακριβές match', 'rows': page_exact_full})
        if page_exact_partial:
            exact_subgroups.append({'key': 'exact-partial', 'title': 'Μερικό match', 'rows': page_exact_partial})
        score_groups.append({
            'key': 'exact', 'title': 'Ακριβή CPV matches',
            'count': len(page_exact_full) + len(page_exact_partial), 'subgroups': exact_subgroups,
        })
    if page_broad:
        score_groups.append({
            'key': 'broad', 'title': 'Ευρύτερα / child CPV matches',
            'count': len(page_broad),
            'subgroups': [{'key': 'broad', 'title': 'Child / broad matches', 'rows': page_broad}],
        })
    if page_without_cpv_match:
        score_groups.append({
            'key': 'none', 'title': 'Χωρίς CPV match',
            'count': len(page_without_cpv_match),
            'subgroups': [{'key': 'none', 'title': 'Χωρίς αντιστοίχιση CPV', 'rows': page_without_cpv_match}],
        })
    match_filter_urls = {
        key: _dashboard_query_url(request, match_type=None if key == 'all' else key, page=None)
        for key in DASHBOARD_MATCH_TYPES
    }
    report_scope = 'latest_new' if new_from_last_ingest else ('all' if normalized_filter_status != 'all' else 'matches')
    report_params = {
        'profile_id': selected_profile_id or 0,
        'scope': report_scope,
        'match_type': match_type,
        'min_score': min_score,
        'deadline_filter': deadline_filter,
        'deadline_from': deadline_from,
        'deadline_to': deadline_to,
        'user_status': user_status,
        'q': q,
        'region': region,
        'authority_region': authority_region,
    }
    report_url = '/reports?' + urlencode({key: value for key, value in report_params.items() if value not in ('', None)})
    page_numbers = list(range(max(1, page - 2), min(total_pages, page + 2) + 1))
    pagination = {
        'page': page,
        'total_pages': total_pages,
        'total_results': total_results,
        'start': start_index + 1 if total_results else 0,
        'end': min(start_index + DASHBOARD_PAGE_SIZE, total_results),
        'previous_url': _dashboard_query_url(request, page=page - 1) if page > 1 else None,
        'next_url': _dashboard_query_url(request, page=page + 1) if page < total_pages else None,
        'page_urls': {number: _dashboard_query_url(request, page=number) for number in page_numbers},
    }
    active_filters: list[dict[str, str]] = []
    if min_score > 0:
        active_filters.append({'label': f'Score ≥ {min_score}', 'url': _dashboard_query_url(request, min_score=0, page=None)})
    # "active" is the normal dashboard view, not a filter the user applied.
    # Every alternative (including "all") is removable back to that default.
    if deadline_filter != 'active':
        active_filters.append({'label': f'Προθεσμία: {DEADLINE_FILTERS.get(deadline_filter, deadline_filter)}', 'url': _dashboard_query_url(request, deadline_filter='active', page=None)})
    if deadline_from:
        active_filters.append({'label': f'Λήξη από {deadline_from}', 'url': _dashboard_query_url(request, deadline_from=None, page=None)})
    if deadline_to:
        active_filters.append({'label': f'Λήξη έως {deadline_to}', 'url': _dashboard_query_url(request, deadline_to=None, page=None)})
    if normalized_filter_status != 'all':
        active_filters.append({'label': f'Κατάσταση: {workflow_status_label(normalized_filter_status)}', 'url': _dashboard_query_url(request, user_status='all', page=None)})
    if new_from_last_ingest:
        active_filters.append({'label': 'Νέα τελευταίας εισαγωγής', 'url': _dashboard_query_url(request, new_from_last_ingest=None, page=None)})
    if region:
        active_filters.append({'label': f'Τόπος: {region}', 'url': _dashboard_query_url(request, region=None, page=None)})
    if authority_region:
        active_filters.append({'label': f'Έδρα: {authority_region}', 'url': _dashboard_query_url(request, authority_region=None, page=None)})
    if q_clean:
        active_filters.append({'label': f'Αναζήτηση: {q_clean}', 'url': _dashboard_query_url(request, q=None, page=None)})
    return templates.TemplateResponse(
        'dashboard.html',
        {
            'request': request,
            'scores': scores,
            'score_groups': score_groups,
            'match_counts': match_counts,
            'match_filter_urls': match_filter_urls,
            'report_url': report_url,
            'pagination': pagination,
            'active_filters': active_filters,
            'profiles': profiles,
            'settings': settings,
            'min_score': min_score,
            'profile_id': selected_profile_id,
            'deadline_filter': deadline_filter,
            'deadline_from': deadline_from,
            'deadline_to': deadline_to,
            'match_type': match_type,
            'user_status': user_status,
            'new_from_last_ingest': new_from_last_ingest,
            'q': q,
            'region': region,
            'authority_region': authority_region,
            'summary': summary,
            'selected_profile': selected_profile,
            'dashboard_mode_all': dashboard_mode_all,
            'profile_summary': profile_summary,
            'nuts_options_grouped': nuts_options_grouped(),
            'rescore_done': rescore_done,
            'ingest_done': ingest_done,
            'ingest_warning': ingest_warning,
            'profile_warning': profile_warning,
            'job_id': job_id,
            'job_created': job_created,
            'profile_created': profile_created,
            'active_profiles_count': active_profiles_count,
        },
    )


@app.post('/ingest/run', dependencies=[AuthDep])
def run_ingest_now(
    request: Request,
    db: Session = DbDep,
    days: int = Form(3),
    profile_id: str = Form('0'),
    return_to: str = Form('/'),
) -> RedirectResponse:
    current_user = current_user_from_request(request)
    selected_profile_id = _parse_int(profile_id)
    if selected_profile_id is None and not current_user.is_admin:
        raise HTTPException(status_code=400, detail='Profile is required')
    if selected_profile_id and _get_visible_profile(db, current_user, selected_profile_id) is None:
        raise HTTPException(status_code=404, detail='Profile not found')
    job, created = enqueue_job(
        db,
        job_type='ingest',
        profile_id=selected_profile_id,
        requested_by_user_id=current_user.id,
        payload={'days': max(1, min(int(days), 180)), 'manual': True},
    )
    log_event(
        db,
        'background_job_queued' if created else 'background_job_duplicate',
        'Προγραμματίστηκε εισαγωγή δεδομένων' if created else 'Υπάρχει ήδη ενεργή εισαγωγή',
        job.id,
        {'job_id': job.id, 'profile_id': selected_profile_id, 'days': days},
    )
    db.commit()
    safe_return = _safe_return_url(return_to)
    separator = '&' if '?' in safe_return else '?'
    return RedirectResponse(url=f'{safe_return}{separator}job_id={job.id}&job_created={1 if created else 0}', status_code=status.HTTP_303_SEE_OTHER)


@app.post('/rescore/run', dependencies=[AuthDep])
def run_rescore_now(
    request: Request,
    db: Session = DbDep,
    profile_id: str = Form('0'),
    return_to: str = Form('/'),
) -> RedirectResponse:
    current_user = current_user_from_request(request)
    selected_profile_id = _parse_int(profile_id)
    if selected_profile_id is None and not current_user.is_admin:
        raise HTTPException(status_code=400, detail='Profile is required')
    if selected_profile_id and _get_visible_profile(db, current_user, selected_profile_id) is None:
        raise HTTPException(status_code=404, detail='Profile not found')
    job, created = enqueue_job(
        db,
        job_type='rescore',
        profile_id=selected_profile_id,
        requested_by_user_id=current_user.id,
        payload={'manual': True},
    )
    log_event(db, 'background_job_queued' if created else 'background_job_duplicate', 'Προγραμματίστηκε επαναβαθμολόγηση', job.id, {'job_id': job.id, 'profile_id': selected_profile_id})
    db.commit()
    safe_return = _safe_return_url(return_to)
    separator = '&' if '?' in safe_return else '?'
    return RedirectResponse(url=f'{safe_return}{separator}job_id={job.id}&job_created={1 if created else 0}', status_code=status.HTTP_303_SEE_OTHER)


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
    cancel_date_from: str = '',
    cancel_date_to: str = '',
    signer: str = '',
    aaht: str = '',
    public_funding_ref_num: str = '',
    modified_only: str = '',
    active_only: str = '',
    max_pages: int = 1,
    search: str = '',
    profile_id: str = '',
):
    current_user = current_user_from_request(request)
    date_from = normalize_date_input(date_from)
    date_to = normalize_date_input(date_to)
    final_date_from = normalize_date_input(final_date_from)
    final_date_to = normalize_date_input(final_date_to)
    cancel_date_from = normalize_date_input(cancel_date_from)
    cancel_date_to = normalize_date_input(cancel_date_to)

    profiles = _visible_profiles_query(db, current_user).order_by(ClientProfile.is_active.desc(), ClientProfile.name.asc()).all()
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
        cancel_date_from.strip(), cancel_date_to.strip(), signer.strip(), aaht.strip(),
        public_funding_ref_num.strip(), modified_only == 'on',
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
        # Ο ΑΔΑΜ περιορίζει την αναζήτηση στο αντίστοιχο υποστηριζόμενο endpoint.
        inferred_resource = infer_resource_from_reference_number(reference_number)
        if inferred_resource:
            if inferred_resource not in OPERATION_TYPES:
                resources = []
                warnings.append('Ο ΑΔΑΜ ανήκει σε ιστορική πράξη αγοράς, η οποία δεν υποστηρίζεται πλέον από την εφαρμογή.')
            elif inferred_resource in resources:
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
                cancel_date_from=cancel_date_from,
                cancel_date_to=cancel_date_to,
                signer=signer,
                aaht=aaht,
                public_funding_ref_num=public_funding_ref_num,
                is_modified=True if modified_only == 'on' else False,
                include_final_dates=(res == 'notice'),
            )
            # User friendly mode: final dates only make sense for notices.
            if res != 'notice' and (final_date_from or final_date_to or active_only == 'on'):
                warnings.append(f"Το φίλτρο καταληκτικής ημερομηνίας αγνοήθηκε για {OPERATION_TYPES.get(res, {}).get('label', res)}.")
            try:
                raw_records = client.search_resource(
                    res,
                    body,
                    max_pages=max_pages_safe,
                    timeout_seconds=client.settings.khmdhs_interactive_timeout_seconds,
                    transport_retries=client.settings.khmdhs_interactive_transport_retries,
                    rate_limit_retries=0,
                )
            except Exception as exc:
                resource_errors.append(f"{OPERATION_TYPES.get(res, {}).get('label', res)}: {exc}")
                continue
            if client.last_transient_error:
                resource_errors.append(
                    f"{OPERATION_TYPES.get(res, {}).get('label', res)}: το ΚΗΜΔΗΣ δεν απάντησε έγκαιρα. "
                    "Δοκιμάστε ξανά σε λίγο."
                )
            elif client.last_rate_limited:
                resource_errors.append(
                    f"{OPERATION_TYPES.get(res, {}).get('label', res)}: το ΚΗΜΔΗΣ έβαλε προσωρινό όριο αιτημάτων. "
                    "Δοκιμάστε ξανά σε λίγο."
                )
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
            'cancel_date_from': cancel_date_from,
            'cancel_date_to': cancel_date_to,
            'signer': signer,
            'aaht': aaht,
            'public_funding_ref_num': public_funding_ref_num,
            'modified_only': modified_only,
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
    request: Request,
    resource: str = Form(...),
    reference_number: str = Form(...),
    profile_id: str = Form(...),
    return_to: str = Form('/kimdis'),
    db: Session = DbDep,
) -> RedirectResponse:
    current_user = current_user_from_request(request)
    if resource not in OPERATION_TYPES or not reference_number.strip():
        raise HTTPException(status_code=400, detail='Invalid KIMDIS resource/reference')
    selected_profile_id = _parse_int(profile_id)
    if selected_profile_id is None:
        raise HTTPException(status_code=400, detail='Πρέπει να επιλέξετε προφίλ αποθήκευσης/βαθμολόγησης.')
    profile = _get_visible_profile(db, current_user, selected_profile_id, active_only=True)
    if profile is None:
        raise HTTPException(status_code=404, detail='Το επιλεγμένο προφίλ δεν βρέθηκε ή δεν είναι ενεργό.')

    client = KhmdhsClient()
    body = build_search_body(resource=resource, reference_number=reference_number.strip(), include_final_dates=(resource == 'notice'))
    records = client.search_resource(
        resource,
        body,
        max_pages=1,
        timeout_seconds=client.settings.khmdhs_interactive_timeout_seconds,
        transport_retries=client.settings.khmdhs_interactive_transport_retries,
        rate_limit_retries=0,
    )
    raw = next((r for r in records if str(r.get('referenceNumber')) == reference_number.strip()), records[0] if records else None)
    if raw is None:
        raise HTTPException(status_code=404, detail='KIMDIS record not found')
    tender = upsert_tender(db, client.normalize_record(resource, raw))
    # General search is an explicit user save, so preserve it even when it does not
    # match the profile CPV automatically.
    score = score_and_store(db, tender, profile, store_zero_score=True)
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
    request: Request,
    score_id: int,
    user_status: str = Form(...),
    user_notes: Optional[str] = Form(None),
    return_to: str = Form('/'),
    db: Session = DbDep,
) -> RedirectResponse:
    current_user = current_user_from_request(request)
    score = db.query(TenderScore).filter(TenderScore.id == score_id).one_or_none()
    if score is None:
        raise HTTPException(status_code=404, detail='Score not found')
    if not current_user.is_admin and _get_visible_profile(db, current_user, score.profile_id) is None:
        raise HTTPException(status_code=404, detail='Score not found')
    normalized_status = normalize_workflow_status(user_status)
    if normalized_status not in WORKFLOW_STATUSES:
        raise HTTPException(status_code=400, detail='Invalid workflow status')
    score.user_status = normalized_status
    if user_notes is not None:
        score.user_notes = user_notes.strip() or None
    score.status_updated_at = now_utc()
    db.commit()
    return RedirectResponse(url=_safe_return_url(return_to), status_code=status.HTTP_303_SEE_OTHER)


@app.post('/tenders/{tender_id}/delete', dependencies=[AuthDep])
def delete_tender(
    request: Request,
    tender_id: int,
    return_to: str = Form('/'),
    db: Session = DbDep,
) -> RedirectResponse:
    current_user = current_user_from_request(request)
    tender = db.query(Tender).options(joinedload(Tender.scores).joinedload(TenderScore.profile)).filter(Tender.id == tender_id).one_or_none()
    if tender is None:
        raise HTTPException(status_code=404, detail='Tender not found')

    reference = tender.reference_number or tender.source_reference
    title = tender.title or ''
    deleted_scope = 'tender'
    deleted_score_ids: list[int] = []
    if not current_user.is_admin:
        owned_scores = [
            score for score in (tender.scores or [])
            if score.profile is not None and score.profile.owner_user_id == current_user.id
        ]
        if not owned_scores:
            raise HTTPException(status_code=404, detail='Tender not found')
        deleted_scope = 'user_scores'
        deleted_score_ids = [score.id for score in owned_scores]
        for score in owned_scores:
            db.delete(score)
        db.flush()
        remaining_scores = db.query(TenderScore.id).filter(TenderScore.tender_id == tender.id).first()
        if remaining_scores is None:
            deleted_scope = 'tender'
            db.delete(tender)
    else:
        deleted_score_ids = [score.id for score in (tender.scores or [])]
        db.delete(tender)

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
            'deleted_scope': deleted_scope,
            'deleted_score_ids': deleted_score_ids,
            'user_id': current_user.id,
        },
    )
    db.commit()

    safe_return = _safe_return_url(return_to)
    return RedirectResponse(url=safe_return, status_code=status.HTTP_303_SEE_OTHER)


@app.get('/tenders/{tender_id}', response_class=HTMLResponse, dependencies=[AuthDep])
def tender_detail(request: Request, tender_id: int, db: Session = DbDep, timeline: int = 0, profile_id: str = '', diavgeia_refreshed: str = '', diavgeia_error: str = '', job_id: str = '', job_created: str = '') -> HTMLResponse:
    current_user = current_user_from_request(request)
    tender = db.query(Tender).options(joinedload(Tender.scores).joinedload(TenderScore.profile)).filter(Tender.id == tender_id).one_or_none()
    if tender is None:
        raise HTTPException(status_code=404, detail='Tender not found')
    selected_profile_id = _parse_int(profile_id)
    raw_scores = [score for score in (tender.scores or []) if score.profile is not None and (current_user.is_admin or score.profile.owner_user_id == current_user.id)]
    if not raw_scores:
        raise HTTPException(status_code=404, detail='Tender not found')
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
        chain_items, chain_error = load_tender_chain(tender)
    diavgeia_decisions = (
        db.query(DiavgeiaDecision)
        .filter(DiavgeiaDecision.tender_id == tender.id, DiavgeiaDecision.is_current.is_(True))
        .order_by(DiavgeiaDecision.issue_date.desc(), DiavgeiaDecision.id.desc())
        .all()
    )
    tender_changes = (
        db.query(TenderChange)
        .filter(TenderChange.tender_id == tender.id)
        .order_by(TenderChange.detected_at.desc(), TenderChange.id.desc())
        .limit(50)
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
            'chain_items': chain_items,
            'chain_error': chain_error,
            'timeline_checked': timeline_checked,
            'selected_profile_id': selected_profile_id,
            'display_scores': display_scores,
            'diavgeia_decisions': diavgeia_decisions,
            'tender_changes': tender_changes,
            'diavgeia_message': diavgeia_message,
            'job_id': job_id,
            'job_created': job_created,
        },
    )


@app.post('/tenders/{tender_id}/diavgeia-refresh', dependencies=[AuthDep])
def refresh_tender_diavgeia(
    request: Request,
    tender_id: int,
    db: Session = DbDep,
    profile_id: str = Form(''),
) -> RedirectResponse:
    current_user = current_user_from_request(request)
    tender = db.query(Tender).filter(Tender.id == tender_id).one_or_none()
    if tender is None:
        raise HTTPException(status_code=404, detail='Tender not found')
    if not current_user.is_admin:
        visible = (
            db.query(TenderScore)
            .join(ClientProfile)
            .filter(TenderScore.tender_id == tender_id, ClientProfile.owner_user_id == current_user.id)
            .first()
        )
        if visible is None:
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
def analyze_tender_pdf(request: Request, tender_id: int, db: Session = DbDep) -> RedirectResponse:
    current_user = current_user_from_request(request)
    tender = db.query(Tender).options(joinedload(Tender.scores).joinedload(TenderScore.profile)).filter(Tender.id == tender_id).one_or_none()
    if tender is None:
        raise HTTPException(status_code=404, detail='Tender not found')
    if _visible_tender_score_query(db, current_user, tender_id).first() is None:
        raise HTTPException(status_code=404, detail='Tender not found')
    if not tender.attachment_url:
        raise HTTPException(status_code=400, detail='Δεν υπάρχει διαθέσιμο PDF για αυτή την πράξη.')
    profile_ids = sorted({
        score.profile_id
        for score in tender.scores
        if score.profile is not None and (current_user.is_admin or score.profile.owner_user_id == current_user.id)
    })
    if not profile_ids:
        profile_ids = [row[0] for row in _visible_profiles_query(db, current_user).filter(ClientProfile.is_active.is_(True)).with_entities(ClientProfile.id).all()]
    job, created = enqueue_job(
        db,
        job_type='pdf_analysis',
        requested_by_user_id=current_user.id,
        payload={'tender_id': tender.id, 'profile_ids': profile_ids},
    )
    log_event(
        db,
        event_type='background_job_queued' if created else 'background_job_duplicate',
        title='Προγραμματίστηκε ανάλυση PDF',
        message=f'{tender.reference_number or tender.source_reference} — {tender.title[:160]}',
        payload={'tender_id': tender.id, 'reference_number': tender.reference_number, 'job_id': job.id},
    )
    db.commit()
    selected_profile_id = profile_ids[0] if len(profile_ids) == 1 else ''
    return RedirectResponse(
        url=f'/tenders/{tender.id}?profile_id={selected_profile_id}&job_id={job.id}&job_created={1 if created else 0}',
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get('/profiles', response_class=HTMLResponse, dependencies=[AuthDep])
def profiles_list(request: Request, db: Session = DbDep, profile_warning: str = '') -> HTMLResponse:
    current_user = current_user_from_request(request)
    profiles = _visible_profiles_query(db, current_user).order_by(ClientProfile.name.asc()).all()
    active_profiles_count = sum(1 for p in profiles if p.is_active)
    profile_summaries = {p.id: build_profile_summary(p) for p in profiles}
    return templates.TemplateResponse(
        'profiles.html',
        {
            'request': request,
            'profiles': profiles,
            'profile_summaries': profile_summaries,
            'profile_warning': profile_warning,
            'active_profiles_count': active_profiles_count,
        },
    )


@app.get('/profiles/new', response_class=HTMLResponse, dependencies=[AuthDep])
def profile_new(request: Request, db: Session = DbDep, cpv_q: str = '', cpv_category: str = '') -> HTMLResponse:
    current_user = current_user_from_request(request)
    profile = ClientProfile(slug='', name='', description='', cpv_codes=[], cpv_prefixes=[], keywords=[], negative_keywords=[], required_certificates=[], preferred_regions=[], rss_feeds=[], is_active=True)
    if current_user.id:
        profile.owner_user_id = current_user.id
    owners = db.query(AppUser).filter(AppUser.is_active.is_(True)).order_by(AppUser.username.asc()).all() if current_user.is_admin else []
    return templates.TemplateResponse('profile_form.html', _profile_form_context(request, profile, 'new', cpv_q=cpv_q, cpv_category=cpv_category, owners=owners))


@app.get('/profiles/{profile_id}/edit', response_class=HTMLResponse, dependencies=[AuthDep])
def profile_edit(request: Request, profile_id: int, db: Session = DbDep, cpv_q: str = '', cpv_category: str = '') -> HTMLResponse:
    current_user = current_user_from_request(request)
    profile = _get_visible_profile(db, current_user, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail='Profile not found')
    owners = db.query(AppUser).filter(AppUser.is_active.is_(True)).order_by(AppUser.username.asc()).all() if current_user.is_admin else []
    return templates.TemplateResponse('profile_form.html', _profile_form_context(request, profile, 'edit', cpv_q=cpv_q, cpv_category=cpv_category, owners=owners))


def _save_profile_from_form(
    profile: ClientProfile,
    slug: str,
    name: str,
    description: str,
    cpv_codes: str,
    cpv_prefixes: str,
    required_certificates: str,
    preferred_regions: str | list[str],
    min_budget: str,
    max_budget: str,
    is_active: Optional[str],
) -> None:
    profile.name = name.strip()
    profile.slug = _slugify(slug or profile.name)
    profile.description = description.strip()
    profile.cpv_codes = _split_lines(cpv_codes)
    # Οι οικογένειες CPV υπολογίζονται από τους επιλεγμένους κωδικούς.
    # Δεν βασιζόμαστε στο hidden field/JS ώστε να είναι σωστό και σε manual POST.
    profile.cpv_prefixes = cpv_prefixes_for_codes(profile.cpv_codes)
    # Keyword-based profile scoring has been retired. Clear legacy values when
    # an existing profile is saved so they cannot affect older code paths.
    profile.keywords = []
    profile.negative_keywords = []
    profile.required_certificates = _split_lines(required_certificates)
    profile.preferred_regions = _split_lines(preferred_regions)
    profile.min_budget = _parse_float(min_budget)
    profile.max_budget = _parse_float(max_budget)
    # RSS ingestion was retired in favor of verified Διαύγεια API enrichment.
    profile.rss_feeds = []
    profile.is_active = is_active == 'on'


@app.post('/profiles', response_class=HTMLResponse, dependencies=[AuthDep])
def profile_create(
    request: Request,
    slug: str = Form(''),
    name: str = Form(...),
    description: str = Form(''),
    cpv_codes: str = Form(''),
    cpv_prefixes: str = Form(''),
    required_certificates: str = Form(''),
    preferred_regions: list[str] = Form([]),
    min_budget: str = Form(''),
    max_budget: str = Form(''),
    is_active: Optional[str] = Form(None),
    owner_user_id: str = Form(''),
    db: Session = DbDep,
):
    current_user = current_user_from_request(request)
    profile = ClientProfile(slug=slug.strip(), name=name.strip())
    if current_user.id:
        profile.owner_user_id = current_user.id
    requested_owner_id = _parse_int(owner_user_id)
    if current_user.is_admin and requested_owner_id:
        owner = db.query(AppUser).filter(AppUser.id == requested_owner_id, AppUser.is_active.is_(True)).one_or_none()
        if owner is None:
            return templates.TemplateResponse('profile_form.html', _profile_form_context(request, profile, 'new', 'Invalid owner.'))
        profile.owner_user_id = owner.id
    _save_profile_from_form(profile, slug, name, description, cpv_codes, cpv_prefixes, required_certificates, preferred_regions, min_budget, max_budget, is_active)
    if not profile.name:
        return templates.TemplateResponse('profile_form.html', _profile_form_context(request, profile, 'new', 'Συμπληρώστε όνομα προφίλ.'))
    validation_errors = _validate_profile_values(cpv_codes, min_budget, max_budget, is_active)
    if validation_errors:
        owners = db.query(AppUser).filter(AppUser.is_active.is_(True)).order_by(AppUser.username.asc()).all() if current_user.is_admin else []
        return templates.TemplateResponse(
            'profile_form.html',
            _profile_form_context(
                request, profile, 'new', ' '.join(validation_errors), owners=owners,
                form_values=_profile_form_values(min_budget, max_budget),
            ),
            status_code=422,
        )
    if db.query(ClientProfile).filter(ClientProfile.slug == profile.slug).first():
        return templates.TemplateResponse('profile_form.html', _profile_form_context(request, profile, 'new', 'Υπάρχει ήδη profile με αυτό το slug.'))
    db.add(profile)
    db.flush()
    log_event(db, 'profile_created', 'Δημιουργήθηκε νέο προφίλ', f'{profile.name} ({profile.slug})', {'profile_slug': profile.slug})
    job = None
    if profile.is_active and profile.cpv_codes:
        job, _ = enqueue_job(
            db,
            job_type='rescore',
            profile_id=profile.id,
            requested_by_user_id=current_user.id,
            payload={'after_profile_create': True},
        )
        log_event(db, 'background_job_queued', 'Προγραμματίστηκε αρχική βαθμολόγηση προφίλ', job.id, {'profile_id': profile.id, 'job_id': job.id})
    db.commit()
    target = f'/?profile_id={profile.id}&profile_created=1'
    if job is not None:
        target += f'&job_id={job.id}&job_created=1'
    return RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)


@app.post('/profiles/{profile_id}', response_class=HTMLResponse, dependencies=[AuthDep])
def profile_update(
    request: Request,
    profile_id: int,
    slug: str = Form(''),
    name: str = Form(...),
    description: str = Form(''),
    cpv_codes: str = Form(''),
    cpv_prefixes: str = Form(''),
    required_certificates: str = Form(''),
    preferred_regions: list[str] = Form([]),
    min_budget: str = Form(''),
    max_budget: str = Form(''),
    is_active: Optional[str] = Form(None),
    owner_user_id: str = Form(''),
    db: Session = DbDep,
):
    current_user = current_user_from_request(request)
    profile = _get_visible_profile(db, current_user, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail='Profile not found')
    old_slug = profile.slug
    old_scoring_signature = _profile_scoring_signature(profile)
    requested_owner_id = _parse_int(owner_user_id)
    if current_user.is_admin and requested_owner_id:
        owner = db.query(AppUser).filter(AppUser.id == requested_owner_id, AppUser.is_active.is_(True)).one_or_none()
        if owner is None:
            owners = db.query(AppUser).filter(AppUser.is_active.is_(True)).order_by(AppUser.username.asc()).all()
            return templates.TemplateResponse('profile_form.html', _profile_form_context(request, profile, 'edit', 'Invalid owner.', owners=owners))
        profile.owner_user_id = owner.id
    _save_profile_from_form(profile, slug, name, description, cpv_codes, cpv_prefixes, required_certificates, preferred_regions, min_budget, max_budget, is_active)
    if not profile.name:
        return templates.TemplateResponse('profile_form.html', _profile_form_context(request, profile, 'edit', 'Συμπληρώστε όνομα προφίλ.'))
    validation_errors = _validate_profile_values(cpv_codes, min_budget, max_budget, is_active)
    if validation_errors:
        owners = db.query(AppUser).filter(AppUser.is_active.is_(True)).order_by(AppUser.username.asc()).all() if current_user.is_admin else []
        return templates.TemplateResponse(
            'profile_form.html',
            _profile_form_context(
                request, profile, 'edit', ' '.join(validation_errors), owners=owners,
                form_values=_profile_form_values(min_budget, max_budget),
            ),
            status_code=422,
        )
    duplicate = db.query(ClientProfile).filter(ClientProfile.slug == profile.slug, ClientProfile.id != profile.id).first()
    if duplicate:
        profile.slug = old_slug
        return templates.TemplateResponse('profile_form.html', _profile_form_context(request, profile, 'edit', 'Υπάρχει ήδη profile με αυτό το slug.'))
    log_event(db, 'profile_updated', 'Ενημερώθηκε προφίλ', f'{profile.name} ({profile.slug})', {'profile_id': profile.id})
    # Recalculate automatically only when a field that participates in scoring
    # changes. Name/description edits stay instant and do not create needless jobs.
    if old_scoring_signature != _profile_scoring_signature(profile):
        db.flush()
        job, _ = enqueue_job(db, job_type='rescore', profile_id=profile.id, requested_by_user_id=current_user.id, payload={'after_profile_save': True})
        log_event(db, 'background_job_queued', 'Προγραμματίστηκε επαναβαθμολόγηση προφίλ', job.id, {'profile_id': profile.id, 'job_id': job.id})
    db.commit()
    return RedirectResponse(url='/profiles', status_code=status.HTTP_303_SEE_OTHER)


@app.post('/profiles/{profile_id}/toggle', dependencies=[AuthDep])
def profile_toggle(request: Request, profile_id: int, db: Session = DbDep) -> RedirectResponse:
    current_user = current_user_from_request(request)
    profile = _get_visible_profile(db, current_user, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail='Profile not found')
    profile.is_active = not profile.is_active
    log_event(db, 'profile_toggled', 'Άλλαξε κατάσταση προφίλ', f"{profile.name}: {'ενεργό' if profile.is_active else 'ανενεργό'}", {'profile_id': profile.id, 'is_active': profile.is_active})
    db.flush()
    active_count = _visible_profiles_query(db, current_user).filter(ClientProfile.is_active.is_(True)).count()
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
def profile_delete(request: Request, profile_id: int, db: Session = DbDep) -> RedirectResponse:
    current_user = current_user_from_request(request)
    profile = _get_visible_profile(db, current_user, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail='Profile not found')
    total_profiles = _visible_profiles_query(db, current_user).count()
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
    min_score: int = 0,
    scope: str = 'matches',
    match_type: str = 'all',
    deadline_filter: str = '',
    deadline_from: str = '',
    deadline_to: str = '',
    user_status: str = 'all',
    active_only: str = '',
    q: str = '',
    region: str = '',
    authority_region: str = '',
    rescore_done: str = '',
    ingest_done: str = '',
    ingest_warning: str = '',
) -> HTMLResponse:
    current_user = current_user_from_request(request)
    date_from = normalize_date_input(date_from)
    date_to = normalize_date_input(date_to)
    deadline_from = normalize_date_input(deadline_from)
    deadline_to = normalize_date_input(deadline_to)
    if scope == 'new':
        scope = 'latest_new'
    if scope not in {'matches', 'latest_new', 'shortlist', 'all'}:
        scope = 'matches'
    match_type = match_type if match_type in DASHBOARD_MATCH_TYPES else 'all'
    if not deadline_filter:
        deadline_filter = 'all' if active_only == 'off' else 'active'
    deadline_filter = deadline_filter if deadline_filter in DEADLINE_FILTERS else 'active'
    user_status = user_status if user_status in {'all', *WORKFLOW_STATUSES.keys()} else 'all'
    min_score = max(0, min(100, min_score))
    # Reports start without an implicit period. Empty dates mean "all stored KIMDIS records",
    # so the numbers are easier to compare with the dashboard unless the user narrows them.
    selected_profile_id = _parse_int(profile_id)
    profiles = _visible_profiles_query(db, current_user).order_by(ClientProfile.is_active.desc(), ClientProfile.name.asc()).all()
    if selected_profile_id is None and profile_id in ('', None) and profiles:
        first_active = next((p for p in profiles if p.is_active), profiles[0])
        selected_profile_id = first_active.id
    profile = _get_visible_profile(db, current_user, selected_profile_id) if selected_profile_id else None
    if selected_profile_id and selected_profile_id > 0 and profile is None:
        raise HTTPException(status_code=404, detail='Profile not found')
    filters = ReportFilters(
        date_from=date_from,
        date_to=date_to,
        profile_id=selected_profile_id,
        profile_ids=None if current_user.is_admin or selected_profile_id else _visible_profile_ids(db, current_user),
        min_score=min_score,
        scope=scope,
        match_type=match_type,
        active_only=deadline_filter == 'active',
        deadline_filter=deadline_filter,
        deadline_from=deadline_from,
        deadline_to=deadline_to,
        user_status=user_status,
        q=q,
        region=region,
        authority_region=authority_region,
    )
    report_scores = query_report_scores(db, filters)
    scores = report_scores[:100]
    for score_row in scores:
        score_row.matched_cpv = _safe_list(score_row.matched_cpv)
        score_row.preview_cpvs, score_row.overflow_cpvs = _split_cpv_preview(
            score_row.tender.cpv_codes,
            score_row.matched_cpv,
        )
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
            'match_type': match_type,
            'match_type_label': report_match_label(match_type),
            'deadline_filter': deadline_filter,
            'deadline_from': deadline_from,
            'deadline_to': deadline_to,
            'user_status': user_status,
            'q': q,
            'region': region,
            'authority_region': authority_region,
            'selected_profile': profile,
            'summary': summary,
            'report_total': len(report_scores),
            'scope_label': report_scope_label(scope),
            'period_label': report_period_label(filters),
            'deadline_scope_label': DEADLINE_FILTERS[deadline_filter],
            'nuts_options_grouped': nuts_options_grouped(),
            'rescore_done': rescore_done,
            'ingest_done': ingest_done,
            'ingest_warning': ingest_warning,
        },
    )


@app.get('/reports/export', dependencies=[AuthDep])
def reports_export(
    request: Request,
    db: Session = DbDep,
    date_from: str = '',
    date_to: str = '',
    profile_id: str = '',
    min_score: int = 0,
    scope: str = 'matches',
    match_type: str = 'all',
    deadline_filter: str = '',
    deadline_from: str = '',
    deadline_to: str = '',
    user_status: str = 'all',
    active_only: str = '',
    q: str = '',
    region: str = '',
    authority_region: str = '',
    format: str = 'pdf',
    include_pdf_text: str = 'off',
):
    current_user = current_user_from_request(request)
    date_from = normalize_date_input(date_from)
    date_to = normalize_date_input(date_to)
    deadline_from = normalize_date_input(deadline_from)
    deadline_to = normalize_date_input(deadline_to)
    if scope == 'new':
        scope = 'latest_new'
    if scope not in {'matches', 'latest_new', 'shortlist', 'all'}:
        scope = 'matches'
    match_type = match_type if match_type in DASHBOARD_MATCH_TYPES else 'all'
    if not deadline_filter:
        deadline_filter = 'all' if active_only == 'off' else 'active'
    deadline_filter = deadline_filter if deadline_filter in DEADLINE_FILTERS else 'active'
    user_status = user_status if user_status in {'all', *WORKFLOW_STATUSES.keys()} else 'all'
    min_score = max(0, min(100, min_score))
    selected_profile_id = _parse_int(profile_id)
    if selected_profile_id is None and profile_id in ('', None):
        first_profile = _visible_profiles_query(db, current_user).filter(ClientProfile.is_active.is_(True)).order_by(ClientProfile.name.asc()).first()
        selected_profile_id = first_profile.id if first_profile else None
    profile = _get_visible_profile(db, current_user, selected_profile_id) if selected_profile_id else None
    if selected_profile_id and selected_profile_id > 0 and profile is None:
        raise HTTPException(status_code=404, detail='Profile not found')
    filters = ReportFilters(
        date_from=date_from,
        date_to=date_to,
        profile_id=selected_profile_id,
        profile_ids=None if current_user.is_admin or selected_profile_id else _visible_profile_ids(db, current_user),
        min_score=min_score,
        scope=scope,
        match_type=match_type,
        active_only=deadline_filter == 'active',
        deadline_filter=deadline_filter,
        deadline_from=deadline_from,
        deadline_to=deadline_to,
        user_status=user_status,
        q=q,
        region=region,
        authority_region=authority_region,
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
    if format == 'pdf_urls':
        return make_pdf_urls_response(scores, f'{stem}_pdf_urls.txt')
    include_pdf = include_pdf_text == 'on'
    md = report_to_markdown(scores, filters, profile, include_pdf_text=include_pdf)
    if format == 'md':
        return make_markdown_response(md, f'{stem}.md')
    return make_pdf_response('Αναφορά διαγωνισμών', md, f'{stem}.pdf')


@app.get('/profiles/{profile_id}/export', dependencies=[AuthDep])
def profile_export(request: Request, profile_id: int, format: str = 'pdf', db: Session = DbDep):
    current_user = current_user_from_request(request)
    profile = _get_visible_profile(db, current_user, profile_id)
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
    current_user = current_user_from_request(request)
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail='Admin access required')
    stats = database_usage_summary(db)
    return templates.TemplateResponse('maintenance.html', {'request': request, 'stats': stats})


@app.get('/activity', response_class=HTMLResponse, dependencies=[AuthDep])
def activity_log(request: Request, db: Session = DbDep) -> HTMLResponse:
    current_user = current_user_from_request(request)
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail='Admin access required')
    events = db.query(SystemEvent).order_by(SystemEvent.created_at.desc()).limit(200).all()
    return templates.TemplateResponse('activity.html', {'request': request, 'events': events})


@app.get('/jobs', response_class=HTMLResponse, dependencies=[AuthDep])
def jobs_page(request: Request, db: Session = DbDep) -> HTMLResponse:
    current_user = current_user_from_request(request)
    jobs = _visible_jobs_query(db, current_user).order_by(BackgroundJob.created_at.desc()).limit(100).all()
    active_jobs = sum(1 for job in jobs if job.status in ('queued', 'running'))
    return templates.TemplateResponse('jobs.html', {'request': request, 'jobs': jobs, 'active_jobs': active_jobs})


@app.get('/api/jobs/{job_id}', dependencies=[AuthDep])
def api_job_status(request: Request, job_id: str, db: Session = DbDep) -> dict[str, object]:
    current_user = current_user_from_request(request)
    job = _visible_jobs_query(db, current_user).filter(BackgroundJob.id == job_id).one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail='Job not found')
    return {
        'id': job.id,
        'job_type': job.job_type,
        'status': job.status,
        'profile_id': job.profile_id,
        'payload': job.payload or {},
        'result': job.result or {},
        'error': job.error,
        'created_at': job.created_at.isoformat() if job.created_at else None,
        'started_at': job.started_at.isoformat() if job.started_at else None,
        'heartbeat_at': job.heartbeat_at.isoformat() if job.heartbeat_at else None,
        'finished_at': job.finished_at.isoformat() if job.finished_at else None,
    }


@app.get('/api/jobs/{job_id}/events', dependencies=[AuthDep])
async def api_job_events(request: Request, job_id: str, db: Session = DbDep) -> StreamingResponse:
    """Stream one job's state over a single connection instead of browser polling."""
    current_user = current_user_from_request(request)
    visible_job = _visible_jobs_query(db, current_user).filter(BackgroundJob.id == job_id).one_or_none()
    if visible_job is None:
        raise HTTPException(status_code=404, detail='Job not found')
    user_id = current_user.id
    is_admin = current_user.is_admin
    # Streaming responses keep dependencies alive until the stream closes. Return
    # the initial visibility-check connection to the pool; the generator below
    # deliberately uses short-lived sessions for each state refresh.
    db.close()

    async def event_stream():
        last_state = None
        unchanged_seconds = 0
        while True:
            if await request.is_disconnected():
                break
            stream_db = SessionLocal()
            try:
                query = stream_db.query(BackgroundJob).filter(BackgroundJob.id == job_id)
                if not is_admin:
                    owned_profile_ids = [
                        row[0] for row in stream_db.query(ClientProfile.id).filter(ClientProfile.owner_user_id == user_id).all()
                    ]
                    clauses = [BackgroundJob.requested_by_user_id == user_id]
                    if owned_profile_ids:
                        clauses.append(BackgroundJob.profile_id.in_(owned_profile_ids))
                    query = query.filter(or_(*clauses))
                job = query.one_or_none()
                if job is None:
                    yield 'event: error\ndata: {"error":"Job not found"}\n\n'
                    break
                state = {
                    'id': job.id,
                    'job_type': job.job_type,
                    'status': job.status,
                    'result': job.result or {},
                    'error': job.error,
                }
            finally:
                stream_db.close()
            serialized = json.dumps(state, ensure_ascii=False, separators=(',', ':'))
            if serialized != last_state:
                yield f'data: {serialized}\n\n'
                last_state = serialized
                unchanged_seconds = 0
            else:
                unchanged_seconds += 1
                if unchanged_seconds >= 15:
                    # Keep reverse proxies and browsers from closing an otherwise
                    # healthy stream while a long-running job has no state change.
                    yield ': keep-alive\n\n'
                    unchanged_seconds = 0
            if state['status'] in ('completed', 'failed'):
                break
            await asyncio.sleep(1)

    return StreamingResponse(
        event_stream(),
        media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@app.get('/api/tenders', dependencies=[AuthDep])
def api_tenders(request: Request, db: Session = DbDep, min_score: int = 0, active_only: bool = True) -> list[dict]:
    current_user = current_user_from_request(request)
    query = (
        db.query(TenderScore)
        .options(joinedload(TenderScore.tender), joinedload(TenderScore.profile))
        .join(Tender)
        .filter(TenderScore.score >= min_score)
    )
    if active_only:
        query = query.filter(actionable_tender_clause())
    query = _filter_scores_for_user(query, current_user)
    scores = query.order_by(TenderScore.score.desc()).limit(200).all()
    return [
        {
            'score': s.score,
            'recommended_action': s.recommended_action,
            'lifecycle_status': tender_lifecycle_label(s.tender),
            'workflow_status': s.user_status,
            'profile': s.profile.name,
            'title': s.tender.title,
            'organization': s.tender.organization_name,
            'reference_number': s.tender.reference_number,
            'final_submission_date': format_local_datetime(s.tender.final_submission_date, include_tz=False) if s.tender.final_submission_date else None,
            'cpv_codes': s.tender.cpv_codes,
            'attachment_url': s.tender.attachment_url,
            'reasons': [display_scoring_reason(reason) for reason in (s.reasons or [])],
        }
        for s in scores
    ]
