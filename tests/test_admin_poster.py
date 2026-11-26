"""Admin A3 poster page: access control and print-ready markup."""

import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app import auth
from app.main import app


def make_client(role: str) -> TestClient:
    client = TestClient(app)
    email = f"{role}-{uuid.uuid4().hex[:6]}@college.edu"
    assert auth.create_user(email, "secret123", role, "Poster Tester")
    response = client.post("/login", data={"email": email, "password": "secret123"})
    assert response.status_code in (200, 302)
    return client


def test_admin_poster_renders_a3_sheet():
    client = make_client("admin")
    response = client.get("/admin/poster")
    assert response.status_code == 200
    body = response.text
    assert "Class Metrics Radar" in body
    assert "radar-svg" in body
    assert "Under the hood" in body
    assert "student-analytics-iota.vercel.app" in body
    poster_css = (Path(__file__).resolve().parent.parent / "static" / "poster.css").read_text(encoding="utf-8")
    assert "width: 420mm" in poster_css
    assert "height: 297mm" in poster_css


def test_admin_poster_clean_mode_hides_controls():
    client = make_client("admin")
    body = client.get("/admin/poster?clean=1").text
    assert '<body class="poster-root clean">' in body
    assert "Screenshot mode" in body
    poster_css = (Path(__file__).resolve().parent.parent / "static" / "poster.css").read_text(encoding="utf-8")
    assert "body.clean .tools { display: none !important; }" in poster_css


def test_admin_poster_redirects_students_home():
    client = make_client("student")
    response = client.get("/admin/poster", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_admin_poster_redirects_anonymous_to_login():
    client = TestClient(app)
    response = client.get("/admin/poster", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login"


def test_admin_settings_links_to_poster():
    client = make_client("admin")
    body = client.get("/settings").text
    assert "A3 Platform Poster" in body
    assert 'href="/admin/poster"' in body


def test_admin_overview_does_not_link_to_poster():
    client = make_client("admin")
    body = client.get("/").text
    assert "A3 Poster" not in body
    assert 'href="/admin/poster"' not in body


def test_student_settings_hides_poster_link():
    client = make_client("student")
    body = client.get("/settings").text
    assert "A3 Platform Poster" not in body
