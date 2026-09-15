from app.models import ClientProfile, Tender
from app.services.scoring import classify_cpv_match, display_scoring_reason, rule_score_tender


def test_legacy_scoring_reasons_use_current_match_labels_at_display_time():
    assert display_scoring_reason('Πλήρες exact match: παλιό') == 'Ακριβές match: παλιό'
    assert display_scoring_reason('Μερικό exact match: 1 από 2') == 'Μερικό match: 1 από 2'


def test_rule_scoring_matches_exact_cpv():
    profile = ClientProfile(
        slug='test',
        name='Test',
        cpv_codes=['79340000-9'],
        cpv_prefixes=[],
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
    assert result.score >= 75
    assert '79340000-9' in result.matched_cpv
    assert result.matched_keywords == []


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
    assert any('Μερικό match' in reason and '1 από 4' in reason for reason in mixed.reasons)
    assert any('CPV εκτός ακριβούς αντιστοίχισης' in reason and '33600000-6' in reason for reason in mixed.reasons)
    classification = classify_cpv_match(mixed_cpv_tender, profile)
    assert classification.kind == 'exact'
    assert classification.is_partial_exact
    assert classification.exact_count == 1
    assert classification.total_count == 4


def test_single_exact_parent_cpv_is_full_cpv_match():
    profile = ClientProfile(
        slug='software-parent',
        name='Software parent',
        cpv_codes=['48000000-8'],
        cpv_prefixes=[],
        keywords=[],
        negative_keywords=[],
        required_certificates=[],
    )
    single_cpv_tender = Tender(
        source='test',
        source_reference='software-single',
        title='Software procurement',
        cpv_codes=['48000000-8'],
        cpv_descriptions={'48000000-8': 'Software package and information systems'},
    )
    mixed_cpv_tender = Tender(
        source='test',
        source_reference='software-mixed',
        title='Mixed software procurement',
        cpv_codes=['48000000-8', '72210000-0', '35125100-7'],
        cpv_descriptions={
            '48000000-8': 'Software package and information systems',
            '72210000-0': 'Programming services of packaged software products',
            '35125100-7': 'Sensors',
        },
    )

    single = rule_score_tender(single_cpv_tender, profile)
    mixed = rule_score_tender(mixed_cpv_tender, profile)

    assert single.score == 100.0
    assert mixed.score == 85.0
    assert single.recommended_action == 'bid'
    assert mixed.recommended_action == 'bid'
    assert any('Ακριβές ταίριασμα μοναδικού δηλωμένου CPV' in reason for reason in single.reasons)
    assert any('Μερικό match' in reason for reason in mixed.reasons)


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

    assert all_matched_result.score <= single_result.score
    assert all_matched_result.score >= 55
    assert {'09000000-3', '09135100-5'}.issubset(set(all_matched_result.matched_cpv))
    assert any('Μερικό match' in reason for reason in all_matched_result.reasons)
    classification = classify_cpv_match(all_matched, profile)
    assert classification.kind == 'exact'
    assert classification.is_partial_exact
    assert not any('Λοιποί CPV' in reason for reason in all_matched_result.reasons)


def test_multiple_cpv_family_match_stays_visible_at_cpv_floor():
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

    assert single.score == 55
    assert mixed.score == 55
    assert {'33100000-1', '33600000-6', '33700000-7'}.issubset(set(mixed.matched_cpv))
    assert any('Ευρύτερο CPV match' in reason and '3 από 4' in reason for reason in mixed.reasons)
    classification = classify_cpv_match(mixed_children, profile)
    assert classification.kind == 'broad'
    assert classification.exact_count == 0
    assert classification.broad_count == 3


def test_partial_cpv_match_has_same_category_base_for_one_of_two_or_one_of_sixteen():
    profile = ClientProfile(
        slug='simple-partial',
        name='Simple partial',
        cpv_codes=['72413000-8'],
        cpv_prefixes=[],
        keywords=[],
        negative_keywords=[],
        required_certificates=[],
    )
    single = Tender(source='test', source_reference='single', title='Web', cpv_codes=['72413000-8'])
    one_of_two = Tender(
        source='test', source_reference='two', title='Web lot',
        cpv_codes=['72413000-8', '45000000-7'],
    )
    one_of_sixteen = Tender(
        source='test', source_reference='sixteen', title='Web lot in a large tender',
        cpv_codes=['72413000-8', *[f'4500000{i}-0' for i in range(15)]],
    )

    full_result = rule_score_tender(single, profile)
    two_result = rule_score_tender(one_of_two, profile)
    sixteen_result = rule_score_tender(one_of_sixteen, profile)

    assert full_result.score == 100
    assert two_result.score == 85
    assert sixteen_result.score == 85
    assert two_result.matched_cpv == ['72413000-8']
    assert sixteen_result.matched_cpv == ['72413000-8']


def test_deadline_and_cancellation_do_not_change_relevance_score():
    from datetime import datetime, timedelta, timezone

    profile = ClientProfile(
        slug='lifecycle-neutral',
        name='Lifecycle neutral',
        cpv_codes=['72413000-8'],
        cpv_prefixes=[],
        keywords=[],
        negative_keywords=[],
        required_certificates=[],
    )
    active = Tender(
        source='test', source_reference='active', title='Web', cpv_codes=['72413000-8'],
        final_submission_date=datetime.now(timezone.utc) + timedelta(days=3),
    )
    expired = Tender(
        source='test', source_reference='expired', title='Web', cpv_codes=['72413000-8'],
        final_submission_date=datetime.now(timezone.utc) - timedelta(days=3),
    )
    cancelled = Tender(
        source='test', source_reference='cancelled', title='Web', cpv_codes=['72413000-8'],
        cancelled=True,
    )

    assert rule_score_tender(active, profile).score == 100
    assert rule_score_tender(expired, profile).score == 100
    assert rule_score_tender(cancelled, profile).score == 100


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


def test_optional_criteria_rank_inside_cpv_bands_without_crossing_categories():
    common = {
        'preferred_regions': ['EL30 — Αττική'],
        'min_budget': 1000,
        'max_budget': 10000,
        'required_certificates': ['ISO 9001'],
    }
    exact_profile = ClientProfile(
        slug='band-exact', name='Exact', cpv_codes=['33140000-3'], cpv_prefixes=[], **common,
    )
    broad_profile = ClientProfile(
        slug='band-broad', name='Broad', cpv_codes=['33000000-0'], cpv_prefixes=['33'], **common,
    )
    mismatching_data = {
        'total_cost_without_vat': 20000,
        'pdf_text': 'Τεχνικό κείμενο χωρίς το απαιτούμενο πιστοποιητικό.',
        'raw': {'nutsCodes': [{'nutsCode': {'key': 'EL422', 'value': 'Θήρα'}}]},
    }
    full = Tender(
        source='test', source_reference='band-full', title='Full',
        cpv_codes=['33140000-3'], **mismatching_data,
    )
    partial = Tender(
        source='test', source_reference='band-partial', title='Partial',
        cpv_codes=['33140000-3', '45000000-7'], **mismatching_data,
    )
    broad = Tender(
        source='test', source_reference='band-broad', title='Broad',
        cpv_codes=['33140000-3'], **mismatching_data,
    )

    full_score = rule_score_tender(full, exact_profile).score
    partial_score = rule_score_tender(partial, exact_profile).score
    broad_score = rule_score_tender(broad, broad_profile).score

    assert (full_score, partial_score, broad_score) == (86, 71, 41)
    assert full_score > 85
    assert partial_score > 55
    assert broad_score <= 55


def test_workflow_new_label_is_no_action_not_import_new():
    from app.services.workflow import workflow_status_label

    assert workflow_status_label('new') == 'Χωρίς ενέργεια'
