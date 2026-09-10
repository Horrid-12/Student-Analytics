"""Tests for the 3.6 page ports.

End-to-end over TestClient with a fake GitHub backend: upload the synthetic
roster, run both batches (state accumulates & completes, the run is recorded
into a temp history DB), then assert every ported page renders with data,
filters/export/workflow behave, and pages without an analysis show the legacy
placeholder. No network, no Streamlit.
"""

import io
import json
from pathlib import Path
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import storage
from app.main import app, roster_store

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

import app.services as psvc


def make_roster_xlsx(rows=None) -> io.BytesIO:
    from services import EXCEL_COLUMNS

    df = pd.DataFrame(rows or roster_rows(), columns=EXCEL_COLUMNS)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False)
    buf.name = "roster.xlsx"
    buf.seek(0)
    return buf


def roster_rows() -> list[dict]:
    from services import REQUIRED_EXCEL_COLUMNS

    return [
        {
            "Timestamp": "2025-08-01 10:00:00",
            "PRN No": "101.0",
            "Student Name": "Alice Example",
            "Division": "A",
            "Batch": "2026",
            "Actual GitHub Account Link:": "https://github.com/alice-dev",
        },
        {
            "Timestamp": "2025-08-01 10:05:00",
            "PRN No": "202.0",
            "Student Name": "Bob Example",
            "Division": "B",
            "Batch": "2026",
            "Actual GitHub Account Link:": "https://github.com/bob-cat",
        },
    ]


class FakeGitHub:
    """Mirror of tests/test_batch.py::FakeGitHub."""

    def __init__(self, users, repos, contributions=None):
        self.users = users
        self.repos = repos
        self.contributions = contributions or {}

    def __call__(self, url, token, timeout=None):
        from services import GITHUB_API_BASE

        if "/search/issues" in url:
            username = url.split("q=author%3A")[1].split("+")[0]
            prs, issues = self.contributions.get(username, ([], []))
            items = prs if "type%3Apr" in url else issues
            return 200, {}, {"items": items}
        after_base = url[len(GITHUB_API_BASE):]
        if after_base.startswith("/users/") and "/repos" not in after_base:
            username = after_base.split("/")[2]
            return 200, {}, self.users[username]
        if "/repos?" in after_base:
            username = after_base.split("/")[2]
            return 200, {}, self.repos.get(username, [])
        raise AssertionError(f"unexpected url: {url}")


def user_payload(username, public_repos=3, followers=10, following=5):
    return {
        "login": username,
        "public_repos": public_repos,
        "followers": followers,
        "following": following,
        "created_at": "2020-01-01T00:00:00Z",
        "html_url": f"https://github.com/{username}",
        "avatar_url": f"https://avatars/{username}",
    }


def repo_item(name, language, updated="2026-07-01T00:00:00Z"):
    return {
        "name": name,
        "language": language,
        "stargazers_count": 1,
        "forks_count": 1,
        "description": "desc",
        "license": {"spdx_id": "MIT"},
        "created_at": "2021-01-01T00:00:00Z",
        "updated_at": updated,
        "html_url": f"https://github.com/alice-dev/{name}",
    }


def crash_free_fake() -> FakeGitHub:
    return FakeGitHub(
        users={
            "alice-dev": user_payload("alice-dev", public_repos=3),
            "bob-cat": user_payload("bob-cat", public_repos=2, followers=4, following=1),
        },
        repos={
            "alice-dev": [repo_item("py1", "Python"), repo_item("js1", "JavaScript")],
            "bob-cat": [repo_item("go1", "Go")],
        },
        contributions={
            "alice-dev": (
                [{"state": "open", "repository_url": "https://api.github.com/repos/o/thing"}],
                [{"state": "closed", "repository_url": "https://api.github.com/repos/o/thing"}],
            ),
            "bob-cat": ([], []),
        },
    )


def patch_app_pipeline(monkeypatch, fake):
    monkeypatch.setattr(psvc.time, "sleep", lambda _: None)
    monkeypatch.setattr(psvc, "_cached_get_json", fake)


@pytest.fixture
def client(tmp_path):
    storage.DB_PATH = tmp_path / "analytics_history.db"
    test_client = TestClient(app)
    yield test_client


def upload_roster(client):
    buf = make_roster_xlsx()
    response = client.post("/upload", files={"file": ("roster.xlsx", buf.getvalue(), XLSX_MIME)})
    assert response.status_code == 200
    return response.json()


def run_all_batches(client, data):
    roster_id = data["roster_id"]
    ids = [s["student_id"] for s in data["students"]]
    for sid in ids:
        response = client.post(
            "/analysis/batch",
            json={"roster_id": roster_id, "student_ids": [sid]},
        )
        assert response.status_code == 200
    return roster_id


class TestPageRenderingWithData:
    def setup_method(self):
        self.monkeypatch = pytest.MonkeyPatch()

    def teardown_method(self):
        self.monkeypatch.undo()

    def _setup(self, tmp_path):
        self.monkeypatch.setattr(storage, "DB_PATH", tmp_path / "analytics_history.db")
        self.client = TestClient(app)
        patch_app_pipeline(self.monkeypatch, crash_free_fake())
        data = upload_roster(self.client)
        roster_id = run_all_batches(self.client, data)
        return roster_id

    def test_overview_renders_complete_analysis(self, tmp_path):
        roster_id = self._setup(tmp_path)
        body = self.client.get(f"/?roster={roster_id}").text
        assert "Student Analytics Workspace" not in body
        assert "Key Metrics" in body
        assert "Account Validation Status" in body
        assert "Analysis Pipeline" in body
        assert "Run Log" in body
        assert "plotly" in body or "Plotly.react" in body

    def test_overview_without_roster_shows_empty_state(self, tmp_path):
        self.monkeypatch.setattr(storage, "DB_PATH", tmp_path / "analytics_history.db")
        self.client = TestClient(app)
        body = self.client.get("/").text
        assert "Student Analytics Workspace" in body
        assert "No student data loaded yet" in body

    def test_students_page_renders_rows_and_profile(self, tmp_path):
        roster_id = self._setup(tmp_path)
        body = self.client.get(f"/students?roster={roster_id}").text
        assert "Student Explorer" in body
        assert "Alice Example" in body
        assert "Subject" in body or "Metrics" in body or "Select a student" in body

        profile = self.client.get(f"/students?roster={roster_id}&select=101").text
        assert "Recent Repositories" in profile

    def test_students_filter_narrows_results(self, tmp_path):
        roster_id = self._setup(tmp_path)
        body = self.client.get(f"/students?roster={roster_id}&q=alice").text
        assert "Alice Example" in body
        assert "Bob Example" not in body

    def test_repositories_page(self, tmp_path):
        roster_id = self._setup(tmp_path)
        body = self.client.get(f"/repositories?roster={roster_id}").text
        assert "Repository Cards" in body
        assert "py1" in body
        assert "All Repositories" in body

    def test_leaderboards_page(self, tmp_path):
        roster_id = self._setup(tmp_path)
        body = self.client.get(f"/leaderboards?roster={roster_id}").text
        assert "Most Public Repositories" in body
        assert "Most-Followed GitHub Profiles" in body
        assert "Top Languages by Repositories" in body
        assert "Alice Example" in body

    def test_verification_page_and_export(self, tmp_path):
        roster_id = self._setup(tmp_path)
        body = self.client.get(f"/verification?roster={roster_id}").text
        assert "Account Verification Audit" in body
        assert "Verified" in body

        csv = self.client.get(f"/verification/export?roster={roster_id}").text
        assert "Student Name" in csv
        assert "Alice Example" in csv

    def test_issues_page_and_workflow_save(self, tmp_path):
        roster_id = self._setup(tmp_path)
        body = self.client.get(f"/issues?roster={roster_id}").text
        assert "Issues" in body and "Student" in body

        response = self.client.post(
            "/issues/workflow?roster=" + roster_id,
            json={"101|test|alice-dev": {"Status": "Resolved", "Owner": "faculty", "Notes": "fixed"}},
        )
        assert response.status_code == 200
        workflow = roster_store.get_workflow(roster_id)
        assert workflow["101|test|alice-dev"]["Status"] == "Resolved"

    def test_students_export_csv_and_xlsx(self, tmp_path):
        roster_id = self._setup(tmp_path)
        csv = self.client.get(f"/students/export?roster={roster_id}&format=csv")
        assert csv.status_code == 200
        assert "text/csv" in csv.headers["content-type"]
        assert "Alice Example" in csv.text

        xlsx = self.client.get(f"/students/export?roster={roster_id}&format=xlsx")
        assert xlsx.status_code == 200
        assert "spreadsheetml" in xlsx.headers["content-type"]

    def test_demo_metrics_route_removed(self, tmp_path):
        self.monkeypatch.setattr(storage, "DB_PATH", tmp_path / "analytics_history.db")
        self.client = TestClient(app)
        res = self.client.get("/overview/partial")
        assert res.status_code == 404

    def test_verification_export_respects_status_filter(self, tmp_path):
        self.monkeypatch.setattr(storage, "DB_PATH", tmp_path / "analytics_history.db")
        self.client = TestClient(app)
        patch_app_pipeline(self.monkeypatch, crash_free_fake())
        rows = roster_rows() + [
            {
                "Timestamp": "2025-08-01 10:10:00",
                "PRN No": "303.0",
                "Student Name": "Carol None",
                "Division": "B",
                "Batch": "2026",
                "Actual GitHub Account Link:": "",
            }
        ]
        buf = make_roster_xlsx(rows)
        data = self.client.post(
            "/upload", files={"file": ("roster.xlsx", buf.getvalue(), XLSX_MIME)}
        ).json()
        roster_id = run_all_batches(self.client, data)
        page = self.client.get(f"/verification?roster={roster_id}&status=Missing").text
        assert "status=Missing" in page
        assert "Carol None" in page
        csv = self.client.get(f"/verification/export?roster={roster_id}&status=Missing").text
        assert "Carol None" in csv
        assert "Alice Example" not in csv

    def test_csv_export_has_utf8_bom(self, tmp_path):
        roster_id = self._setup(tmp_path)
        raw = self.client.get(f"/students/export?roster={roster_id}&format=csv")
        assert raw.content.startswith(b"\xef\xbb\xbf")
        assert "Alice Example" in raw.content.decode("utf-8-sig")

    def test_history_records_completed_run(self, tmp_path):
        roster_id = self._setup(tmp_path)
        body = self.client.get("/history").text
        assert "Analysis Runs" in body
        assert "Complete" in body
        runs = storage.load_run_history()
        assert len(runs) == 1
        assert int(runs.iloc[0]["valid_accounts"]) == 2
        assert int(runs.iloc[0]["repos_found"]) == 3
        assert int(runs.iloc[0]["active_repos"]) > 0
        assert runs.iloc[0]["source_file_hash"] is not None

    def test_run_recorded_only_once(self, tmp_path):
        roster_id = self._setup(tmp_path)
        ids = roster_store.get(roster_id)
        first_ids = [str(row["Student_ID"]) for row in ids[:1]]
        self.client.post(
            "/analysis/batch",
            json={"roster_id": roster_id, "student_ids": first_ids},
        )
        assert len(storage.load_run_history()) == 1

    def test_pages_without_analysis_show_placeholder(self, tmp_path):
        self.monkeypatch.setattr(storage, "DB_PATH", tmp_path / "analytics_history.db")
        self.client = TestClient(app)
        data = upload_roster(self.client)
        roster_id = data["roster_id"]
        for path in ("students", "repositories", "leaderboards", "issues", "verification"):
            body = self.client.get(f"/{path}?roster={roster_id}").text
            assert "populates after you upload a roster" in body
        body = self.client.get("/students").text
        assert "populates after you upload a roster" in body

    def test_export_without_analysis_404(self, tmp_path):
        self.monkeypatch.setattr(storage, "DB_PATH", tmp_path / "analytics_history.db")
        self.client = TestClient(app)
        data = upload_roster(self.client)
        response = self.client.get(f"/students/export?roster={data['roster_id']}")
        assert response.status_code == 404


class TestVercelEntrypoint:
    """api/index.py wraps the FastAPI app with a Vercel-prefix stripper so the
    rewrite  /(.*) -> /api/index  still routes to '/', '/students', etc."""

    def test_prefix_stripped_for_root(self):
        from api.index import Mangum, wrapped

        client = TestClient(wrapped, raise_server_exceptions=False)
        assert client.get("/api/index").status_code == 200
        assert "Student Analytics" in client.get("/api/index").text or "student" in client.get("/api/index").text.lower()

    def test_prefix_stripped_for_pages(self):
        from api.index import wrapped

        client = TestClient(wrapped, raise_server_exceptions=False)
        assert client.get("/api/index/history").status_code == 200
        assert client.get("/api/index/students").status_code == 200

    def test_real_paths_unaffected(self):
        from api.index import wrapped

        client = TestClient(wrapped, raise_server_exceptions=False)
        assert client.get("/history").status_code == 200

    def test_no_prefix_stripped_from_deep_static(self):
        from api.index import wrapped

        client = TestClient(wrapped, raise_server_exceptions=False)
        css = client.get("/api/index/static/style.css")
        assert css.status_code in (200, 404)

    def test_py_function_path_forms(self):
        from api.index import wrapped

        client = TestClient(wrapped, raise_server_exceptions=False)
        assert client.get("/api/index.py").status_code == 200
        assert client.get("/api/index.py/history").status_code == 200
        assert client.get("/api/history").status_code == 200


class TestSettingsPage:
    """BUG-085 — Settings was dropped in the new stack; the gear link was a dead
    #settings fragment with no selector/route/JS. /settings now renders a storage
    health card + theme toggle, and the gear targets the real route."""

    def _client(self, tmp_path, monkeypatch):
        monkeypatch.setattr(storage, "DB_PATH", tmp_path / "settings.db")
        return TestClient(app, raise_server_exceptions=True)

    def test_settings_route_renders(self, tmp_path, monkeypatch):
        client = self._client(tmp_path, monkeypatch)
        r = client.get("/settings")
        assert r.status_code == 200
        assert "Run History Storage" in r.text
        assert "gsad_theme_v1" in r.text
        assert "data-theme-btn" in r.text

    def test_gear_link_targets_settings_route(self, tmp_path, monkeypatch):
        client = self._client(tmp_path, monkeypatch)
        r = client.get("/")
        assert 'href="/settings"' in r.text
        assert 'href="#settings"' not in r.text

    def test_storage_healthy_true_on_writable_tmp_db(self, tmp_path, monkeypatch):
        monkeypatch.setattr(storage, "DB_PATH", tmp_path / "settings.db")
        assert storage.storage_healthy() is True


class TestCssThemeHygiene:
    """BUG-100: live app CSS must not hardcode blue accent rgba values."""

    STREAMLIT_MARKERS = (
        "stSidebar", "stButton", "stDownloadButton", "stPopover",
        "stFileUploader", "data-testid=\"stBaseButton", "stBaseButton-primary",
    )
    BLUE_RGBA = "rgba(59, 130, 246"

    def _live_rules(self, css_text):
        """Declaration lines that hardcode blue rgba, skipping rules whose
        selector targets Streamlit (legacy dead) components."""
        selected = []
        n = len(css_text)
        i = 0
        while i < n:
            if css_text[i] == "}":
                i += 1
                continue
            brace = css_text.find("{", i)
            if brace == -1:
                break
            head = css_text[i:brace]
            depth = 0
            j = brace
            end = -1
            while j < n:
                if css_text[j] == "{":
                    depth += 1
                elif css_text[j] == "}":
                    depth -= 1
                    if depth == 0:
                        end = j
                        break
                j += 1
            if end == -1:
                break
            body = css_text[brace + 1:end]
            if self.BLUE_RGBA in body and not any(m in head for m in self.STREAMLIT_MARKERS):
                for decl in body.splitlines():
                    if self.BLUE_RGBA in decl:
                        selected.append(decl.strip())
            i = end + 1
        return selected

    def test_theme_blue_alpha_vars_defined(self):
        theme_root = Path(__file__).resolve().parents[1] / "static" / "theme.css"
        css = theme_root.read_text(encoding="utf-8")
        for var in ("--blue-active-bg", "--blue-active-border", "--blue-hover-border",
                    "--blue-card-hover-border", "--blue-badge-border", "--blue-icon-bg"):
            assert var in css, f"{var} missing from theme.css"

    def test_no_hardcoded_blue_rgba_in_live_css(self):
        static_dir = Path(__file__).resolve().parents[1] / "static"
        for name in ("layout.css", "style.css"):
            css = (static_dir / name).read_text(encoding="utf-8")
            offenders = self._live_rules(css)
            assert not offenders, f"{name} live rules still hardcode blue rgba: {offenders}"


class TestSidebarIdentityContext:
    """BUG-098: sidebar/account identity must be context-driven, never hardcoded
    in templates — Phase 4.7 auth overrides the context values per role."""

    def test_identity_values_render_from_context(self, tmp_path, monkeypatch):
        monkeypatch.setattr(storage, "DB_PATH", tmp_path / "ident.db")
        client = TestClient(app)
        settings = client.get("/settings").text
        assert "Faculty Workspace" in settings       # sidebar brand edition
        assert "anonymous" in settings               # footer user name
        assert "Open Access" in settings             # settings account footer

    def test_base_template_uses_context_variables(self):
        base = Path(__file__).resolve().parent.parent / "app" / "templates" / "base.html"
        source = base.read_text(encoding="utf-8")
        assert "{{ brand_edition }}" in source
        assert "{{ auth_user }}" in source
        assert "{{ auth_status }}" in source
        assert "Faculty Workspace" not in source     # hardcoded string must be gone

    def test_settings_template_uses_context_variables(self):
        settings = (
            Path(__file__).resolve().parent.parent / "app" / "templates" / "pages" / "settings.html"
        )
        source = settings.read_text(encoding="utf-8")
        assert "{{ auth_role }}" in source
        assert "{{ auth_user }}" in source
        assert "{{ auth_footer }}" in source
        for hardcoded in ("Faculty Workspace", 'user-name">anonymous', "Connected &bull; Open Access"):
            assert hardcoded not in source


class TestNotFoundRouting:
    """Catch-all friendly 404 page for typo and unknown slugs."""

    def test_unknown_slug_returns_friendly_404_html(self):
        client = TestClient(app)
        res = client.get("/nonexistent-typo-slug")
        assert res.status_code == 404
        assert "text/html" in res.headers.get("content-type", "")
        assert "Page Not Found" in res.text
        assert "Go to Overview" in res.text

    def test_nested_typo_route_returns_friendly_404_html(self):
        client = TestClient(app)
        res = client.get("/students/typo/unknown")
        assert res.status_code == 404
        assert "text/html" in res.headers.get("content-type", "")
        assert "Page Not Found" in res.text
        assert "Go to Overview" in res.text

    def test_overview_route_renders_overview_page(self):
        client = TestClient(app)
        res = client.get("/overview")
        assert res.status_code == 200
        assert "Overview" in res.text
        assert "Student Analytics Workspace" in res.text
        assert "No student data loaded yet" in res.text