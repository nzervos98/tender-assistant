from app.models import ClientProfile, Tender
from app.services.scoring import rule_score_tender


def test_rule_scoring_matches_cpv_and_keyword():
    profile = ClientProfile(
        slug='test',
        name='Test',
        cpv_codes=['79340000-9'],
        cpv_prefixes=[],
        keywords=['διαφήμιση'],
        negative_keywords=[],
        required_certificates=[],
        min_budget=100,
        max_budget=10000,
    )
    tender = Tender(
        source='test',
        source_reference='1',
        title='Υπηρεσίες διαφήμισης και προβολής',
        cpv_codes=['79340000-9'],
        cpv_descriptions={'79340000-9': 'Υπηρεσίες διαφήμισης και μάρκετινγκ'},
        total_cost_without_vat=5000,
    )
    result = rule_score_tender(tender, profile)
    assert result.score >= 50
    assert '79340000-9' in result.matched_cpv


def test_rule_scoring_explains_cpv_family_match():
    profile = ClientProfile(
        slug='family',
        name='Family',
        cpv_codes=['79000000-4'],
        cpv_prefixes=['79'],
        keywords=[],
        negative_keywords=[],
        required_certificates=[],
    )
    tender = Tender(
        source='test',
        source_reference='2',
        title='Υπηρεσίες μάρκετινγκ',
        cpv_codes=['79340000-9'],
        cpv_descriptions={'79340000-9': 'Υπηρεσίες διαφήμισης και μάρκετινγκ'},
    )
    result = rule_score_tender(tender, profile)
    assert '79340000-9' in result.matched_cpv
    assert any('παιδιού/οικογένειας CPV' in reason for reason in result.reasons)


def test_positive_keyword_absent_is_neutral_for_cpv_match():
    profile_base = ClientProfile(
        slug='cpv-base',
        name='CPV base',
        cpv_codes=['79340000-9'],
        cpv_prefixes=[],
        keywords=[],
        negative_keywords=[],
        required_certificates=[],
    )
    profile_with_absent_keyword = ClientProfile(
        slug='cpv-keyword',
        name='CPV keyword',
        cpv_codes=['79340000-9'],
        cpv_prefixes=[],
        keywords=['ανύπαρκτηλέξη'],
        negative_keywords=[],
        required_certificates=[],
    )
    tender = Tender(
        source='test',
        source_reference='3',
        title='Υπηρεσίες διαφήμισης και προβολής',
        cpv_codes=['79340000-9'],
        cpv_descriptions={'79340000-9': 'Υπηρεσίες διαφήμισης και μάρκετινγκ'},
    )
    base = rule_score_tender(tender, profile_base)
    with_keyword = rule_score_tender(tender, profile_with_absent_keyword)
    assert with_keyword.score == base.score
    assert any('δεν επηρέασαν' in reason for reason in with_keyword.reasons)


def test_missing_budget_amount_is_neutral():
    profile_base = ClientProfile(
        slug='cpv-budget-base',
        name='CPV budget base',
        cpv_codes=['79340000-9'],
        cpv_prefixes=[],
        keywords=[],
        negative_keywords=[],
        required_certificates=[],
    )
    profile_with_budget = ClientProfile(
        slug='cpv-budget',
        name='CPV budget',
        cpv_codes=['79340000-9'],
        cpv_prefixes=[],
        keywords=[],
        negative_keywords=[],
        required_certificates=[],
        min_budget=1000,
        max_budget=10000,
    )
    tender = Tender(
        source='test',
        source_reference='4',
        title='Υπηρεσίες διαφήμισης και προβολής',
        cpv_codes=['79340000-9'],
        cpv_descriptions={'79340000-9': 'Υπηρεσίες διαφήμισης και μάρκετινγκ'},
        total_cost_without_vat=None,
    )
    base = rule_score_tender(tender, profile_base)
    with_budget = rule_score_tender(tender, profile_with_budget)
    assert with_budget.score == base.score
    assert any('budget δεν επηρέασε' in reason for reason in with_budget.reasons)


def test_required_certificates_need_pdf_before_penalty():
    profile = ClientProfile(
        slug='certs',
        name='Certs',
        cpv_codes=['79340000-9'],
        cpv_prefixes=[],
        keywords=[],
        negative_keywords=[],
        required_certificates=['ISO 9001'],
    )
    tender_without_pdf = Tender(
        source='test',
        source_reference='5',
        title='Υπηρεσίες διαφήμισης και προβολής',
        cpv_codes=['79340000-9'],
        cpv_descriptions={'79340000-9': 'Υπηρεσίες διαφήμισης και μάρκετινγκ'},
        pdf_text='',
    )
    tender_with_pdf_missing_cert = Tender(
        source='test',
        source_reference='6',
        title='Υπηρεσίες διαφήμισης και προβολής',
        cpv_codes=['79340000-9'],
        cpv_descriptions={'79340000-9': 'Υπηρεσίες διαφήμισης και μάρκετινγκ'},
        pdf_text='Τεχνική περιγραφή χωρίς το ζητούμενο πιστοποιητικό.',
    )
    without_pdf = rule_score_tender(tender_without_pdf, profile)
    with_pdf = rule_score_tender(tender_with_pdf_missing_cert, profile)
    assert without_pdf.score > with_pdf.score
    assert without_pdf.missing_requirements == []
    assert with_pdf.missing_requirements == ['ISO 9001']


def test_multiple_cpv_partial_exact_match_is_explained_and_conservative():
    profile = ClientProfile(
        slug='multi-cpv',
        name='Multi CPV',
        cpv_codes=['33100000-1'],
        cpv_prefixes=[],
        keywords=[],
        negative_keywords=[],
        required_certificates=[],
    )
    single_cpv_tender = Tender(
        source='test',
        source_reference='multi-1',
        title='Προμήθεια ιατρικών συσκευών',
        cpv_codes=['33100000-1'],
        cpv_descriptions={'33100000-1': 'Ιατρικές συσκευές'},
    )
    mixed_cpv_tender = Tender(
        source='test',
        source_reference='multi-2',
        title='Μικτή προμήθεια υγειονομικού υλικού',
        cpv_codes=['33100000-1', '33600000-6', '33700000-7', '33900000-9'],
        cpv_descriptions={
            '33100000-1': 'Ιατρικές συσκευές',
            '33600000-6': 'Φαρμακευτικά προϊόντα',
            '33700000-7': 'Προϊόντα ατομικής περιποίησης',
            '33900000-9': 'Εξοπλισμός και προμήθειες νεκροψίας και νεκροτομείου',
        },
    )

    single = rule_score_tender(single_cpv_tender, profile)
    mixed = rule_score_tender(mixed_cpv_tender, profile)

    assert mixed.score < single.score
    assert '33100000-1' in mixed.matched_cpv
    assert any('πολλαπλά CPV' in reason and '1 από 4' in reason and 'μερικό/μικτό' in reason for reason in mixed.reasons)
    assert any('Λοιποί CPV' in reason and '33600000-6' in reason for reason in mixed.reasons)


def test_multiple_cpv_all_matched_is_not_described_as_partial():
    profile = ClientProfile(
        slug='all-cpv',
        name='All CPV',
        cpv_codes=['09000000-3'],
        cpv_prefixes=['09'],
        keywords=[],
        negative_keywords=[],
        required_certificates=[],
    )
    single = Tender(
        source='test',
        source_reference='all-single',
        title='Προμήθεια πετρελαιοειδών',
        cpv_codes=['09000000-3'],
        cpv_descriptions={'09000000-3': 'Πετρελαϊκά προϊόντα'},
    )
    all_matched = Tender(
        source='test',
        source_reference='all-matched',
        title='Προμήθεια πετρελαιοειδών',
        cpv_codes=['09000000-3', '09135100-5'],
        cpv_descriptions={
            '09000000-3': 'Πετρελαϊκά προϊόντα',
            '09135100-5': 'Πετρέλαιο θέρμανσης',
        },
    )

    single_result = rule_score_tender(single, profile)
    all_matched_result = rule_score_tender(all_matched, profile)

    assert all_matched_result.score == single_result.score
    assert {'09000000-3', '09135100-5'}.issubset(set(all_matched_result.matched_cpv))
    assert any('καλύπτονται όλα από το προφίλ' in reason for reason in all_matched_result.reasons)
    assert not any('μερικό/μικτό' in reason for reason in all_matched_result.reasons)
    assert not any('Λοιποί CPV' in reason for reason in all_matched_result.reasons)


def test_multiple_cpv_family_match_stays_relevant_but_below_single_family_match():
    profile = ClientProfile(
        slug='health-parent-multi',
        name='Health parent multi',
        cpv_codes=['33000000-0'],
        cpv_prefixes=['33'],
        keywords=[],
        negative_keywords=[],
        required_certificates=[],
    )
    single_child = Tender(
        source='test',
        source_reference='family-single',
        title='Προμήθεια ιατρικών συσκευών',
        cpv_codes=['33100000-1'],
        cpv_descriptions={'33100000-1': 'Ιατρικές συσκευές'},
    )
    mixed_children = Tender(
        source='test',
        source_reference='family-mixed',
        title='Μικτή προμήθεια υγειονομικού υλικού',
        cpv_codes=['33100000-1', '33600000-6', '33700000-7', '45000000-7'],
        cpv_descriptions={
            '33100000-1': 'Ιατρικές συσκευές',
            '33600000-6': 'Φαρμακευτικά προϊόντα',
            '33700000-7': 'Προϊόντα ατομικής περιποίησης',
            '45000000-7': 'Κατασκευαστικές εργασίες',
        },
    )

    single = rule_score_tender(single_child, profile)
    mixed = rule_score_tender(mixed_children, profile)

    assert mixed.score < single.score
    assert mixed.score >= 45
    assert {'33100000-1', '33600000-6', '33700000-7'}.issubset(set(mixed.matched_cpv))
    assert any('πολλαπλά CPV' in reason and '3 από 4' in reason and 'μερικό/μικτό' in reason for reason in mixed.reasons)


def test_broad_root_descendant_match_is_review_not_high_without_other_signals():
    from datetime import datetime, timedelta, timezone

    profile = ClientProfile(
        slug='broad-health',
        name='Broad Health',
        cpv_codes=['33000000-0'],
        cpv_prefixes=['33'],
        keywords=[],
        negative_keywords=[],
        required_certificates=[],
    )
    tender = Tender(
        source='test',
        source_reference='broad-1',
        title='Προμήθεια υγειονομικού υλικού',
        cpv_codes=['33140000-3'],
        cpv_descriptions={'33140000-3': 'Ιατρικά αναλώσιμα'},
        final_submission_date=datetime.now(timezone.utc) + timedelta(days=3),
    )

    result = rule_score_tender(tender, profile)

    assert '33140000-3' in result.matched_cpv
    assert 55 <= result.score < 75
    assert result.recommended_action == 'review'
    assert any('πολύ γενικό γονικό CPV' in reason for reason in result.reasons)


def test_workflow_new_label_is_no_action_not_import_new():
    from app.services.workflow import workflow_status_label

    assert workflow_status_label('new') == 'Χωρίς ενέργεια'
