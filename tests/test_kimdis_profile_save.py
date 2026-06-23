from pathlib import Path


def test_kimdis_search_save_is_profile_oriented_and_marks_saved():
    main = Path('app/main.py').read_text(encoding='utf-8')
    template = Path('app/templates/kimdis_search.html').read_text(encoding='utf-8')

    save_block = main[main.index("@app.post('/kimdis/save'"):main.index("@app.post('/scores/{score_id}/workflow'")]
    assert "profile_id: str = Form(...)" in main
    assert "ClientProfile.id == selected_profile_id" in main
    assert "score_and_store(db, tender, profile, AIService())" in main
    assert "for profile in profiles:" not in save_block
    assert "score.user_status = 'saved'" in save_block
    assert "score.status_updated_at = now_utc()" in save_block
    assert "'profile_id': profile.id" in main
    assert "?profile_id={profile.id}" in main

    assert 'Προφίλ αποθήκευσης / βαθμολόγησης' in template
    assert 'name="profile_id"' in template
    assert 'value="{{ selected_profile_id }}"' in template
    assert 'Αποθήκευση & βαθμολόγηση στο προφίλ' in template
    assert 'Βαθμολόγηση στο επιλεγμένο προφίλ' in template
    assert 'Ήδη βαθμολογημένο για το επιλεγμένο προφίλ' in template
    assert '/tenders/{{ item.saved_id }}/delete' in template
