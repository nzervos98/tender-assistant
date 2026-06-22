from app.models import ClientProfile, Tender
from app.services.cpv_catalog import cpv_catalog_size, cpv_descendant_codes, cpv_selected_ancestor, expand_cpv_codes_for_ingest
from app.services.profiles import collect_cpv_codes
from app.services.scoring import rule_score_tender


def test_parent_cpv_expands_to_known_children_for_ingest():
    children = cpv_descendant_codes('33000000-0')
    assert '33100000-1' in children
    assert '33600000-6' in children

    expanded = expand_cpv_codes_for_ingest(['33000000-0'])
    assert expanded[0] == '33000000-0'
    assert '33100000-1' in expanded
    assert '33600000-6' in expanded


def test_collect_cpv_codes_expands_known_children_by_default():
    profile = ClientProfile(
        slug='health',
        name='Health',
        cpv_codes=['33000000-0'],
        cpv_prefixes=['33'],
    )
    expanded = collect_cpv_codes([profile])
    assert '33000000-0' in expanded
    assert '33100000-1' in expanded
    assert '33600000-6' in expanded

    selected_only = collect_cpv_codes([profile], expand_known_children=False)
    assert selected_only == ['33000000-0']


def test_child_cpv_scores_as_family_match_below_exact_match():
    parent_profile = ClientProfile(
        slug='health-parent',
        name='Health parent',
        cpv_codes=['33000000-0'],
        cpv_prefixes=['33'],
        keywords=[],
        negative_keywords=[],
        required_certificates=[],
    )
    exact_profile = ClientProfile(
        slug='health-exact',
        name='Health exact',
        cpv_codes=['33100000-1'],
        cpv_prefixes=[],
        keywords=[],
        negative_keywords=[],
        required_certificates=[],
    )
    tender = Tender(
        source='test',
        source_reference='health-1',
        title='Προμήθεια ιατρικών συσκευών',
        cpv_codes=['33100000-1'],
        cpv_descriptions={'33100000-1': 'Ιατρικές συσκευές'},
    )

    family_result = rule_score_tender(tender, parent_profile)
    exact_result = rule_score_tender(tender, exact_profile)

    assert '33100000-1' in family_result.matched_cpv
    assert family_result.score < exact_result.score
    assert any('παιδιού/οικογένειας CPV' in reason for reason in family_result.reasons)
    assert cpv_selected_ancestor('33100000-1', ['33000000-0']) == '33000000-0'
    assert any(('Ακριβές ταίριασμα ειδικού CPV' in reason or 'Δηλωμένος γονικός CPV' in reason) for reason in exact_result.reasons)


def test_khmdhs_visible_tree_codes_are_searchable_and_expandable():
    from app.services.cpv_catalog import cpv_search

    search_codes = [entry.code for entry in cpv_search('νεκροψίας', limit=10)]
    assert '33900000-9' in search_codes

    health_children = cpv_descendant_codes('33000000-0')
    assert '33700000-7' in health_children
    assert '33900000-9' in health_children

    agriculture_children = cpv_descendant_codes('03000000-1')
    assert '03100000-2' in agriculture_children
    assert '03111100-3' in agriculture_children



def test_full_cpv_catalog_is_loaded_from_khmdhs_tree():
    assert cpv_catalog_size() >= 9000
    health_children = cpv_descendant_codes('33000000-0')
    assert '33100000-1' in health_children
    assert '33600000-6' in health_children
    assert '33711540-4' in health_children
    assert cpv_selected_ancestor('33711540-4', ['33000000-0']) == '33000000-0'
