"""Privacy Policy integration: reachable from Settings, modern layout.

Covers the Settings Legal entry point, the redesigned privacy page (breadcrumb,
contents card, section cards, no legacy inline styles), and the pre-existing
login-footer link. /privacy stays public so logged-out users can read it.
"""

from pathlib import Path

from fastapi.testclient import TestClient

from app import auth, storage
from app.main import app

from test_pages_36 import make_user


def client(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "privacy_history.db")
    monkeypatch.setattr(auth, "USERS_DB", tmp_path / "users.db")
    monkeypatch.setenv("ALLOWED_OAUTH_DOMAINS", "college.edu")
    return TestClient(app)


SECTIONS = [
    "introduction",
    "information-we-collect",
    "how-we-use",
    "sharing",
    "third-parties",
    "contact",
]


class TestPrivacyPage:
    def test_anonymous_can_read_privacy(self, tmp_path, monkeypatch):
        body = client(tmp_path, monkeypatch).get("/privacy", headers={"Accept": "text/html"}).text
        assert "Privacy Policy" in body
        assert "privacy-body" in body
        assert "privacy-toc" not in body  # index removed; sections read top to bottom
        for section in SECTIONS:
            assert f'id="{section}"' in body

    def test_anonymous_breadcrumb_has_no_settings_link(self, tmp_path, monkeypatch):
        body = client(tmp_path, monkeypatch).get("/privacy", headers={"Accept": "text/html"}).text
        assert "aria-label=\"Breadcrumb\"" in body
        assert 'href="/settings"' not in body
        assert "Sign in" in body  # sidebar degrades sanely without a session

    def test_signed_in_breadcrumb_links_back_to_settings(self, tmp_path, monkeypatch):
        c = client(tmp_path, monkeypatch)
        make_user(c, "student")
        body = c.get("/privacy", headers={"Accept": "text/html"}).text
        assert 'href="/settings"' in body
        assert "Sign out" in body

    def test_no_legacy_inline_styles(self):
        source = (
            Path(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "privacy.html"
        ).read_text(encoding="utf-8")
        # The shared topbar shell keeps its app-wide inline wrapper; the
        # redesigned privacy content itself must be class-driven.
        content = source.split("privacy-body")[1]
        assert "style=\"" not in content


class TestSettingsLegalEntry:
    def test_admin_settings_links_privacy(self, tmp_path, monkeypatch):
        c = client(tmp_path, monkeypatch)
        make_user(c, "admin")
        body = c.get("/settings", headers={"Accept": "text/html"}).text
        assert "Legal" in body
        assert 'href="/privacy"' in body

    def test_student_settings_links_privacy(self, tmp_path, monkeypatch):
        c = client(tmp_path, monkeypatch)
        make_user(c, "student")
        body = c.get("/settings", headers={"Accept": "text/html"}).text
        assert 'href="/privacy"' in body


class TestLoginFooterLink:
    def test_login_footer_still_links_privacy(self, tmp_path, monkeypatch):
        body = client(tmp_path, monkeypatch).get("/login", headers={"Accept": "text/html"}).text
        assert 'href="/privacy"' in body
