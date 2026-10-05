from datetime import datetime, timedelta, timezone
import io

from app.models import ClientProfile, Tender, TenderScore
from pypdf import PdfReader
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.services.reports import ReportFilters, build_tender_report_pdf, pdf_urls_to_text, query_report_scores, report_period_label, report_summary, report_to_markdown, scores_to_rows


def _score(code='33790000-4', status='saved', score=68):
    profile = ClientProfile(slug='p', name='Profile', cpv_codes=['33000000-0'])
    tender = Tender(
        source='test',
        source_reference='1',
        reference_number='26PROCTEST',
        title='Προμήθεια εργαστηριακών ειδών',
        organization_name='Φορέας',
        published_date=datetime(2026, 6, 18, tzinfo=timezone.utc),
        final_submission_date=datetime.now(timezone.utc) + timedelta(days=3),
        cpv_codes=[code],
        cpv_descriptions={code: 'Εργαστηριακά είδη'},
    )
    tender.id = 1
    ts = TenderScore(
        score=score,
        rule_score=score,
        matched_cpv=[code],
        reasons=['Δοκιμή'],
        user_status=status,
        recommended_action='review',
    )
    ts.tender = tender
    ts.profile = profile
    return ts


def test_report_summary_groups_by_cpv_family():
    scores = [_score('33790000-4'), _score('33793000-5')]
    summary = report_summary(scores)
    families = summary['families']
    assert summary['total'] == 2
    assert families[0]['count'] == 2
    assert '33790000-4' in families[0]['family']


def test_report_summary_partitions_match_the_filtered_total():
    now = datetime.now(timezone.utc)
    rows = [
        _score(score=90),
        _score(score=70),
        _score(score=40),
        _score(score=80),
        _score(score=60),
    ]
    rows[0].tender.final_submission_date = now + timedelta(days=3)
    rows[1].tender.final_submission_date = now + timedelta(days=12)
    rows[2].tender.final_submission_date = None
    rows[3].tender.final_submission_date = now - timedelta(days=2)
    rows[4].tender.final_submission_date = now + timedelta(days=3)
    rows[4].tender.cancelled = True
    rows[0].is_new_in_latest_ingest = True

    summary = report_summary(rows)

    assert summary['total'] == 5
    assert sum(summary['match_counts'].values()) == summary['total']
    assert (
        summary['due_soon'] + summary['active_later'] + summary['unknown_deadline']
        + summary['expired'] + summary['cancelled']
    ) == summary['total']
    assert summary['latest_new'] == 1


def test_report_markdown_has_cpv_summary_without_ambiguous_content_scope():
    score = _score('33790000-4', status='reviewing')
    md = report_to_markdown([score], ReportFilters(), score.profile)
    assert 'Περιεχόμενο:' not in md
    assert 'Κύριες CPV οικογένειες' in md
    assert '33790000-4' in md
    assert 'Score:' not in md
    assert 'Ελάχιστο score:' not in md
    assert 'Γιατί εμφανίστηκε:' not in md


def test_report_period_label_all_database_when_dates_empty():
    assert report_period_label(ReportFilters()) == 'Όλη η βάση'
    assert report_period_label(ReportFilters(date_from='2026-06-01', date_to='2026-06-18')) == '2026-06-01 έως 2026-06-18'


def test_report_markdown_uses_all_database_period_when_no_dates():
    score = _score('33790000-4', status='reviewing')
    md = report_to_markdown([score], ReportFilters(), score.profile)
    assert 'Περίοδος ΚΗΜΔΗΣ: Όλη η βάση' in md


def test_latest_new_is_an_explicit_filter_in_markdown():
    score = _score('33790000-4', status='new')
    score.tender.is_new_in_latest_ingest = True
    filters = ReportFilters(new_from_last_ingest=True)
    md = report_to_markdown([score], filters, score.profile)
    assert 'Μόνο νέα τελευταίας εισαγωγής: Ναι' in md
    assert 'Περιεχόμενο:' not in md


def test_latest_new_query_returns_only_latest_relevant_items():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    profile = ClientProfile(slug='p2', name='Profile 2', cpv_codes=['33000000-0'], is_active=True)
    db.add(profile)
    db.flush()

    def add(ref, score, latest, status='new'):
        tender = Tender(
            source='khmdhs_notice',
            source_reference=ref,
            reference_number=ref,
            title='Tender ' + ref,
            organization_name='Org',
            published_date=datetime(2026, 6, 18, tzinfo=timezone.utc),
            final_submission_date=datetime.now(timezone.utc) + timedelta(days=3),
            cpv_codes=['33790000-4'],
        )
        row = TenderScore(profile=profile, tender=tender, score=score, rule_score=score, user_status=status, is_new_in_latest_ingest=latest)
        db.add(row)
        return row

    add('latest-good', 61, True)
    add('latest-low', 20, True)
    add('old-good', 80, False)
    add('irrelevant-latest', 90, True, status='not_relevant')
    db.commit()

    rows = query_report_scores(db, ReportFilters(profile_id=profile.id, new_from_last_ingest=True))
    assert [row.tender.reference_number for row in rows] == ['latest-good', 'latest-low']


def test_active_report_excludes_expired_and_cancelled():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    profile = ClientProfile(slug='lifecycle', name='Lifecycle', cpv_codes=['72413000-8'], is_active=True)
    db.add(profile)
    now = datetime.now(timezone.utc)
    for ref, deadline, cancelled in (
        ('active', now + timedelta(days=2), False),
        ('expired', now - timedelta(days=2), False),
        ('cancelled', now + timedelta(days=2), True),
    ):
        db.add(TenderScore(
            profile=profile,
            tender=Tender(
                source='khmdhs_notice', source_reference=ref, reference_number=ref,
                title=ref, cpv_codes=['72413000-8'],
                final_submission_date=deadline, cancelled=cancelled,
            ),
            score=85,
            rule_score=85,
            matched_cpv=['72413000-8'],
            user_status='new',
        ))
    db.commit()

    active_rows = query_report_scores(db, ReportFilters(profile_id=profile.id, active_only=True))
    all_rows = query_report_scores(db, ReportFilters(profile_id=profile.id, active_only=False))

    assert [row.tender.reference_number for row in active_rows] == ['active']
    assert {row.tender.reference_number for row in all_rows} == {'active', 'expired', 'cancelled'}
    assert {row.score for row in all_rows} == {85}


def test_reports_do_not_filter_by_score_and_explicit_cpv_category_still_works():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    profile = ClientProfile(slug='category', name='Category', cpv_codes=['72413000-8'], is_active=True)
    db.add(profile)
    future = datetime.now(timezone.utc) + timedelta(days=3)
    for ref, cpvs, score in (
        ('exact-low', ['72413000-8'], 40),
        ('partial-high', ['72413000-8', '33600000-6'], 90),
        ('none-high', ['33600000-6'], 90),
    ):
        category = 'exact_full' if len(cpvs) == 1 and cpvs[0] == '72413000-8' else ('exact_partial' if '72413000-8' in cpvs else 'none')
        db.add(TenderScore(
            profile=profile,
            tender=Tender(
                source='khmdhs_notice', source_reference=ref, reference_number=ref,
                title=ref, cpv_codes=cpvs, final_submission_date=future,
            ),
            score=score,
            rule_score=score,
            matched_cpv=[cpvs[0]],
            cpv_match_type=category,
            user_status='new',
        ))
    db.commit()

    all_rows = query_report_scores(db, ReportFilters(
        profile_id=profile.id, match_type='all', active_only=False,
    ))
    exact_rows = query_report_scores(db, ReportFilters(
        profile_id=profile.id, match_type='exact_full', active_only=False,
    ))
    partial_rows = query_report_scores(db, ReportFilters(
        profile_id=profile.id, match_type='exact_partial', active_only=False,
    ))

    assert {row.tender.reference_number for row in all_rows} == {'exact-low', 'partial-high', 'none-high'}
    assert [row.tender.reference_number for row in exact_rows] == ['exact-low']
    assert [row.tender.reference_number for row in partial_rows] == ['partial-high']


def test_report_export_query_is_not_silently_capped_at_1000_rows():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    profile = ClientProfile(slug='complete-export', name='Complete export', cpv_codes=['72413000-8'], is_active=True)
    db.add(profile)
    future = datetime.now(timezone.utc) + timedelta(days=3)
    for number in range(1001):
        db.add(TenderScore(
            profile=profile,
            tender=Tender(
                source='khmdhs_notice', source_reference=f'export-{number}',
                reference_number=f'export-{number}', title=f'Export {number}',
                cpv_codes=['72413000-8'], final_submission_date=future,
            ),
            score=85, rule_score=85, matched_cpv=['72413000-8'],
            cpv_match_type='exact_full', user_status='new',
        ))
    db.commit()

    rows = query_report_scores(db, ReportFilters(
        profile_id=profile.id, active_only=False,
    ))

    assert len(rows) == 1001


def test_report_deadline_range_and_explicit_not_relevant_filter():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    profile = ClientProfile(slug='filters', name='Filters', cpv_codes=['72413000-8'], is_active=True)
    db.add(profile)
    for ref, deadline, status in (
        ('inside', datetime(2026, 9, 12, 12, tzinfo=timezone.utc), 'not_relevant'),
        ('outside', datetime(2026, 9, 20, 12, tzinfo=timezone.utc), 'not_relevant'),
        ('saved-inside', datetime(2026, 9, 12, 12, tzinfo=timezone.utc), 'saved'),
    ):
        db.add(TenderScore(
            profile=profile,
            tender=Tender(
                source='khmdhs_notice', source_reference=ref, reference_number=ref,
                title=ref, cpv_codes=['72413000-8'], final_submission_date=deadline,
            ),
            score=85, rule_score=85, matched_cpv=['72413000-8'], user_status=status,
        ))
    db.commit()

    rows = query_report_scores(db, ReportFilters(
        profile_id=profile.id,
        deadline_filter='all',
        deadline_from='2026-09-10',
        deadline_to='2026-09-15',
        user_status='not_relevant',
    ))

    assert [row.tender.reference_number for row in rows] == ['inside']



def test_reports_template_exposes_dashboard_aligned_export_filters():
    from pathlib import Path
    template = Path('app/templates/reports.html').read_text(encoding='utf-8')
    assert 'name="match_type"' in template
    assert 'name="deadline_filter"' in template
    assert 'name="deadline_from"' in template
    assert 'name="deadline_to"' in template
    assert 'name="user_status"' in template
    assert 'Ακριβές match' in template
    assert 'Μερικό match' in template
    assert 'name="scope"' not in template
    assert 'Περιεχόμενο αναφοράς' not in template
    assert 'name="new_from_last_ingest"' in template
    assert 'summary.active_later' in template
    assert 'summary.match_counts.exact_full' in template
    assert 'name="min_score"' not in template
    assert 's.score' not in template
    assert 'Ελάχιστο score' not in template
    assert 'βαθμολογία' not in template.lower()


def test_report_exports_exclude_score_and_scoring_explanations():
    row = scores_to_rows([_score()])[0]

    assert 'score' not in row
    assert 'reasons' not in row
    assert 'recommended_action' not in row


def test_designed_tender_pdf_has_overview_results_and_page_chrome():
    first = _score('33790000-4', status='saved')
    second = _score('33793000-5', status='reviewing')
    second.tender.source_reference = '2'
    second.tender.reference_number = '26PROCTEST2'
    second.tender.title = 'Δεύτερος διαγωνισμός εργαστηριακού εξοπλισμού'
    payload = build_tender_report_pdf(
        [first, second],
        ReportFilters(deadline_filter='all'),
        first.profile,
    )

    reader = PdfReader(io.BytesIO(payload))
    text = '\n'.join(page.extract_text() or '' for page in reader.pages)

    assert payload.startswith(b'%PDF')
    assert len(reader.pages) >= 2
    assert 'Αναφορά διαγωνισμών' in text
    assert 'Ενεργά φίλτρα' in text
    assert 'Αποτελέσματα' in text
    assert '26PROCTEST2' in text
    assert 'Score:' not in text


def test_report_markdown_includes_saved_profile_description():
    score = _score('33790000-4', status='reviewing')
    score.profile.description = 'Η εταιρεία προμηθεύει εργαστηριακά αναλώσιμα και αντιδραστήρια.'
    md = report_to_markdown([score], ReportFilters(), score.profile)
    assert 'Πλαίσιο προφίλ επιχείρησης' in md
    assert 'Η εταιρεία προμηθεύει εργαστηριακά αναλώσιμα και αντιδραστήρια.' in md


def test_report_markdown_includes_pdf_text_excerpt_only_when_requested():
    score = _score('33790000-4', status='reviewing')
    score.tender.pdf_text = 'Αυτό είναι extracted κείμενο από την επίσημη διακήρυξη PDF. ' * 20
    plain = report_to_markdown([score], ReportFilters(), score.profile)
    assert 'Extracted PDF text αποθηκευμένο: Ναι' in plain
    assert 'Απόσπασμα extracted PDF text για προέλεγχο' not in plain

    with_excerpt = report_to_markdown([score], ReportFilters(), score.profile, include_pdf_text=True, pdf_text_max_chars=120)
    assert 'Απόσπασμα extracted PDF text για προέλεγχο' in with_excerpt
    assert 'Αυτό είναι extracted κείμενο από την επίσημη διακήρυξη PDF' in with_excerpt


def test_reports_template_has_markdown_export_without_ai_ready_option():
    from pathlib import Path
    template = Path('app/templates/reports.html').read_text(encoding='utf-8')
    assert 'format=md' in template
    assert 'format=pdf_urls' in template
    legacy_format = 'format=md_' + 'ai'
    legacy_label = 'AI' + '-ready Markdown'
    assert legacy_format not in template
    assert legacy_label not in template


def test_pdf_urls_export_contains_unique_non_empty_attachment_urls():
    first = _score()
    duplicate = _score()
    without_url = _score()
    first.tender.attachment_url = 'https://cerpp.eprocurement.gov.gr/khmdhs-opendata/notice/attachment/26PROC1'
    duplicate.tender.attachment_url = first.tender.attachment_url
    without_url.tender.attachment_url = ''

    text = pdf_urls_to_text([first, duplicate, without_url])

    assert text == 'https://cerpp.eprocurement.gov.gr/khmdhs-opendata/notice/attachment/26PROC1\n'
