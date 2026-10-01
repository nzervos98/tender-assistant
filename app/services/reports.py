from __future__ import annotations

import csv
import html
import io
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, Optional

from fastapi.responses import Response, StreamingResponse
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import BaseDocTemplate, Frame, KeepTogether, PageBreak, PageTemplate, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.services.timezone import format_local_datetime, iso_local_datetime, local_day_end, local_day_start, now_utc, today_local
from app.services.text_normalizer import display_text
from app.services.workflow import normalize_workflow_status, workflow_status_filter_values, workflow_status_label
from app.services.opportunity_status import (
    actionable_tender_clause,
    expired_tender_clause,
    tender_lifecycle_label,
    tender_lifecycle_status,
)
from app.services.scoring import CPVMatchClassification, classify_cpv_match
from app.services.geography import (
    region_filter_expressions,
    tender_authority_region_values,
    tender_execution_region_values,
)
from app.services.cpv_catalog import cpv_family_label
from app.models import ClientProfile, Tender, TenderScore


@dataclass
class ReportFilters:
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    profile_id: Optional[int] = None
    profile_ids: Optional[list[int]] = None
    match_type: str = 'all'  # all, exact_full, exact_partial, broad, none
    new_from_last_ingest: bool = False
    active_only: bool = True
    deadline_filter: str = ''  # empty keeps compatibility with active_only
    deadline_from: Optional[str] = None
    deadline_to: Optional[str] = None
    user_status: str = 'all'
    q: str = ''
    region: list[str] | str = ''
    authority_region: list[str] | str = ''


def _parse_iso_date(value: str | None) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _dt_start(value: str | None) -> Optional[datetime]:
    d = _parse_iso_date(value)
    if d is None:
        return None
    return local_day_start(d)


def _dt_end(value: str | None) -> Optional[datetime]:
    d = _parse_iso_date(value)
    if d is None:
        return None
    return local_day_end(d)


def default_date_from(days: int = 1) -> str:
    return (today_local() - timedelta(days=days)).isoformat()


def default_date_to() -> str:
    return today_local().isoformat()


REPORT_MATCH_TYPES = {'all', 'exact_full', 'exact_partial', 'broad', 'none'}
REPORT_DEADLINE_FILTERS = {'all', 'active', 'expired', 'cancelled', 'unknown'}


def report_match_label(match_type: str) -> str:
    return {
        'all': 'Όλες οι CPV κατηγορίες',
        'exact_full': 'Ακριβές match',
        'exact_partial': 'Μερικό match',
        'broad': 'Child / broad match',
        'none': 'Χωρίς CPV match',
    }.get(match_type, 'Όλες οι CPV κατηγορίες')


def _classification_key(match: CPVMatchClassification) -> str:
    if match.is_full_exact:
        return 'exact_full'
    if match.kind == 'exact':
        return 'exact_partial'
    return match.kind


def _score_match_type(score: TenderScore) -> str:
    match = getattr(score, 'cpv_match', None)
    if match is None:
        match = classify_cpv_match(score.tender, score.profile)
        score.cpv_match = match
    return _classification_key(match)


def _effective_deadline_filter(filters: ReportFilters) -> str:
    if filters.deadline_filter in REPORT_DEADLINE_FILTERS:
        return filters.deadline_filter
    return 'active' if filters.active_only else 'all'


def query_report_scores(db: Session, filters: ReportFilters) -> list[TenderScore]:
    q = (
        db.query(TenderScore)
        .options(joinedload(TenderScore.tender), joinedload(TenderScore.profile))
        .join(Tender)
    )
    if filters.profile_id:
        q = q.filter(TenderScore.profile_id == filters.profile_id)
    elif filters.profile_ids is not None:
        q = q.filter(TenderScore.profile_id.in_(filters.profile_ids))
    normalized_user_status = normalize_workflow_status(filters.user_status) if filters.user_status != 'all' else 'all'
    if normalized_user_status == 'not_relevant':
        # Rejected rows are available only when the user asks for them explicitly.
        q = q.filter(TenderScore.user_status.in_(workflow_status_filter_values('not_relevant')))
    else:
        q = q.filter(~TenderScore.user_status.in_(workflow_status_filter_values('not_relevant')))
        if normalized_user_status != 'all':
            q = q.filter(TenderScore.user_status.in_(workflow_status_filter_values(normalized_user_status)))

    match_type = filters.match_type if filters.match_type in REPORT_MATCH_TYPES else 'all'
    if match_type != 'all':
        q = q.filter(TenderScore.cpv_match_type == match_type)
    if filters.new_from_last_ingest:
        q = q.filter(TenderScore.is_new_in_latest_ingest.is_(True))
    # The period is intentionally based on KIMDIS dates, not on when our system stored the row.
    # Prefer official published_date and fall back to submission_date when published_date is missing.
    kimdis_date = func.coalesce(Tender.published_date, Tender.submission_date)
    start = _dt_start(filters.date_from)
    end = _dt_end(filters.date_to)
    if start:
        q = q.filter(kimdis_date >= start)
    if end:
        q = q.filter(kimdis_date <= end)
    deadline_filter = _effective_deadline_filter(filters)
    now = now_utc()
    if deadline_filter == 'active':
        q = q.filter(actionable_tender_clause(now_utc()))
    elif deadline_filter == 'expired':
        q = q.filter(expired_tender_clause(now))
    elif deadline_filter == 'cancelled':
        q = q.filter(Tender.cancelled.is_(True))
    elif deadline_filter == 'unknown':
        q = q.filter(Tender.cancelled.is_(False), Tender.final_submission_date.is_(None))

    deadline_start = _dt_start(filters.deadline_from)
    deadline_end = _dt_end(filters.deadline_to)
    if deadline_start:
        q = q.filter(Tender.final_submission_date >= deadline_start)
    if deadline_end:
        q = q.filter(Tender.final_submission_date <= deadline_end)
    if filters.q.strip():
        pattern = f"%{filters.q.strip()}%"
        q = q.filter(or_(Tender.title.ilike(pattern), Tender.organization_name.ilike(pattern), Tender.reference_number.ilike(pattern)))
    execution_clauses = region_filter_expressions(filters.region)
    if execution_clauses:
        q = q.filter(or_(*execution_clauses))
    authority_clauses = region_filter_expressions(filters.authority_region, authority=True)
    if authority_clauses:
        q = q.filter(or_(*authority_clauses))
    ordered = q.order_by(
        Tender.final_submission_date.asc().nullslast(),
        Tender.published_date.desc().nullslast(),
        Tender.reference_number.asc(),
    )
    # Exports must be complete. Category filtering is database-native through the
    # materialized cpv_match_type, so there is no need for a silent row cap or a
    # full Python-side classification pass before filtering.
    rows = ordered.all()
    for score in rows:
        score.cpv_match = classify_cpv_match(score.tender, score.profile)
    return rows


def profile_to_markdown(profile: ClientProfile) -> str:
    def lines(values: Iterable[str]) -> str:
        values = list(values or [])
        if not values:
            return '- Δεν έχει οριστεί.'
        return '\n'.join(f'- {v}' for v in values)

    budget = []
    if profile.min_budget is not None:
        budget.append(f'ελάχιστο {profile.min_budget:g} EUR')
    if profile.max_budget is not None:
        budget.append(f'μέγιστο {profile.max_budget:g} EUR')
    budget_text = ', '.join(budget) if budget else 'Δεν έχει οριστεί συγκεκριμένο εύρος.'

    return f"""# Προφίλ παρακολούθησης: {profile.name}

## Περιγραφή εταιρείας / δυνατοτήτων
{profile.description or 'Δεν έχει συμπληρωθεί περιγραφή.'}

## CPV που παρακολουθούνται
{lines(profile.cpv_codes)}

## CPV prefixes για ευρύτερη συνάφεια
{lines(profile.cpv_prefixes)}

## Πιστοποιητικά ή απαιτήσεις προς έλεγχο
{lines(profile.required_certificates)}

## Περιοχές NUTS
{lines(profile.preferred_regions)}

## Εύρος προϋπολογισμού
{budget_text}

## Οδηγία χρήσης
Χρησιμοποιήστε αυτό το προφίλ μαζί με την αναφορά διαγωνισμών της ίδιας περιόδου, ώστε ο έλεγχος να γίνεται με το ίδιο επιχειρησιακό πλαίσιο: CPV, απαιτήσεις, περιοχές και εύρος προϋπολογισμού.

Η ανάλυση είναι βοηθητική και δεν αντικαθιστά τον έλεγχο της επίσημης διακήρυξης.
"""



def _list_or_dash(values: Iterable[str]) -> str:
    values = [str(v).strip() for v in (values or []) if str(v).strip()]
    return ', '.join(values) if values else '-'


def _profile_context_lines(profile: ClientProfile | None) -> list[str]:
    if profile is None:
        return [
            '## Πλαίσιο προφίλ επιχείρησης',
            'Δεν επιλέχθηκε συγκεκριμένο προφίλ. Η αναφορά περιλαμβάνει αποτελέσματα από όλα τα διαθέσιμα προφίλ.',
            '',
        ]

    budget = []
    if profile.min_budget is not None:
        budget.append(f'ελάχιστο {profile.min_budget:g} EUR')
    if profile.max_budget is not None:
        budget.append(f'μέγιστο {profile.max_budget:g} EUR')
    budget_text = ', '.join(budget) if budget else '-'

    return [
        '## Πλαίσιο προφίλ επιχείρησης',
        f'- Όνομα προφίλ: {profile.name}',
        f'- Αποθηκευμένη περιγραφή επιχείρησης / δυνατοτήτων: {profile.description or "Δεν έχει συμπληρωθεί περιγραφή."}',
        f'- CPV προφίλ: {_list_or_dash(profile.cpv_codes)}',
        f'- CPV prefixes: {_list_or_dash(profile.cpv_prefixes)}',
        f'- Πιστοποιητικά / απαιτήσεις προς έλεγχο: {_list_or_dash(profile.required_certificates)}',
        f'- Περιοχές NUTS: {_list_or_dash(profile.preferred_regions)}',
        f'- Εύρος προϋπολογισμού προφίλ: {budget_text}',
        '',
        'Η παραπάνω περιγραφή είναι το επιχειρησιακό πλαίσιο με βάση το οποίο αξιολογούνται οι διαγωνισμοί της αναφοράς.',
        '',
    ]


def _pdf_text_excerpt(tender: Tender, max_chars: int = 2500) -> str:
    text = (tender.pdf_text or '').strip()
    if not text:
        return ''
    normalized = ' '.join(text.split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[:max_chars].rstrip() + '…'



def _location_hint(tender: Tender) -> str:
    values = tender_execution_region_values(tender)
    return ' · '.join(values[1::2] or values) if values else '-'


def _authority_location_hint(tender: Tender) -> str:
    values = tender_authority_region_values(tender)
    return ' · '.join(values) if values else '-'

def _kimdis_date(tender: Tender):
    """Official-ish date shown to users: published date first, submission date fallback."""
    return tender.published_date or tender.submission_date


def _kimdis_date_label(tender: Tender) -> str:
    if tender.published_date:
        return 'Δημοσίευση ΚΗΜΔΗΣ'
    if tender.submission_date:
        return 'Καταχώριση ΚΗΜΔΗΣ'
    return 'Ημερομηνία ΚΗΜΔΗΣ'



def _format_date_only_if_midnight(dt) -> str:
    if not dt:
        return 'Δεν παρέχεται'
    # publishedDate from KIMDIS is often date-only and arrives as 00:00.
    # Showing the time there confuses end users, so omit it when it is midnight.
    if getattr(dt, 'hour', 0) == 0 and getattr(dt, 'minute', 0) == 0 and getattr(dt, 'second', 0) == 0:
        from app.services.timezone import format_local_date
        return format_local_date(dt)
    local_text = format_local_datetime(dt)
    return local_text


def scores_to_rows(scores: list[TenderScore]) -> list[dict[str, object]]:
    rows = []
    for s in scores:
        t = s.tender
        rows.append({
            'cpv_match_category': report_match_label(_score_match_type(s)),
            'cpv_match_count': getattr(getattr(s, 'cpv_match', None), 'matched_count', 0),
            'cpv_total_count': getattr(getattr(s, 'cpv_match', None), 'total_count', 0),
            'profile': s.profile.name if s.profile else '',
            'status': workflow_status_label(s.user_status),
            'new_from_latest_ingest': 'Ναι' if getattr(s, 'is_new_in_latest_ingest', False) else 'Όχι',
            'lifecycle_status': tender_lifecycle_label(t),
            'reference_number': t.reference_number or t.source_reference,
            'title': display_text(t.title, 'Τίτλος μη αναγνώσιμος - δείτε το επίσημο PDF'),
            'organization': display_text(t.organization_name) if t.organization_name else '',
            'source': t.source,
            'region_hint': _location_hint(t),
            'kimdis_date_type': _kimdis_date_label(t),
            'kimdis_date': iso_local_datetime(_kimdis_date(t)),
            'published_date': iso_local_datetime(t.published_date),
            'submission_date': iso_local_datetime(t.submission_date),
            'final_submission_date': iso_local_datetime(t.final_submission_date),
            'total_cost_without_vat': t.total_cost_without_vat,
            'cpv_codes': ', '.join(t.cpv_codes or []),
            'cpv_family': cpv_family_for_score(s),
            'cpv_descriptions': '; '.join([f'{k}: {v}' for k, v in (t.cpv_descriptions or {}).items()]),
            'profile_description': s.profile.description if s.profile and s.profile.description else '',
            'pdf_url': t.attachment_url or '',
            'pdf_text_chars': len(t.pdf_text or ''),
        })
    return rows


def pdf_urls_to_text(scores: list[TenderScore]) -> str:
    seen: set[str] = set()
    urls: list[str] = []
    for score in scores:
        url = ((score.tender.attachment_url if score.tender else '') or '').strip()
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return '\n'.join(urls) + ('\n' if urls else '')



def report_period_label(filters: ReportFilters) -> str:
    if filters.date_from and filters.date_to:
        return f'{filters.date_from} έως {filters.date_to}'
    if filters.date_from:
        return f'Από {filters.date_from}'
    if filters.date_to:
        return f'Έως {filters.date_to}'
    return 'Όλη η βάση'


def primary_cpv_code(score: TenderScore) -> str:
    matched = list(score.matched_cpv or [])
    if matched:
        return str(matched[0])
    tender_codes = list(score.tender.cpv_codes or []) if score.tender else []
    return str(tender_codes[0]) if tender_codes else ''


def cpv_family_for_score(score: TenderScore) -> str:
    code = primary_cpv_code(score)
    return cpv_family_label(code, target_level=2) if code else 'Χωρίς CPV'


def report_summary(scores: list[TenderScore]) -> dict[str, object]:
    now = now_utc()
    soon_limit = now + timedelta(days=7)
    statuses: dict[str, int] = {}
    family_counts: dict[str, dict[str, object]] = {}
    due_soon = 0
    active_later = 0
    unknown_deadline = 0
    latest_new = 0
    expired = 0
    cancelled = 0
    match_counts = {'exact_full': 0, 'exact_partial': 0, 'broad': 0, 'none': 0}
    for score in scores:
        status_label = workflow_status_label(score.user_status)
        statuses[status_label] = statuses.get(status_label, 0) + 1
        deadline = score.tender.final_submission_date if score.tender else None
        lifecycle = tender_lifecycle_status(score.tender, now) if score.tender else 'unknown_deadline'
        if lifecycle == 'cancelled':
            cancelled += 1
        elif lifecycle == 'unknown_deadline':
            unknown_deadline += 1
        elif lifecycle == 'expired':
            expired += 1
        elif deadline is not None:
            comparable_deadline = deadline if deadline.tzinfo else deadline.replace(tzinfo=timezone.utc)
            if comparable_deadline <= soon_limit:
                due_soon += 1
            else:
                active_later += 1
        if getattr(score, 'is_new_in_latest_ingest', False):
            latest_new += 1
        match_key = _score_match_type(score)
        match_counts[match_key] = match_counts.get(match_key, 0) + 1
        family = cpv_family_for_score(score)
        item = family_counts.setdefault(family, {'family': family, 'count': 0, 'examples': []})
        item['count'] = int(item['count']) + 1
        examples = item['examples']
        if isinstance(examples, list) and len(examples) < 3:
            title = display_text(score.tender.title, 'Τίτλος μη αναγνώσιμος') if score.tender else ''
            ref = score.tender.reference_number or score.tender.source_reference if score.tender else ''
            examples.append({'reference': ref, 'title': title})
    families = sorted(family_counts.values(), key=lambda row: (-int(row['count']), str(row['family'])))
    return {
        'total': len(scores),
        'statuses': statuses,
        'due_soon': due_soon,
        'active_later': active_later,
        'unknown_deadline': unknown_deadline,
        'latest_new': latest_new,
        'expired': expired,
        'cancelled': cancelled,
        'match_counts': match_counts,
        'families': families,
    }


def _summary_lines(scores: list[TenderScore]) -> list[str]:
    summary = report_summary(scores)
    lines = [
        '## Σύνοψη',
        f'- Σύνολο αποτελεσμάτων: {summary["total"]}',
        f'- Λήγουν μέσα σε 7 ημέρες: {summary["due_soon"]}',
        f'- Ενεργά με λήξη μετά τις 7 ημέρες: {summary["active_later"]}',
        f'- Νέα από τελευταία εισαγωγή: {summary["latest_new"]}',
        f'- Ληγμένα: {summary["expired"]}',
        f'- Ακυρωμένα / ματαιωμένα: {summary["cancelled"]}',
        f'- Χωρίς καταληκτική ημερομηνία: {summary["unknown_deadline"]}',
        f'- Ακριβή matches: {summary["match_counts"]["exact_full"]}',
        f'- Μερικά matches: {summary["match_counts"]["exact_partial"]}',
        f'- Child / broad matches: {summary["match_counts"]["broad"]}',
        '',
    ]
    statuses = summary['statuses']
    if statuses:
        lines.append('## Κατάσταση εργασίας')
        for label, count in sorted(statuses.items(), key=lambda item: item[0]):
            lines.append(f'- {label}: {count}')
        lines.append('')
    families = summary['families']
    if families:
        lines.append('## Κύριες CPV οικογένειες')
        for row in families[:12]:
            lines.append(f'- {row["family"]}: {row["count"]} διαγωνισμοί')
        lines.append('')
    return lines

def report_to_markdown(scores: list[TenderScore], filters: ReportFilters, profile: ClientProfile | None = None, include_pdf_text: bool = False, pdf_text_max_chars: int = 2500) -> str:
    title = 'Αναφορά διαγωνισμών'
    period = report_period_label(filters)
    deadline_filter_label = {
        'all': 'Όλες',
        'active': 'Ενεργές ή άγνωστης προθεσμίας',
        'expired': 'Ληγμένες',
        'cancelled': 'Ακυρωμένες / ματαιωμένες',
        'unknown': 'Άγνωστη προθεσμία',
    }[_effective_deadline_filter(filters)]
    lines = [
        f'# {title}',
        '',
        f'Περίοδος ΚΗΜΔΗΣ: {period}',
        f'Προφίλ: {profile.name if profile else "Όλα"}',
        f'Κατηγορία CPV: {report_match_label(filters.match_type)}',
        f'Μόνο νέα τελευταίας εισαγωγής: {"Ναι" if filters.new_from_last_ingest else "Όχι"}',
        f'Κατάσταση προθεσμίας: {deadline_filter_label}',
        f'Λήξη προσφορών από: {filters.deadline_from or "-"}',
        f'Λήξη προσφορών έως: {filters.deadline_to or "-"}',
        f'Κατάσταση εργασίας: {workflow_status_label(filters.user_status) if filters.user_status != "all" else "Όλες"}',
        f'Τόπος εκτέλεσης NUTS: {_list_or_dash(filters.region) if not isinstance(filters.region, str) else (filters.region or "-")}',
        f'Έδρα Αναθέτουσας Αρχής NUTS: {_list_or_dash(filters.authority_region) if not isinstance(filters.authority_region, str) else (filters.authority_region or "-")}',
        f'Πλήθος αποτελεσμάτων: {len(scores)}',
        f'Δημιουργήθηκε: {format_local_datetime(now_utc())}',
        '',
        'Σημείωση: Η περίοδος βασίζεται στις ημερομηνίες ΚΗΜΔΗΣ, δηλαδή στη δημοσίευση ή, αν λείπει, στην καταχώριση/υποβολή.',
        'Για συμμετοχή σε διαγωνισμό, τελική πηγή ελέγχου παραμένει το επίσημο PDF και ο ΑΔΑΜ.',
        'Για πλήρη έλεγχο διακήρυξης, προτείνεται να ανοίγετε και το επίσημο PDF όταν είναι διαθέσιμο. Η αναφορά περιλαμβάνει URL PDF και μπορεί να περιλάβει σύντομο απόσπασμα από extracted PDF text όπου υπάρχει.',
        '',
    ]
    lines.extend(_profile_context_lines(profile))
    lines.extend(_summary_lines(scores))
    for idx, s in enumerate(scores, start=1):
        t = s.tender
        title_text = display_text(t.title, 'Τίτλος μη αναγνώσιμος - δείτε το επίσημο PDF')
        cpv_desc = '; '.join([f'{k}: {v}' for k, v in (t.cpv_descriptions or {}).items()]) or '-'
        lines.extend([
            f'## {idx}. {title_text}',
            f'- Κατηγορία CPV match: {report_match_label(_score_match_type(s))}',
            f'- Προφίλ: {s.profile.name if s.profile else "-"}',
            f'- ΑΔΑΜ: {t.reference_number or t.source_reference}',
            f'- Φορέας: {display_text(t.organization_name) if t.organization_name else "-"}',
            f'- Δημοσίευση στο ΚΗΜΔΗΣ: {_format_date_only_if_midnight(t.published_date)}',
            f'- Καταχώριση/υποβολή στο ΚΗΜΔΗΣ: {format_local_datetime(t.submission_date) if t.submission_date else "Δεν παρέχεται"}',
            f'- Λήξη υποβολής προσφορών: {format_local_datetime(t.final_submission_date) if t.final_submission_date else "Δεν παρέχεται"}',
            f'- Κατάσταση ευκαιρίας: {tender_lifecycle_label(t)}',
            f'- Τόπος εκτέλεσης από ΚΗΜΔΗΣ: {_location_hint(t)}',
            f'- Έδρα Αναθέτουσας Αρχής από ΚΗΜΔΗΣ: {_authority_location_hint(t)}',
            f'- Ποσό χωρίς ΦΠΑ: {t.total_cost_without_vat if t.total_cost_without_vat is not None else "-"}',
            f'- CPV: {", ".join(t.cpv_codes or []) or "-"}',
            f'- CPV οικογένεια αναφοράς: {cpv_family_for_score(s)}',
            f'- Περιγραφές CPV: {cpv_desc}',
            f'- Νέο από τελευταία εισαγωγή: {"Ναι" if getattr(s, "is_new_in_latest_ingest", False) else "Όχι"}',
            f'- Κατάσταση εργασίας: {workflow_status_label(s.user_status)}',
        ])
        pdf_text_len = len(t.pdf_text or '')
        lines.append(f'- Extracted PDF text αποθηκευμένο: {"Ναι" if pdf_text_len else "Όχι"}' + (f' ({pdf_text_len} χαρακτήρες)' if pdf_text_len else ''))
        lines.append(f'- Επίσημο PDF: {t.attachment_url or "-"}')
        if include_pdf_text:
            excerpt = _pdf_text_excerpt(t, max_chars=pdf_text_max_chars)
            if excerpt:
                lines.extend([
                    '- Απόσπασμα extracted PDF text για προέλεγχο:',
                    f'  > {excerpt}',
                    '  >',
                    '  > Σημείωση: Το απόσπασμα είναι βοηθητικό. Για πλήρη έλεγχο χρησιμοποιήστε το επίσημο PDF.',
                ])
            else:
                lines.append('- Απόσπασμα extracted PDF text για προέλεγχο: Δεν υπάρχει αποθηκευμένο κείμενο PDF.')
        lines.append('')
    return '\n'.join(lines)

def make_csv_response(scores: list[TenderScore], filename: str = 'tender_report.csv') -> StreamingResponse:
    rows = scores_to_rows(scores)
    output = io.StringIO()
    fieldnames = list(rows[0].keys()) if rows else ['title']
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    data = output.getvalue().encode('utf-8-sig')
    return StreamingResponse(io.BytesIO(data), media_type='text/csv; charset=utf-8', headers={'Content-Disposition': f'attachment; filename="{filename}"'})


def make_jsonl_response(scores: list[TenderScore], filename: str = 'tender_report.jsonl') -> StreamingResponse:
    rows = scores_to_rows(scores)
    payload = '\n'.join(json.dumps(row, ensure_ascii=False) for row in rows).encode('utf-8')
    return StreamingResponse(io.BytesIO(payload), media_type='application/x-ndjson; charset=utf-8', headers={'Content-Disposition': f'attachment; filename="{filename}"'})


def make_markdown_response(text: str, filename: str = 'report.md') -> Response:
    return Response(text, media_type='text/markdown; charset=utf-8', headers={'Content-Disposition': f'attachment; filename="{filename}"'})


def make_pdf_urls_response(scores: list[TenderScore], filename: str = 'pdf_urls.txt') -> Response:
    return Response(
        pdf_urls_to_text(scores),
        media_type='text/plain; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


def _register_pdf_font() -> str:
    candidates = [
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf',
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont('AppGreek', path))
                return 'AppGreek'
            except Exception:
                continue
    return 'Helvetica'


def _register_pdf_font_family() -> tuple[str, str]:
    regular = _register_pdf_font()
    bold_candidates = [
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
        '/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf',
    ]
    for path in bold_candidates:
        if os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont('AppGreekBold', path))
                return regular, 'AppGreekBold'
            except Exception:
                continue
    return regular, 'Helvetica-Bold'


def _paragraph(text: object, style: ParagraphStyle) -> Paragraph:
    safe = str(text or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    safe = safe.replace('\n', '<br/>')
    return Paragraph(safe, style)


def make_pdf_response(title: str, body_markdown: str, filename: str = 'report.pdf') -> StreamingResponse:
    font_name = _register_pdf_font()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=1.4 * cm, rightMargin=1.4 * cm, topMargin=1.2 * cm, bottomMargin=1.2 * cm)
    styles = getSampleStyleSheet()
    normal = ParagraphStyle('GreekNormal', parent=styles['Normal'], fontName=font_name, fontSize=9.5, leading=13, alignment=TA_LEFT)
    h1 = ParagraphStyle('GreekH1', parent=styles['Heading1'], fontName=font_name, fontSize=16, leading=20, spaceAfter=10)
    h2 = ParagraphStyle('GreekH2', parent=styles['Heading2'], fontName=font_name, fontSize=12, leading=15, spaceBefore=8, spaceAfter=6)

    story = [_paragraph(title, h1)]
    for line in body_markdown.splitlines():
        if line.startswith('# '):
            continue
        if line.startswith('## '):
            story.append(_paragraph(line[3:], h2))
        elif line.strip() == '':
            story.append(Spacer(1, 5))
        else:
            story.append(_paragraph(line, normal))
    doc.build(story)
    buffer.seek(0)
    return StreamingResponse(buffer, media_type='application/pdf', headers={'Content-Disposition': f'attachment; filename="{filename}"'})


def _pdf_safe(value: object) -> str:
    return html.escape('' if value is None else str(value), quote=True)


def _pdf_money(value: object) -> str:
    if value in (None, ''):
        return 'Δεν παρέχεται'
    try:
        return f'{float(value):,.2f} EUR'.replace(',', 'X').replace('.', ',').replace('X', '.')
    except (TypeError, ValueError):
        return str(value)


def _pdf_deadline(value: object) -> str:
    return format_local_datetime(value) if value else 'Δεν παρέχεται'


def build_tender_report_pdf(
    scores: list[TenderScore],
    filters: ReportFilters,
    profile: ClientProfile | None,
    *,
    include_pdf_text: bool = False,
) -> bytes:
    """Build the designed tender report PDF independently from the Markdown export."""
    regular_font, bold_font = _register_pdf_font_family()
    navy = colors.HexColor('#0F172A')
    slate = colors.HexColor('#475569')
    muted = colors.HexColor('#64748B')
    line = colors.HexColor('#E2E8F0')
    surface = colors.HexColor('#F8FAFC')
    blue = colors.HexColor('#2563EB')
    green = colors.HexColor('#15803D')
    amber = colors.HexColor('#B45309')
    violet = colors.HexColor('#6D28D9')
    red = colors.HexColor('#B91C1C')
    white = colors.white

    buffer = io.BytesIO()
    doc = BaseDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=1.35 * cm,
        rightMargin=1.35 * cm,
        topMargin=1.75 * cm,
        bottomMargin=1.55 * cm,
        title='Αναφορά διαγωνισμών',
        author='Tender Assistant',
        subject='Φιλτραρισμένη αναφορά διαγωνισμών ΚΗΜΔΗΣ',
    )
    page_width, page_height = A4
    content_width = page_width - doc.leftMargin - doc.rightMargin

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'TenderReportTitle', parent=styles['Title'], fontName=bold_font,
        fontSize=22, leading=27, textColor=white, spaceAfter=3,
    )
    cover_meta_style = ParagraphStyle(
        'TenderReportCoverMeta', parent=styles['Normal'], fontName=regular_font,
        fontSize=9.5, leading=13, textColor=colors.HexColor('#CBD5E1'),
    )
    section_style = ParagraphStyle(
        'TenderReportSection', parent=styles['Heading2'], fontName=bold_font,
        fontSize=13, leading=17, textColor=navy, spaceBefore=5, spaceAfter=8,
    )
    card_title_style = ParagraphStyle(
        'TenderReportCardTitle', parent=styles['Heading3'], fontName=bold_font,
        fontSize=11.2, leading=14.5, textColor=navy, spaceAfter=3,
    )
    body_style = ParagraphStyle(
        'TenderReportBody', parent=styles['Normal'], fontName=regular_font,
        fontSize=8.6, leading=12, textColor=slate,
    )
    small_style = ParagraphStyle(
        'TenderReportSmall', parent=body_style, fontSize=7.6, leading=10, textColor=muted,
    )
    label_style = ParagraphStyle(
        'TenderReportLabel', parent=small_style, fontName=bold_font,
        fontSize=7.2, leading=9, textColor=muted,
    )
    value_style = ParagraphStyle(
        'TenderReportValue', parent=body_style, fontSize=8.2, leading=11, textColor=navy,
    )
    metric_number_style = ParagraphStyle(
        'TenderReportMetricNumber', parent=styles['Normal'], fontName=bold_font,
        fontSize=18, leading=21, alignment=TA_CENTER, textColor=navy,
    )
    metric_label_style = ParagraphStyle(
        'TenderReportMetricLabel', parent=small_style, fontSize=7.2, leading=9,
        alignment=TA_CENTER, textColor=muted,
    )
    badge_style = ParagraphStyle(
        'TenderReportBadge', parent=small_style, fontName=bold_font,
        fontSize=7.2, leading=9, alignment=TA_CENTER, textColor=white,
    )
    link_style = ParagraphStyle(
        'TenderReportLink', parent=body_style, fontName=bold_font,
        fontSize=8, leading=11, textColor=blue,
    )
    excerpt_style = ParagraphStyle(
        'TenderReportExcerpt', parent=small_style, fontSize=7.5, leading=10.5,
        textColor=slate, leftIndent=7, borderColor=line, borderWidth=.5,
        borderPadding=7, backColor=surface,
    )

    def page_chrome(canvas, current_doc):
        canvas.saveState()
        header_y = page_height - doc.topMargin - 0.72 * cm
        canvas.setFillColor(navy)
        canvas.roundRect(doc.leftMargin, header_y, doc.width, 0.72 * cm, 4, stroke=0, fill=1)
        canvas.setFillColor(white)
        canvas.setFont(bold_font, 8)
        canvas.drawString(doc.leftMargin + 8, header_y + 0.24 * cm, 'TENDER ASSISTANT')
        canvas.setFont(regular_font, 7.5)
        canvas.drawRightString(page_width - doc.rightMargin - 8, header_y + 0.24 * cm, 'ΑΝΑΦΟΡΑ ΔΙΑΓΩΝΙΣΜΩΝ')
        canvas.setStrokeColor(line)
        canvas.line(doc.leftMargin, doc.bottomMargin + 0.52 * cm, page_width - doc.rightMargin, doc.bottomMargin + 0.52 * cm)
        canvas.setFillColor(muted)
        canvas.setFont(regular_font, 7.2)
        canvas.drawString(doc.leftMargin, doc.bottomMargin + 0.18 * cm, 'Πηγή δεδομένων: ΚΗΜΔΗΣ - Η επίσημη διακήρυξη παραμένει η τελική πηγή ελέγχου.')
        canvas.setFont(bold_font, 7.2)
        canvas.drawRightString(page_width - doc.rightMargin, doc.bottomMargin + 0.18 * cm, f'Σελίδα {canvas.getPageNumber()}')
        canvas.restoreState()

    class TenderReportCanvas(Canvas):
        def showPage(self):
            page_chrome(self, doc)
            super().showPage()

    report_frame = Frame(
        doc.leftMargin,
        doc.bottomMargin,
        doc.width,
        doc.height,
        id='tender-report-frame',
        leftPadding=0,
        rightPadding=0,
        topPadding=0.88 * cm,
        bottomPadding=0.78 * cm,
    )
    doc.addPageTemplates(PageTemplate(id='tender-report-pages', frames=[report_frame]))

    def paragraph(value: object, style=body_style) -> Paragraph:
        return Paragraph(_pdf_safe(value).replace('\n', '<br/>'), style)

    def metric_box(number: object, label: str, accent=blue) -> Table:
        table = Table(
            [[Paragraph(_pdf_safe(number), metric_number_style)], [Paragraph(_pdf_safe(label), metric_label_style)]],
            colWidths=[content_width / 5 - 5],
        )
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), white),
            ('BOX', (0, 0), (-1, -1), .7, line),
            ('LINEABOVE', (0, 0), (-1, 0), 2.2, accent),
            ('TOPPADDING', (0, 0), (-1, 0), 7),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 1),
            ('TOPPADDING', (0, 1), (-1, 1), 1),
            ('BOTTOMPADDING', (0, 1), (-1, 1), 7),
            ('LEFTPADDING', (0, 0), (-1, -1), 5),
            ('RIGHTPADDING', (0, 0), (-1, -1), 5),
        ]))
        return table

    summary = report_summary(scores)
    generated_label = format_local_datetime(now_utc())
    profile_label = profile.name if profile else 'Όλα τα διαθέσιμα προφίλ'
    cover = Table([
        [Paragraph('Αναφορά διαγωνισμών', title_style)],
        [Paragraph(f'Προφίλ: {_pdf_safe(profile_label)}<br/>Δημιουργήθηκε: {_pdf_safe(generated_label)}', cover_meta_style)],
    ], colWidths=[content_width])
    cover.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), navy),
        ('LEFTPADDING', (0, 0), (-1, -1), 18),
        ('RIGHTPADDING', (0, 0), (-1, -1), 18),
        ('TOPPADDING', (0, 0), (-1, 0), 18),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 4),
        ('TOPPADDING', (0, 1), (-1, 1), 2),
        ('BOTTOMPADDING', (0, 1), (-1, 1), 17),
        ('BOX', (0, 0), (-1, -1), 0, navy),
    ]))

    story: list[object] = [cover, Spacer(1, 12)]
    story.append(Paragraph('Ενεργά φίλτρα', section_style))
    filter_rows = [
        [paragraph('Περίοδος ΚΗΜΔΗΣ', label_style), paragraph(report_period_label(filters), value_style),
         paragraph('CPV match', label_style), paragraph(report_match_label(filters.match_type), value_style)],
        [paragraph('Προθεσμία', label_style), paragraph({
            'all': 'Όλες', 'active': 'Ενεργές ή άγνωστης προθεσμίας', 'expired': 'Ληγμένες',
            'cancelled': 'Ακυρωμένες / ματαιωμένες', 'unknown': 'Άγνωστη προθεσμία',
        }[_effective_deadline_filter(filters)], value_style),
         paragraph('Κατάσταση εργασίας', label_style), paragraph(
             workflow_status_label(filters.user_status) if filters.user_status != 'all' else 'Όλες εκτός «Δεν αφορά»', value_style)],
        [paragraph('Λήξη προσφορών', label_style), paragraph(
            f'{filters.deadline_from or "Αρχή"} έως {filters.deadline_to or "Χωρίς όριο"}', value_style),
         paragraph('Νέα εισαγωγής', label_style), paragraph('Μόνο νέα' if filters.new_from_last_ingest else 'Όχι', value_style)],
        [paragraph('Τόπος εκτέλεσης', label_style), paragraph(
            _list_or_dash(filters.region) if not isinstance(filters.region, str) else (filters.region or '-'), value_style),
         paragraph('Έδρα φορέα', label_style), paragraph(
            _list_or_dash(filters.authority_region) if not isinstance(filters.authority_region, str) else (filters.authority_region or '-'), value_style)],
    ]
    filter_table = Table(filter_rows, colWidths=[2.6 * cm, 6.25 * cm, 2.6 * cm, 6.25 * cm])
    filter_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), surface),
        ('GRID', (0, 0), (-1, -1), .45, line),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 7),
        ('RIGHTPADDING', (0, 0), (-1, -1), 7),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
    ]))
    story.extend([filter_table, Spacer(1, 12), Paragraph('Σύνοψη', section_style)])

    overview_metrics = Table([[
        metric_box(summary['total'], 'Σύνολο', navy),
        metric_box(summary['match_counts']['exact_full'], 'Ακριβή', green),
        metric_box(summary['match_counts']['exact_partial'], 'Μερικά', blue),
        metric_box(summary['match_counts']['broad'], 'Child / broad', amber),
        metric_box(summary['latest_new'], 'Νέα εισαγωγής', violet),
    ]], colWidths=[content_width / 5] * 5)
    overview_metrics.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 2.5), ('RIGHTPADDING', (0, 0), (-1, -1), 2.5)]))
    deadline_metrics = Table([[
        metric_box(summary['due_soon'], 'Λήγουν ≤ 7 ημέρες', red),
        metric_box(summary['active_later'], 'Λήγουν αργότερα', green),
        metric_box(summary['unknown_deadline'], 'Άγνωστη λήξη', muted),
        metric_box(summary['expired'], 'Ληγμένα', amber),
        metric_box(summary['cancelled'], 'Ακυρωμένα', red),
    ]], colWidths=[content_width / 5] * 5)
    deadline_metrics.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 2.5), ('RIGHTPADDING', (0, 0), (-1, -1), 2.5)]))
    story.extend([overview_metrics, Spacer(1, 7), deadline_metrics])

    if profile:
        cpv_values = list(profile.cpv_codes or [])
        cpv_preview = ', '.join(cpv_values[:10]) + (f'  +{len(cpv_values) - 10} ακόμη' if len(cpv_values) > 10 else '')
        budget_bits = []
        if profile.min_budget is not None:
            budget_bits.append(f'από {_pdf_money(profile.min_budget)}')
        if profile.max_budget is not None:
            budget_bits.append(f'έως {_pdf_money(profile.max_budget)}')
        profile_rows = [
            [paragraph('CPV προφίλ', label_style), paragraph(cpv_preview or '-', value_style)],
            [paragraph('Περιοχές προφίλ', label_style), paragraph(_list_or_dash(profile.preferred_regions), value_style)],
            [paragraph('Budget προφίλ', label_style), paragraph(' · '.join(budget_bits) or '-', value_style)],
        ]
        if profile.description:
            profile_rows.append([paragraph('Περιγραφή', label_style), paragraph(profile.description, value_style)])
        profile_table = Table(profile_rows, colWidths=[3.1 * cm, content_width - 3.1 * cm])
        profile_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#F1F5F9')),
            ('BOX', (0, 0), (-1, -1), .6, line),
            ('INNERGRID', (0, 0), (-1, -1), .35, line),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LEFTPADDING', (0, 0), (-1, -1), 7), ('RIGHTPADDING', (0, 0), (-1, -1), 7),
            ('TOPPADDING', (0, 0), (-1, -1), 5), ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ]))
        story.extend([Spacer(1, 12), Paragraph('Πλαίσιο προφίλ', section_style), profile_table])

    if scores:
        story.extend([PageBreak(), Paragraph('Αποτελέσματα', section_style)])
    else:
        story.extend([Spacer(1, 14), Paragraph('Δεν βρέθηκαν αποτελέσματα με τα επιλεγμένα φίλτρα.', body_style)])

    category_colors = {'exact_full': green, 'exact_partial': blue, 'broad': amber, 'none': muted}
    for index, score in enumerate(scores, start=1):
        tender = score.tender
        match_key = _score_match_type(score)
        accent = category_colors.get(match_key, muted)
        lifecycle = tender_lifecycle_status(tender)
        lifecycle_color = {'active': green, 'unknown_deadline': muted, 'expired': amber, 'cancelled': red}.get(lifecycle, muted)
        header_badges = [
            Table([[Paragraph(_pdf_safe(report_match_label(match_key)), badge_style)]], style=TableStyle([
                ('BACKGROUND', (0, 0), (-1, -1), accent), ('BOX', (0, 0), (-1, -1), 0, accent),
                ('LEFTPADDING', (0, 0), (-1, -1), 6), ('RIGHTPADDING', (0, 0), (-1, -1), 6),
                ('TOPPADDING', (0, 0), (-1, -1), 3), ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ])),
            Table([[Paragraph(_pdf_safe(tender_lifecycle_label(tender)), badge_style)]], style=TableStyle([
                ('BACKGROUND', (0, 0), (-1, -1), lifecycle_color), ('BOX', (0, 0), (-1, -1), 0, lifecycle_color),
                ('LEFTPADDING', (0, 0), (-1, -1), 6), ('RIGHTPADDING', (0, 0), (-1, -1), 6),
                ('TOPPADDING', (0, 0), (-1, -1), 3), ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ])),
        ]
        if getattr(score, 'is_new_in_latest_ingest', False):
            header_badges.append(Table([[Paragraph('ΝΕΟ', badge_style)]], style=TableStyle([
                ('BACKGROUND', (0, 0), (-1, -1), violet), ('BOX', (0, 0), (-1, -1), 0, violet),
                ('LEFTPADDING', (0, 0), (-1, -1), 6), ('RIGHTPADDING', (0, 0), (-1, -1), 6),
                ('TOPPADDING', (0, 0), (-1, -1), 3), ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ])))
        badges = Table([header_badges], colWidths=[None] * len(header_badges))
        badges.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'MIDDLE'), ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 4)]))

        cpvs = list(tender.cpv_codes or [])
        cpv_text = ', '.join(cpvs[:12]) + (f'  +{len(cpvs) - 12} ακόμη' if len(cpvs) > 12 else '')
        reference = tender.reference_number or tender.source_reference or '-'
        title_text = display_text(tender.title, 'Τίτλος μη αναγνώσιμος - δείτε την πηγή ΚΗΜΔΗΣ')
        organization = display_text(tender.organization_name) if tender.organization_name else 'Δεν παρέχεται'
        location = _location_hint(tender)
        authority_location = _authority_location_hint(tender)
        published = _format_date_only_if_midnight(tender.published_date or tender.submission_date)
        meta_rows = [
            [paragraph('ΑΔΑΜ', label_style), paragraph(reference, value_style), paragraph('Προφίλ', label_style), paragraph(score.profile.name if score.profile else '-', value_style)],
            [paragraph('Φορέας', label_style), paragraph(organization, value_style), paragraph('Λήξη', label_style), paragraph(_pdf_deadline(tender.final_submission_date), value_style)],
            [paragraph('Δημοσίευση', label_style), paragraph(published, value_style), paragraph('Ποσό χωρίς ΦΠΑ', label_style), paragraph(_pdf_money(tender.total_cost_without_vat), value_style)],
            [paragraph('Τόπος εκτέλεσης', label_style), paragraph(location, value_style), paragraph('Έδρα φορέα', label_style), paragraph(authority_location, value_style)],
            [paragraph('CPV', label_style), paragraph(cpv_text or '-', value_style), paragraph('Κατάσταση', label_style), paragraph(workflow_status_label(score.user_status), value_style)],
        ]
        meta_table = Table(meta_rows, colWidths=[2.4 * cm, 6.45 * cm, 2.4 * cm, 6.45 * cm])
        meta_table.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'TOP'), ('GRID', (0, 0), (-1, -1), .35, line),
            ('BACKGROUND', (0, 0), (-1, -1), white),
            ('LEFTPADDING', (0, 0), (-1, -1), 6), ('RIGHTPADDING', (0, 0), (-1, -1), 6),
            ('TOPPADDING', (0, 0), (-1, -1), 4.5), ('BOTTOMPADDING', (0, 0), (-1, -1), 4.5),
        ]))
        title_row = Table([[
            Paragraph(f'{index:02d}', ParagraphStyle('CardIndex', parent=metric_number_style, fontSize=13, leading=15, textColor=accent)),
            Paragraph(_pdf_safe(title_text), card_title_style),
        ]], colWidths=[1.05 * cm, content_width - 1.05 * cm - 12])
        title_row.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 0),
            ('RIGHTPADDING', (0, 0), (-1, -1), 5), ('TOPPADDING', (0, 0), (-1, -1), 0), ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ]))
        card_flowables: list[object] = [badges, Spacer(1, 5), title_row, Spacer(1, 6), meta_table]
        if tender.attachment_url:
            safe_url = _pdf_safe(tender.attachment_url)
            card_flowables.extend([
                Spacer(1, 5),
                Paragraph(f'<link href="{safe_url}" color="#2563EB">Άνοιγμα επίσημου PDF</link>', link_style),
            ])
        if include_pdf_text:
            excerpt = _pdf_text_excerpt(tender, max_chars=900)
            if excerpt:
                card_flowables.extend([Spacer(1, 6), Paragraph('Απόσπασμα PDF', label_style), Paragraph(_pdf_safe(excerpt), excerpt_style)])
        card = Table([[card_flowables]], colWidths=[content_width])
        card.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), surface), ('BOX', (0, 0), (-1, -1), .65, line),
            ('LINEBEFORE', (0, 0), (0, -1), 3.5, accent), ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LEFTPADDING', (0, 0), (-1, -1), 11), ('RIGHTPADDING', (0, 0), (-1, -1), 10),
            ('TOPPADDING', (0, 0), (-1, -1), 9), ('BOTTOMPADDING', (0, 0), (-1, -1), 9),
        ]))
        story.extend([KeepTogether([card]), Spacer(1, 9)])

    doc.build(story, canvasmaker=TenderReportCanvas)
    return buffer.getvalue()


def make_tender_report_pdf_response(
    scores: list[TenderScore],
    filters: ReportFilters,
    profile: ClientProfile | None,
    filename: str = 'tender_report.pdf',
    *,
    include_pdf_text: bool = False,
) -> StreamingResponse:
    payload = build_tender_report_pdf(scores, filters, profile, include_pdf_text=include_pdf_text)
    return StreamingResponse(
        io.BytesIO(payload),
        media_type='application/pdf',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )
