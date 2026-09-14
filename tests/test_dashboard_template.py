from pathlib import Path


def test_dashboard_gates_all_profiles_to_admin_and_keeps_actions_in_drawer():
    template = Path('app/templates/dashboard.html').read_text(encoding='utf-8')
    main = Path('app/main.py').read_text(encoding='utf-8')

    assert 'request.state.current_user.is_admin' in template
    assert 'href="/?profile_id=0&deadline_filter=active&user_status=all">Όλα τα προφίλ</a>' in template
    assert 'Φίλτρα & εισαγωγή' not in template
    assert 'Φίλτρα & ενέργειες' in template
    assert 'Ανανέωση σχετικότητας προφίλ' in template
    assert 'Ακριβή CPV matches' in main
    assert 'Ευρύτερα / child CPV matches' in main
    assert 'Ακριβές match' in template
    assert 'Μερικό match' in template
    assert 'match-filter-bar' in template
    assert "match_filter_urls['exact_full']" in template
    assert "match_filter_urls['exact_partial']" in template
    assert "match_filter_urls['broad']" in template
    assert 'pagination.previous_url' in template
    assert 'pagination.next_url' in template


def test_dashboard_has_exact_deadline_range_and_expandable_cpv_list():
    template = Path('app/templates/dashboard.html').read_text(encoding='utf-8')

    assert 'type="date" name="deadline_from"' in template
    assert 'type="date" name="deadline_to"' in template
    assert '{% for cpv in s.preview_cpvs %}' in template
    assert '+{{ s.overflow_cpvs|length }} ακόμη CPV' in template
    assert '{% for cpv in s.overflow_cpvs %}' in template
    assert 's.tender.cpv_codes[:5]' not in template


def test_dashboard_manual_ingest_is_profile_specific():
    template = Path('app/templates/dashboard.html').read_text(encoding='utf-8')

    assert 'Εισαγωγή ΚΗΜΔΗΣ για αυτό το προφίλ' in template
    assert 'Ψάχνει στο ΚΗΜΔΗΣ τις τελευταίες Χ ημέρες μόνο με τα CPV του επιλεγμένου προφίλ.' in template
    assert '<input type="hidden" name="profile_id" value="{{ profile_id or 0 }}">' in template
    assert 'Η αυτόματη ημερήσια εισαγωγή συνεχίζει να ενημερώνει όλα τα ενεργά προφίλ.' in template


def test_reports_are_profile_oriented_without_all_profiles_option():
    template = Path('app/templates/reports.html').read_text(encoding='utf-8')

    assert '<option value="0"' not in template
    assert 'Οι αναφορές είναι ανά προφίλ' in template
    assert "selected_profile else 'Δεν έχει επιλεγεί'" in template
