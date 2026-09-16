from pathlib import Path


def test_dashboard_gates_all_profiles_to_admin_and_keeps_actions_in_drawer():
    template = Path('app/templates/dashboard.html').read_text(encoding='utf-8')
    main = Path('app/main.py').read_text(encoding='utf-8')

    assert 'request.state.current_user.is_admin' in template
    assert 'href="/?profile_id=0&deadline_filter=active&user_status=all">Όλα τα προφίλ</a>' in template
    assert 'Φίλτρα & εισαγωγή' not in template
    assert 'class="button dashboard-sidebar-toggle"' in template
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

    assert 'type="text" name="deadline_from"' in template
    assert 'type="text" name="deadline_to"' in template
    assert template.count('placeholder="ηη/μμ/εεεε"') >= 2
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


def test_dashboard_sidebar_is_compact_and_notes_are_not_buried_in_more_details():
    template = Path('app/templates/dashboard.html').read_text(encoding='utf-8')

    sidebar = template.split('<aside class="side-drawer"', 1)[1].split('</aside>', 1)[0]
    more_panel = template.split('<summary>Περισσότερα</summary>', 1)[1].split('</details>', 1)[0]
    assert '<h3>Σύνοψη</h3>' not in sidebar
    assert 'class="sidebar-filter-more"' in sidebar
    assert 'class="drawer-section sidebar-tools"' in sidebar
    assert 'Φίλτρα{% if active_filters %}<span class="filter-count">' in template
    assert '>Σύνοψη</label>' not in template
    assert '<div class="result-note">' in template
    assert "'Επεξεργασία σημείωσης' if s.user_notes else 'Προσθήκη σημείωσης'" in template
    assert 'name="user_notes"' not in more_panel


def test_dashboard_filter_reset_and_job_progress_follow_the_full_workflow():
    dashboard_template = Path('app/templates/dashboard.html').read_text(encoding='utf-8')
    base_template = Path('app/templates/base.html').read_text(encoding='utf-8')
    main = Path('app/main.py').read_text(encoding='utf-8')

    assert 'deadline_filter=active&user_status=all&min_score=0">Καθαρισμός όλων' in dashboard_template
    assert "result.continuation_job?.id || result.rescore_job?.id" in base_template
    assert "follow(nextStream)" in base_template
    assert 'tender-assistant-active-job:' in base_template
    assert 'data-job-restored' in base_template
    assert 'window.localStorage.setItem(storageKey' in base_template
    assert 'Η εργασία συνεχίζεται, ακόμη και μετά από αλλαγή σελίδας ή ανανέωση.' not in base_template
    assert "yield ': keep-alive\\n\\n'" in main


def test_reports_are_profile_oriented_without_all_profiles_option():
    template = Path('app/templates/reports.html').read_text(encoding='utf-8')

    assert '<option value="0"' not in template
    assert 'Οι αναφορές είναι ανά προφίλ' in template
    assert "selected_profile else 'Δεν έχει επιλεγεί'" in template
    assert 'type="text" name="deadline_from"' in template
    assert 'type="text" name="deadline_to"' in template
    assert '+{{ s.overflow_cpvs|length }} ακόμη CPV' in template


def test_profile_form_hides_internal_slug_and_keeps_admin_owner_assignment():
    template = Path('app/templates/profile_form.html').read_text(encoding='utf-8')

    assert '<summary>Τεχνικά στοιχεία</summary>' not in template
    assert '<input type="hidden" name="slug" value="{{ profile.slug }}">' in template
    assert 'Ιδιοκτήτης προφίλ' in template


def test_new_profile_starts_initial_kimdis_ingest_and_detail_exposes_submission_link():
    dashboard = Path('app/templates/dashboard.html').read_text(encoding='utf-8')
    detail = Path('app/templates/tender.html').read_text(encoding='utf-8')
    main = Path('app/main.py').read_text(encoding='utf-8')

    assert "job_type='ingest'" in main
    assert "'initial_profile_ingest': True" in main
    assert 'INITIAL_PROFILE_INGEST_DAYS' in Path('app/config.py').read_text(encoding='utf-8')
    assert 'αρχική αναζήτηση ΚΗΜΔΗΣ {{ settings.initial_profile_ingest_days }} ημερών' in dashboard
    assert 'tender_bidding_website(tender)' in detail
    assert 'Πλατφόρμα υποβολής' in detail
    assert 'Έλεγχος απαιτήσεων PDF' in detail


def test_latest_ingest_counter_explains_new_items_and_keeps_workflow_shortcuts():
    template = Path('app/templates/dashboard.html').read_text(encoding='utf-8')

    assert 'νέα ενεργά για το προφίλ' in template
    assert 'ενεργά πάνω από το όριο σχετικότητας' not in template
    assert 'class="workflow-quick-links"' in template
    assert '<strong>{{ summary.new_items }}</strong> νέα τελευταίας εισαγωγής' in template
    assert '<strong>{{ summary.saved }}</strong> αποθηκευμένα' in template
    assert '<strong>{{ summary.reviewing }}</strong> σε έλεγχο' in template
    assert '<div class="summary-strip">' not in template
    assert 'ενεργά προς έλεγχο</span>' not in template
    assert 'υψηλής προτεραιότητας</span>' not in template
    assert 'λήγουν μέσα σε 7 ημέρες</span>' not in template


def test_opportunity_badge_is_only_used_for_mixed_advanced_kimdis_results():
    dashboard = Path('app/templates/dashboard.html').read_text(encoding='utf-8')
    detail = Path('app/templates/tender.html').read_text(encoding='utf-8')
    kimdis = Path('app/templates/kimdis_search.html').read_text(encoding='utf-8')

    assert 'context.friendly_label' not in dashboard
    assert 'ctx.friendly_label' not in detail
    assert "{% if view == 'advanced' %}<span class=\"badge\">{{ ctx.friendly_label }}</span>{% endif %}" in kimdis
