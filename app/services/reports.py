from __future__ import annotations

import csv
import io
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, Optional

from fastapi.responses import Response, StreamingResponse
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import String, cast, func, or_
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.services.timezone import format_local_datetime, iso_local_datetime, local_day_end, local_day_start, now_utc, today_local
from app.services.text_normalizer import display_text
from app.services.workflow import workflow_status_filter_values, workflow_status_label
from app.services.geography import expand_region_terms
from app.services.cpv_catalog import cpv_family_label
from app.models import ClientProfile, Tender, TenderScore


@dataclass
class ReportFilters:
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    profile_id: Optional[int] = None
    profile_ids: Optional[list[int]] = None
    min_score: int = 55
    scope: str = 'matches'  # matches, latest_new, shortlist, all
    active_only: bool = True
    q: str = ''
    region: str = ''


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
    # Client-facing reports never include rows explicitly marked as not relevant.
    q = q.filter(~TenderScore.user_status.in_(workflow_status_filter_values('not_relevant')))
    if filters.scope == 'matches':
        q = q.filter(TenderScore.score >= filters.min_score)
    elif filters.scope == 'latest_new':
        # Latest-ingest report: practical new items only, using the same relevance threshold as "Πιθανές ευκαιρίες".
        q = q.filter(TenderScore.is_new_in_latest_ingest.is_(True), TenderScore.score >= filters.min_score)
    elif filters.scope == 'shortlist':
        q = q.filter(TenderScore.user_status.in_(workflow_status_filter_values('saved') + workflow_status_filter_values('reviewing')))
    # The period is intentionally based on KIMDIS dates, not on when our system stored the row.
    # Prefer official published_date and fall back to submission_date when published_date is missing.
    kimdis_date = func.coalesce(Tender.published_date, Tender.submission_date)
    start = _dt_start(filters.date_from)
    end = _dt_end(filters.date_to)
    if start:
        q = q.filter(kimdis_date >= start)
    if end:
        q = q.filter(kimdis_date <= end)
    if filters.active_only:
        now = now_utc()
        q = q.filter(or_(Tender.final_submission_date.is_(None), Tender.final_submission_date >= now))
    if filters.q.strip():
        pattern = f"%{filters.q.strip()}%"
        q = q.filter(or_(Tender.title.ilike(pattern), Tender.organization_name.ilike(pattern), Tender.reference_number.ilike(pattern)))
    if filters.region.strip():
        clauses = []
        for term in expand_region_terms(filters.region):
            pattern = f"%{term}%"
            clauses.extend([Tender.organization_name.ilike(pattern), Tender.title.ilike(pattern), cast(Tender.raw, String).ilike(pattern)])
        if clauses:
            q = q.filter(or_(*clauses))
    return q.order_by(TenderScore.score.desc(), Tender.final_submission_date.asc().nullslast()).limit(1000).all()


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

## Λέξεις που ανεβάζουν συνάφεια
{lines(profile.keywords)}

## Λέξεις που μειώνουν συνάφεια ή αποκλείουν αποτελέσματα
{lines(profile.negative_keywords)}

## Πιστοποιητικά ή απαιτήσεις προς έλεγχο
{lines(profile.required_certificates)}

## Περιοχές NUTS
{lines(profile.preferred_regions)}

## Εύρος προϋπολογισμού
{budget_text}

## Οδηγία χρήσης
Χρησιμοποιήστε αυτό το προφίλ μαζί με την αναφορά διαγωνισμών της ίδιας περιόδου, ώστε ο έλεγχος να γίνεται με το ίδιο επιχειρησιακό πλαίσιο: CPV, keywords, απαιτήσεις, περιοχές και εύρος προϋπολογισμού.

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
        f'- Λέξεις που ανεβάζουν συνάφεια: {_list_or_dash(profile.keywords)}',
        f'- Λέξεις που μειώνουν συνάφεια: {_list_or_dash(profile.negative_keywords)}',
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
    raw = tender.raw or {}
    if isinstance(raw, dict):
        if raw.get('nutsCity'):
            return str(raw.get('nutsCity'))
        item = raw.get('nutsCode')
        if isinstance(item, dict) and item.get('value'):
            return str(item.get('value'))
        items = raw.get('nutsCodes')
        if isinstance(items, list) and items:
            first = items[0]
            if isinstance(first, dict):
                nested = first.get('nutsCode') or first
                if isinstance(nested, dict) and nested.get('value'):
                    return str(nested.get('value'))
    return '-'

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


def _reason_bullets(reasons) -> list[str]:
    return [str(r).strip() for r in (reasons or []) if str(r).strip()]

def scores_to_rows(scores: list[TenderScore]) -> list[dict[str, object]]:
    rows = []
    for s in scores:
        t = s.tender
        rows.append({
            'score': round(float(s.score or 0), 2),
            'profile': s.profile.name if s.profile else '',
            'status': workflow_status_label(s.user_status),
            'new_from_latest_ingest': 'Ναι' if getattr(s, 'is_new_in_latest_ingest', False) else 'Όχι',
            'recommended_action': s.recommended_action,
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
            'reasons': ' | '.join(s.reasons or []),
            'matched_keywords': ', '.join(s.matched_keywords or []),
            'profile_description': s.profile.description if s.profile and s.profile.description else '',
            'pdf_url': t.attachment_url or '',
            'pdf_text_chars': len(t.pdf_text or ''),
        })
    return rows



def report_scope_label(scope: str) -> str:
    if scope == 'new':
        scope = 'latest_new'
    return {
        'matches': 'Πιθανές ευκαιρίες',
        'latest_new': 'Νέα από τελευταία εισαγωγή',
        'shortlist': 'Αποθηκευμένα / σε έλεγχο',
        'all': 'Όλα τα μη απορριφθέντα',
    }.get(scope or 'matches', 'Πιθανές ευκαιρίες')


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
    bands = {'high': 0, 'review': 0, 'low': 0}
    statuses: dict[str, int] = {}
    family_counts: dict[str, dict[str, object]] = {}
    due_soon = 0
    unknown_deadline = 0
    latest_new = 0
    for score in scores:
        value = float(score.score or 0)
        if value >= 75:
            bands['high'] += 1
        elif value >= 55:
            bands['review'] += 1
        else:
            bands['low'] += 1
        status_label = workflow_status_label(score.user_status)
        statuses[status_label] = statuses.get(status_label, 0) + 1
        deadline = score.tender.final_submission_date if score.tender else None
        if deadline is None:
            unknown_deadline += 1
        elif now <= deadline <= soon_limit:
            due_soon += 1
        if getattr(score, 'is_new_in_latest_ingest', False):
            latest_new += 1
        family = cpv_family_for_score(score)
        item = family_counts.setdefault(family, {'family': family, 'count': 0, 'max_score': 0.0, 'examples': []})
        item['count'] = int(item['count']) + 1
        item['max_score'] = max(float(item['max_score']), value)
        examples = item['examples']
        if isinstance(examples, list) and len(examples) < 3:
            title = display_text(score.tender.title, 'Τίτλος μη αναγνώσιμος') if score.tender else ''
            ref = score.tender.reference_number or score.tender.source_reference if score.tender else ''
            examples.append({'reference': ref, 'title': title, 'score': round(value, 1)})
    families = sorted(family_counts.values(), key=lambda row: (-int(row['count']), -float(row['max_score']), str(row['family'])))
    return {
        'total': len(scores),
        'bands': bands,
        'statuses': statuses,
        'due_soon': due_soon,
        'unknown_deadline': unknown_deadline,
        'latest_new': latest_new,
        'families': families,
    }


def _summary_lines(scores: list[TenderScore], filters: ReportFilters) -> list[str]:
    summary = report_summary(scores)
    bands = summary['bands']
    lines = [
        '## Σύνοψη',
        f'- Σύνολο αποτελεσμάτων: {summary["total"]}',
        f'- Υψηλή προτεραιότητα: {bands["high"]}',
        f'- Μεσαία προτεραιότητα: {bands["review"]}',
        f'- Χαμηλή προτεραιότητα/λοιπά: {bands["low"]}',
        f'- Λήγουν μέσα σε 7 ημέρες: {summary["due_soon"]}',
        f'- Νέα από τελευταία εισαγωγή: {summary["latest_new"]}',
        f'- Χωρίς καταληκτική ημερομηνία: {summary["unknown_deadline"]}',
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
        if filters.scope == 'shortlist':
            title = '## CPV οικογένειες από αποθηκευμένα / σε έλεγχο'
        elif filters.scope == 'latest_new':
            title = '## CPV οικογένειες νέων ευρημάτων'
        else:
            title = '## Κύριες CPV οικογένειες'
        lines.append(title)
        for row in families[:12]:
            lines.append(f'- {row["family"]}: {row["count"]} διαγωνισμοί')
        lines.append('')
    return lines

def report_to_markdown(scores: list[TenderScore], filters: ReportFilters, profile: ClientProfile | None = None, include_pdf_text: bool = False, pdf_text_max_chars: int = 2500) -> str:
    title = 'Αναφορά διαγωνισμών'
    period = report_period_label(filters)
    scope_label = report_scope_label(filters.scope)
    active_label = 'Ναι' if filters.active_only else 'Όχι'
    lines = [
        f'# {title}',
        '',
        f'Περίοδος ΚΗΜΔΗΣ: {period}',
        f'Προφίλ: {profile.name if profile else "Όλα"}',
        f'Περιεχόμενο: {scope_label}',
        f'Ελάχιστο score: {filters.min_score if filters.scope in ("matches", "latest_new") else "-"}',
        f'Μόνο ενεργά ή άγνωστης προθεσμίας: {active_label}',
        f'Περιοχή NUTS: {filters.region or "-"}',
        f'Πλήθος αποτελεσμάτων: {len(scores)}',
        f'Δημιουργήθηκε: {format_local_datetime(now_utc())}',
        '',
        'Σημείωση: Η περίοδος βασίζεται στις ημερομηνίες ΚΗΜΔΗΣ, δηλαδή στη δημοσίευση ή, αν λείπει, στην καταχώριση/υποβολή.',
        'Για συμμετοχή σε διαγωνισμό, τελική πηγή ελέγχου παραμένει το επίσημο PDF και ο ΑΔΑΜ.',
        'Για πλήρη έλεγχο διακήρυξης, προτείνεται να ανοίγετε και το επίσημο PDF όταν είναι διαθέσιμο. Η αναφορά περιλαμβάνει URL PDF και μπορεί να περιλάβει σύντομο απόσπασμα από extracted PDF text όπου υπάρχει.',
        '',
    ]
    lines.extend(_profile_context_lines(profile))
    lines.extend(_summary_lines(scores, filters))
    for idx, s in enumerate(scores, start=1):
        t = s.tender
        title_text = display_text(t.title, 'Τίτλος μη αναγνώσιμος - δείτε το επίσημο PDF')
        cpv_desc = '; '.join([f'{k}: {v}' for k, v in (t.cpv_descriptions or {}).items()]) or '-'
        lines.extend([
            f'## {idx}. {title_text}',
            f'- Score: {s.score:.1f}',
            f'- Προφίλ: {s.profile.name if s.profile else "-"}',
            f'- ΑΔΑΜ: {t.reference_number or t.source_reference}',
            f'- Φορέας: {display_text(t.organization_name) if t.organization_name else "-"}',
            f'- Δημοσίευση στο ΚΗΜΔΗΣ: {_format_date_only_if_midnight(t.published_date)}',
            f'- Καταχώριση/υποβολή στο ΚΗΜΔΗΣ: {format_local_datetime(t.submission_date) if t.submission_date else "Δεν παρέχεται"}',
            f'- Λήξη υποβολής προσφορών: {format_local_datetime(t.final_submission_date) if t.final_submission_date else "Δεν παρέχεται"}',
            f'- Γεωγραφική ένδειξη από ΚΗΜΔΗΣ: {_location_hint(t)}',
            f'- Ποσό χωρίς ΦΠΑ: {t.total_cost_without_vat if t.total_cost_without_vat is not None else "-"}',
            f'- CPV: {", ".join(t.cpv_codes or []) or "-"}',
            f'- CPV οικογένεια αναφοράς: {cpv_family_for_score(s)}',
            f'- Περιγραφές CPV: {cpv_desc}',
            f'- Νέο από τελευταία εισαγωγή: {"Ναι" if getattr(s, "is_new_in_latest_ingest", False) else "Όχι"}',
            f'- Κατάσταση εργασίας: {workflow_status_label(s.user_status)}',
            '- Γιατί εμφανίστηκε:',
        ])
        reason_lines = _reason_bullets(s.reasons)
        if reason_lines:
            lines.extend([f'  - {reason}' for reason in reason_lines])
        else:
            lines.append('  - Δεν υπάρχουν καταγεγραμμένοι λόγοι.')
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
    fieldnames = list(rows[0].keys()) if rows else ['score', 'title']
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
