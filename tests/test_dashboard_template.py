from pathlib import Path


def test_dashboard_hides_all_profiles_tab_and_topbar_rescore():
    template = Path('app/templates/dashboard.html').read_text()

    assert 'Όλα τα προφίλ' not in template
    assert 'href="/?profile_id=0&deadline_filter=active&user_status=all">Όλα</a>' not in template
    assert 'Φίλτρα & εισαγωγή' not in template
    assert 'Φίλτρα & ενέργειες' in template
    assert 'Ανανέωση σχετικότητας προφίλ' in template


def test_dashboard_manual_ingest_is_profile_specific():
    template = Path('app/templates/dashboard.html').read_text()

    assert 'Εισαγωγή ΚΗΜΔΗΣ για αυτό το προφίλ' in template
    assert 'Ψάχνει στο ΚΗΜΔΗΣ τις τελευταίες Χ ημέρες μόνο με τα CPV του επιλεγμένου προφίλ.' in template
    assert '<input type="hidden" name="profile_id" value="{{ profile_id or 0 }}">' in template
    assert 'Η αυτόματη ημερήσια εισαγωγή συνεχίζει να ενημερώνει όλα τα ενεργά προφίλ.' in template


def test_reports_are_profile_oriented_without_all_profiles_option():
    template = Path('app/templates/reports.html').read_text()

    assert '<option value="0"' not in template
    assert 'Οι αναφορές είναι ανά προφίλ' in template
    assert "selected_profile else 'Δεν έχει επιλεγεί'" in template
