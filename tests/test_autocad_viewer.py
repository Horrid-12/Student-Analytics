from fastapi.testclient import TestClient

from app import main as main_module
from app.main import app
from test_pages_36 import make_user


def test_autocad_page_renders_the_viewer_bundle_url():
    with TestClient(app) as client:
        make_user(client, role="student")
        response = client.get("/autocad")

    assert response.status_code == 200
    assert 'var BUNDLE_URL = "http://testserver/static/cad-viewer/cad-viewer.js";' in response.text
    assert 'id="cadViewerModal"' in response.text
    assert 'id="cadViewerFit"' not in response.text
    assert "Fit view" not in response.text


def test_autocad_staff_submissions_render_as_onboarding_profile_cards(monkeypatch):
    monkeypatch.setattr(
        main_module.autocad,
        "list_submissions",
        lambda **kwargs: [{
            "id": 7,
            "student_email": "student@college.edu",
            "student_id": "1234567890",
            "student_name": "Test Student",
            "filename": "floor-plan.dwg",
            "file_size": 1_048_576,
            "created_at": "2026-11-25T10:30:00+00:00",
        }],
    )
    monkeypatch.setattr(
        main_module.auth,
        "get_student_accounts",
        lambda: [{
            "email": "student@college.edu",
            "degree_branch": "Computer Engineering",
            "division": "A",
            "practical_batch": "2",
            "semester": "Semester 4",
        }],
    )

    with TestClient(app) as client:
        make_user(client, role="admin")
        response = client.get("/autocad")

    assert response.status_code == 200
    assert 'class="autocad-panel autocad-submission-card"' in response.text
    assert 'class="autocad-submission-avatar"' in response.text
    assert "Test Student" in response.text
    assert "1234567890" in response.text
    assert "Computer Engineering" in response.text
    assert "Practical batch" in response.text
    assert "Semester 4" in response.text
    assert "floor-plan.dwg" in response.text
    assert "DWG submission" in response.text
