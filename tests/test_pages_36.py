"""Tests for the 3.6 page ports.

End-to-end over TestClient with a fake GitHub backend: upload the synthetic
roster, run both batches (state accumulates & completes, the run is recorded
into a temp history DB), then assert every ported page renders with data,
filters/export/workflow behave, and pages without an analysis show the legacy
placeholder. No network, no Streamlit.
"""

import io
import json
import uuid
from pathlib import Path
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import auth, storage
from app.main import app, roster_store

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

import app.services as psvc


def make_user(client, role="admin"):
    """Seed a user directly and log them in so the session cookie persists on the
    TestClient for the rest of the test (Phase 4.7 auth gates every page)."""
    email = f"{role.lower()}-{uuid.uuid4().hex[:6]}@college.edu"
    assert auth.create_user(email, "secret123", role, "Test User"), "user seed failed"
    login = client.post("/login", data={"email": email, "password": "secret123"})
    assert login.status_code in (200, 302)
    return email


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

    def __init__(self, users, repos, contributions=None, events=None, repo_commits=None, repo_meta=None):
        self.users = users
        self.repos = repos
        self.contributions = contributions or {}
        self.events = events or {}
        self.repo_commits = repo_commits or {}
        self.repo_meta = repo_meta or {}

    def __call__(self, url, token, timeout=None):
        from services import GITHUB_API_BASE

        if "/search/issues" in url:
            username = url.split("q=author%3A")[1].split("+")[0]
            prs, issues = self.contributions.get(username, ([], []))
            items = prs if "type%3Apr" in url else issues
            return 200, {}, {"items": items}
        if "/events/public" in url:
            after_base = url[len(GITHUB_API_BASE):]
            username = after_base.split("/")[2]
            return 200, {}, list(self.events.get(username, []))
        if "/commits?" in url:
            try:
                full = url.split("/repos/")[1].split("/commits")[0]
                author = url.split("author=")[1].split("&")[0]
            except Exception:
                return 404, {}, None
            key = (full, author.lower())
            if key not in self.repo_commits:
                return 404, {}, None
            return 200, {}, list(self.repo_commits[key])
        if "/repos/" in url and "/commits" not in url:
            try:
                full = url.split("/repos/")[1].split("?")[0].strip("/")
                if full and "/" in full:
                    if full in self.repo_meta:
                        return 200, {}, dict(self.repo_meta[full])
                    return 404, {}, None
            except Exception:
                pass
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


def commit_item(days_ago) -> dict:
    stamp = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days_ago)).isoformat()
    return {"commit": {"author": {"date": stamp}}}


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
        # alice: py1 5 commits (2 in 30d, 3 in 90d) + js1 1 old = 6 all-time;
        # bob: go1 2 old commits.
        repo_commits={
            ("alice-dev/py1", "alice-dev"): [commit_item(d) for d in (0, 10, 40, 100, 400)],
            ("alice-dev/js1", "alice-dev"): [commit_item(200)],
            ("bob-cat/go1", "bob-cat"): [commit_item(200), commit_item(300)],
        },
    )


def patch_app_pipeline(monkeypatch, fake):
    monkeypatch.setattr(psvc.time, "sleep", lambda _: None)
    monkeypatch.setattr(psvc, "_cached_get_json", fake)


@pytest.fixture
def client(tmp_path, monkeypatch):
    storage.DB_PATH = tmp_path / "analytics_history.db"
    monkeypatch.setattr(auth, "USERS_DB", tmp_path / "users.db")
    test_client = TestClient(app)
    make_user(test_client, "admin")
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
        self.monkeypatch.setattr(auth, "USERS_DB", tmp_path / "users.db")
        self.client = TestClient(app)
        make_user(self.client, "admin")
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

    def test_overview_complete_render_supports_re_run_over_existing(self, tmp_path):
        """BUG-107 regression: a complete Overview must still carry the live
        pipeline-status ids (so updatePipelineStatus works during a re-run) and
        the previous-results wrapper the run script hides while a new run starts."""
        roster_id = self._setup(tmp_path)
        body = self.client.get(f"/?roster={roster_id}").text
        assert 'id="pipeline-status-badge"' in body
        assert 'id="pipeline-status-text"' in body
        assert 'id="pipeline-completed-at"' in body
        assert 'id="previous-results"' in body
        assert "prevResults.hidden = true" in body
        assert "restorePreviousResults" in body

    def test_overview_without_roster_shows_empty_state(self, tmp_path):
        self.monkeypatch.setattr(storage, "DB_PATH", tmp_path / "analytics_history.db")
        self.monkeypatch.setattr(auth, "USERS_DB", tmp_path / "users.db")
        self.client = TestClient(app)
        make_user(self.client, "admin")
        body = self.client.get("/").text
        assert "Student Analytics Workspace" in body
        assert "No student data loaded yet" in body

    def test_students_page_renders_rows_and_profile(self, tmp_path):
        roster_id = self._setup(tmp_path)
        body = self.client.get(f"/students?roster={roster_id}").text
        assert "Student Explorer" in body
        assert "Alice Example" in body
        assert "HackerRank" in body
        assert 'id="student-modal-backdrop"' not in body
        # Labeled filters, no academic-year or rows dropdowns.
        assert ">Division<" in body
        assert ">Batch<" in body
        assert ">Semester<" in body
        assert 'name="year"' not in body
        assert " rows</option>" not in body
        # Export lives in the filter-bar dropdown, not at the bottom.
        assert "export-dropdown" in body
        assert "Export CSV" not in body
        # Avatar opens the same profile popup as the name/ID links.
        assert 'class="avatar-link student-open-link"' in body
        assert "View profile of Alice Example" in body

        profile = self.client.get(f"/students?roster={roster_id}&select=101").text
        assert 'id="student-modal-backdrop"' in profile
        assert 'role="dialog"' in profile
        assert "student-table" in profile
        assert ".student-modal .profile-panel" in profile
        # Profile tabs: GitHub active with the repositories dropdown, the
        # other two panels present but hidden and empty for now.
        assert 'role="tablist"' in profile
        assert 'id="profile-tab-github"' in profile
        assert 'id="profile-panel-github"' in profile
        assert 'id="profile-panel-hackerrank"' in profile
        assert 'id="profile-panel-linkedin"' in profile
        assert "repo-dropdown" in profile
        assert "Repositories (" in profile
        assert "py1" in profile and "js1" in profile
        assert "Language Mix" not in profile
        assert "Top Languages" in profile
        assert "lang-row" in profile
        assert "lang-split" in profile
        assert "lang-divider" in profile
        # One segment per repo: Alice has 1 Python + 1 JavaScript repo.
        assert profile.count('<span class="lang-seg"></span>') == 2
        # Top languages fill the track; the rest scale against them.
        assert profile.count('class="lang-fill" style="width:100%"') == 2
        assert "Activity" in profile
        assert "Contributions in" in profile
        assert "Active repositories" in profile
        assert "Current activity streak" in profile
        # Per-repo exact commit counts: bold on the right, quality band moved
        # left beside the stars.
        assert "repo-commits" in profile
        assert "5 commits" in profile  # alice py1
        assert "1 commit<" in profile  # alice js1 (singular)
        assert "Python &middot; 1 stars &middot;" in profile
        # Activity tabs sit below the number ("Contributions in" + tabs):
        # 30D / 90D / All with 90D selected by default.
        # Alice: 2 in 30d, 3 in 90d, 6 all-time (team commits are 0 here).
        assert "activity-tab" in profile
        assert 'aria-pressed="true">90D<' in profile
        assert profile.index("data-activity-number") < profile.index("activity-tab")
        assert '<div class="activity-number" data-activity-number>3</div>' in profile
        assert 'data-activity-value="2"' in profile
        assert 'data-activity-value="3"' in profile
        assert 'data-activity-value="6"' in profile
        # Icon buttons replace the old text links: GitHub only (no
        # LinkedIn/HackerRank on this fixture roster).
        assert 'class="profile-icon-btn profile-icon-github"' in profile
        assert "https://github.com/alice-dev" in profile
        assert 'profile-icon-btn profile-icon-linkedin"' not in profile
        assert 'profile-icon-btn profile-icon-hackerrank"' not in profile
        assert "external-link-button" not in profile

    def test_profile_top_languages_ranked_with_percentages(self):
        import pandas as pd

        from app.views import students_payload_profile

        def repo(name, language, day):
            return {
                "Username": "u",
                "Repository": name,
                "Language": language,
                "Stars": 0,
                "Updated": f"2026-01-{day:02d}T00:00:00Z",
                "Repository_URL": "",
                "Quality_Band": "",
            }

        repos = pd.DataFrame(
            [repo(f"py{i}", "Python", i) for i in range(1, 10)]
            + [repo(f"go{i}", "Go", 10 + i) for i in range(1, 5)]
            + [repo(f"js{i}", "JavaScript", 14 + i) for i in range(1, 4)]
            + [repo(f"ts{i}", "TypeScript", 17 + i) for i in range(1, 3)]
            + [repo("rs1", "Rust", 20)]
            + [repo(f"misc{i}", None, 21 + i) for i in range(1, 21)]
        )
        row = {
            "GitHub_Username": "u",
            "Student_ID": "1",
            "Student Name": "U",
            "Division": "1",
            "Batch": "1",
            "Semester": "Semester 1",
        }
        top = students_payload_profile(row, repos)["top_languages"]
        # Unknown (20 repos) is excluded entirely; every known language shows
        # (no cap), widths ceiled to multiples of 5.
        assert top == [
            {"language": "Python", "count": 9, "pct": 100},
            {"language": "Go", "count": 4, "pct": 45},
            {"language": "JavaScript", "count": 3, "pct": 35},
            {"language": "TypeScript", "count": 2, "pct": 25},
            {"language": "Rust", "count": 1, "pct": 15},
        ]
        assert all(item["pct"] % 5 == 0 for item in top)

    def test_profile_recent_activity_from_repo_timestamps(self):
        import pandas as pd

        from app.views import students_payload_profile

        now = pd.Timestamp.now(tz="UTC").normalize()

        def repo(name, days_ago):
            updated = (now - pd.Timedelta(days=days_ago)).isoformat()
            return {
                "Username": "u",
                "Repository": name,
                "Language": "Python",
                "Stars": 0,
                "Updated": updated,
                "Repository_URL": "",
                "Quality_Band": "",
            }

        repos = pd.DataFrame(
            [
                repo("today", 0),
                repo("yesterday", 1),
                repo("day-before", 2),
                repo("old", 40),
                repo("older", 200),
            ]
        )
        row = {
            "GitHub_Username": "u",
            "Student_ID": "1",
            "Student Name": "U",
            "Division": "1",
            "Batch": "1",
            "Semester": "Semester 1",
        }
        profile = students_payload_profile(row, repos)
        # today/yesterday/day-before are within 30 days; the old ones are not.
        assert profile["contributions_30d"] == 3
        # Three consecutive active days ending today.
        assert profile["activity_streak"] == 3

    def test_profile_streak_broken_without_recent_updates(self):
        import pandas as pd

        from app.views import students_payload_profile

        now = pd.Timestamp.now(tz="UTC").normalize()
        repos = pd.DataFrame(
            [
                {
                    "Username": "u",
                    "Repository": "old",
                    "Language": "Python",
                    "Stars": 0,
                    "Updated": (now - pd.Timedelta(days=40)).isoformat(),
                    "Repository_URL": "",
                    "Quality_Band": "",
                }
            ]
        )
        row = {
            "GitHub_Username": "u",
            "Student_ID": "1",
            "Student Name": "U",
            "Division": "1",
            "Batch": "1",
            "Semester": "Semester 1",
        }
        profile = students_payload_profile(row, repos)
        assert profile["contributions_30d"] == 0
        assert profile["activity_streak"] == 0

    def test_friendly_timestamp_renders_ist(self):
        from app.views import friendly_timestamp

        assert friendly_timestamp("2026-09-24T00:00:00Z") == "24 Sep 2026 at 05:30 AM"
        assert friendly_timestamp("2026-09-23T20:00:00Z") == "24 Sep 2026 at 01:30 AM"
        assert friendly_timestamp("2026-09-24 06:25:00") == "24 Sep 2026 at 11:55 AM"

    def test_students_divisions_sorted_numerically(self, tmp_path):
        from app.views import dist_options

        assert dist_options(["10", "2", "14", "3"]) == ["All", "2", "3", "10", "14"]
        assert dist_options(["B", "A"]) == ["All", "A", "B"]

    def test_friendly_timestamp_renders_ist(self):
        from app.views import friendly_timestamp

        assert friendly_timestamp("2026-09-24T00:00:00Z") == "24 Sep 2026 at 05:30 AM"
        assert friendly_timestamp("2026-09-24T00:00:00+00:00") == "24 Sep 2026 at 05:30 AM"
        assert friendly_timestamp("2026-09-23T20:00:00Z") == "24 Sep 2026 at 01:30 AM"
        assert friendly_timestamp("Never") == "No completed analysis yet"

    def test_students_infinite_scroll_markup(self, tmp_path):
        roster_id = self._setup(tmp_path)
        body = self.client.get(f"/students?roster={roster_id}").text
        assert 'id="student-tbody"' in body
        assert 'id="shown-count"' in body
        # Only 2 students: everything visible, no sentinel row needed.
        assert 'id="scroll-sentinel"' not in body

    def test_linkedin_display_name_strips_id_suffix(self):
        from app.views import linkedin_display_name

        assert linkedin_display_name("anshuman-kulkarni-b27b0142a") == "anshuman-kulkarni"
        assert linkedin_display_name("aaryan-suktekar-114789242") == "aaryan-suktekar"
        assert linkedin_display_name("lisha-patil") == "lisha-patil"
        assert linkedin_display_name("purushottam-jadhav-0b910842a") == "purushottam-jadhav"
        assert linkedin_display_name("single") == "single"
        assert linkedin_display_name("") == ""
        assert linkedin_display_name(None) == ""

    def test_students_payload_shortens_linkedin_display(self):
        import pandas as pd

        from app import views

        students = pd.DataFrame(
            [
                {
                    "Student_ID": "1",
                    "Student Name": "Anshuman Amit Kulkarni",
                    "Division": "9",
                    "Batch": "3",
                    "Academic_Year": "2026-27",
                    "Semester": "Semester 1",
                    "GitHub_Username": "Anshuman-Kulkarni",
                    "Submitted_GitHub_Username": "Anshuman-Kulkarni",
                    "Avatar_URL": "",
                    "Profile_URL": "https://github.com/Anshuman-Kulkarni",
                    "LinkedIn_Username": "anshuman-kulkarni-b27b0142a",
                    "LinkedIn_URL": "https://www.linkedin.com/in/anshuman-kulkarni-b27b0142a/",
                    "HackerRank_Username": "1272261064_a",
                    "HackerRank_URL": "https://www.hackerrank.com/profile/1272261064_a",
                }
            ]
        )
        view = {
            "roster_id": "test",
            "records": [],
            "state": {},
            "students": students,
            "repos": pd.DataFrame(
                columns=["Username", "Repository", "Language", "Stars", "Updated", "Repository_URL", "Quality_Band"]
            ),
            "issues": pd.DataFrame(),
        }
        payload = views.students_payload(view)
        row = payload["display"].iloc[0]
        # Table text drops the ID suffix; href/tooltip/export keep the full slug.
        assert row["LinkedIn_Display"] == "anshuman-kulkarni"
        assert row["LinkedIn_Username"] == "anshuman-kulkarni-b27b0142a"
        assert row["LinkedIn_URL"] == "https://www.linkedin.com/in/anshuman-kulkarni-b27b0142a/"
        assert payload["profile"] is None
        selected = views.students_payload(view, selected_id="1")["profile"]
        assert selected["linkedin_display"] == "anshuman-kulkarni"
        assert selected["linkedin_username"] == "anshuman-kulkarni-b27b0142a"

    def test_students_payload_batches_thirty_at_a_time(self):
        import pandas as pd

        from app import views

        students = pd.DataFrame(
            [
                {
                    "Student_ID": str(i),
                    "Student Name": f"Student {i}",
                    "Division": str(i % 30),
                    "Batch": "1",
                    "Academic_Year": "2026-27",
                    "Semester": "Semester 1",
                    "GitHub_Username": f"user{i}",
                    "Submitted_GitHub_Username": f"user{i}",
                    "Avatar_URL": "",
                    "Profile_URL": f"https://github.com/user{i}",
                    "LinkedIn_Username": "",
                    "LinkedIn_URL": "",
                    "HackerRank_Username": "",
                    "HackerRank_URL": "",
                }
                for i in range(120)
            ]
        )
        view = {
            "roster_id": "test",
            "records": [],
            "state": {},
            "students": students,
            "repos": pd.DataFrame(),
            "issues": pd.DataFrame(),
        }
        payload = views.students_payload(view)
        assert payload["total"] == 120
        assert len(payload["display"]) == 120
        assert payload["showing"] == 30
        assert payload["initial_visible"] == 30
        assert payload["batch_size"] == 30
        # Explicit rows override grows the first paint (modal depth restore).
        payload = views.students_payload(view, rows=100)
        assert payload["showing"] == 100
        assert payload["initial_visible"] == 100

    def test_students_filter_narrows_results(self, tmp_path):
        roster_id = self._setup(tmp_path)
        body = self.client.get(f"/students?roster={roster_id}&q=alice").text
        assert "Alice Example" in body
        assert "Bob Example" not in body

    def test_repositories_page(self, tmp_path):
        roster_id = self._setup(tmp_path)
        body = self.client.get(f"/repositories?roster={roster_id}").text
        assert "repo-card-v2" in body  # 003d082 revamp renamed the Repository Cards section
        assert "py1" in body
        assert "shown" in body  # revamp replaced the "All Repositories" filter label with a count

    def test_leaderboards_page(self, tmp_path):
        roster_id = self._setup(tmp_path)
        body = self.client.get(f"/leaderboards?roster={roster_id}").text
        assert "Leaderboards" in body
        assert "students ranked" in body
        assert 'name="division"' in body
        assert 'name="batch"' in body
        assert 'name="semester"' in body
        # No year filter, no anonymize, no language board.
        assert 'name="year"' not in body
        assert "anonymize" not in body.lower()
        assert 'name="language"' not in body
        # 2x2 grid with the four boards.
        assert "Most Active Repositories" in body
        assert "Most Commits (All Projects)" in body
        assert "Most Stars on Repositories" in body
        assert "Top Starred Repositories" in body
        assert 'name="active_window"' in body
        assert 'name="commits_window"' in body
        # Windows default to last month; scores never say "repos".
        assert body.count('<option value="1m" selected>') == 2
        assert " repos</span>" not in body
        # Fixture: Alice owns py1 + js1 (2 repos, 2 stars), Bob owns go1.
        # Default 1m: active board empty (repos updated 2026-07-01), Alice
        # shows her 2 last-month commits, stars/top-repos unaffected.
        assert "No active repositories in this period yet." in body
        assert "Alice Example" in body
        assert "2 commits" in body
        assert "py1" in body and "go1" in body
        assert "re-run analysis" not in body
        # No profile popup without ?select=.
        assert 'id="student-modal-backdrop"' not in body

    def test_leaderboards_window_selection(self, tmp_path):
        roster_id = self._setup(tmp_path)
        # All-time active: Alice 2 repositories, Bob 1 repository (singular).
        body = self.client.get(f"/leaderboards?roster={roster_id}&active_window=all").text
        assert '<option value="all" selected>' in body
        assert "2 repositories</span>" in body
        assert "1 repository</span>" in body
        assert " repos</span>" not in body
        # All-time commits: Alice 5 + 1 = 6, Bob 2.
        body = self.client.get(f"/leaderboards?roster={roster_id}&commits_window=all").text
        assert "6 commits" in body
        assert "2 commits" in body
        # Alice has 3 commits in the last 3 months; Bob has none.
        body = self.client.get(f"/leaderboards?roster={roster_id}&commits_window=3m").text
        assert "3 commits" in body
        # Invalid windows fall back to last month.
        body = self.client.get(f"/leaderboards?roster={roster_id}&commits_window=bogus").text
        assert '<option value="1m" selected>' in body

    def test_leaderboards_blacklist_flow(self, tmp_path):
        roster_id = self._setup(tmp_path)
        # Admin sees the blacklist button + popup in the profile.
        popup = self.client.get(f"/leaderboards?roster={roster_id}&select=101").text
        assert 'class="blacklist-dropdown"' in popup
        assert 'class="blacklist-menu"' in popup
        assert 'data-board="commits"' in popup
        assert "is-blacklisted" not in popup
        # Blacklist Alice (101) from the commits board.
        res = self.client.post(
            f"/leaderboards/blacklist?roster={roster_id}",
            json={"student_id": "101", "board": "commits", "action": "blacklist"},
        )
        assert res.status_code == 200
        assert res.json() == {"status": "ok", "blacklisted": ["commits"]}
        # Alice leaves the commits board; Bob stays. Stars untouched.
        body = self.client.get(f"/leaderboards?roster={roster_id}&commits_window=all").text
        assert "6 commits" not in body
        assert "2 commits" in body
        assert "2 stars" in body
        # Popup now highlights the option in red with a whitelist button.
        popup = self.client.get(f"/leaderboards?roster={roster_id}&select=101").text
        assert "is-blacklisted" in popup
        assert 'data-board="commits" data-action="whitelist"' in popup
        # Blacklisting from the repos board hides her repositories too.
        res = self.client.post(
            f"/leaderboards/blacklist?roster={roster_id}",
            json={"student_id": "101", "board": "repos", "action": "blacklist"},
        )
        assert res.json() == {"status": "ok", "blacklisted": ["commits", "repos"]}
        body = self.client.get(f"/leaderboards?roster={roster_id}").text
        assert "py1" not in body and "js1" not in body
        assert "go1" in body
        # Whitelisting resumes consideration.
        res = self.client.post(
            f"/leaderboards/blacklist?roster={roster_id}",
            json={"student_id": "101", "board": "commits", "action": "whitelist"},
        )
        assert res.json() == {"status": "ok", "blacklisted": ["repos"]}
        body = self.client.get(f"/leaderboards?roster={roster_id}&commits_window=all").text
        assert "6 commits" in body

    def test_leaderboards_hide_repo_flow(self, tmp_path):
        roster_id = self._setup(tmp_path)
        py1 = "https://github.com/alice-dev/py1"
        # Admin sees a hide button on every repo row.
        popup = self.client.get(f"/leaderboards?roster={roster_id}&select=101").text
        assert "repo-hide-btn" in popup
        assert f'data-repo="{py1}"' in popup
        assert "is-hidden" not in popup
        # Hide py1: Alice's commits drop 6 -> 1, stars 2 -> 1, and py1 leaves
        # the top-repos board while js1/go1 stay.
        res = self.client.post(
            f"/leaderboards/hidden-repos?roster={roster_id}",
            json={"student_id": "101", "repo": py1, "action": "hide"},
        )
        assert res.status_code == 200
        assert res.json() == {"status": "ok", "hidden": [py1]}
        body = self.client.get(f"/leaderboards?roster={roster_id}&commits_window=all").text
        assert "6 commits" not in body
        assert "1 commit<" in body
        assert "2 stars" not in body
        assert "py1" not in body
        assert "js1" in body and "go1" in body
        # The row turns red with the hidden note; button toggles to unhide.
        popup = self.client.get(f"/leaderboards?roster={roster_id}&select=101").text
        assert "profile-repo is-hidden" in popup
        assert "Repository hidden from leaderboard" in popup
        assert 'data-hidden="1"' in popup
        # Unhiding restores everything.
        res = self.client.post(
            f"/leaderboards/hidden-repos?roster={roster_id}",
            json={"student_id": "101", "repo": py1, "action": "unhide"},
        )
        assert res.json() == {"status": "ok", "hidden": []}
        body = self.client.get(f"/leaderboards?roster={roster_id}&commits_window=all").text
        assert "6 commits" in body
        assert "py1" in body

    def test_leaderboards_hide_repo_validation_and_roles(self, tmp_path):
        roster_id = self._setup(tmp_path)
        url = f"/leaderboards/hidden-repos?roster={roster_id}"
        assert self.client.post(url, json={"student_id": "101", "repo": "x/y", "action": "hide"}).status_code == 200
        assert self.client.post(url, json={"student_id": "", "repo": "x/y", "action": "hide"}).status_code == 400
        assert self.client.post(url, json={"student_id": "101", "repo": "", "action": "hide"}).status_code == 400
        assert self.client.post(url, json={"student_id": "101", "repo": "x/y", "action": "ban"}).status_code == 400
        assert self.client.post("/leaderboards/hidden-repos", json={"student_id": "101", "repo": "x/y", "action": "hide"}).status_code == 400
        student_client = TestClient(app)
        make_user(student_client, "student")
        assert student_client.post(url, json={"student_id": "101", "repo": "x/y", "action": "hide"}).status_code == 403
        student_popup = student_client.get(f"/leaderboards?roster={roster_id}&select=101").text
        assert "repo-hide-btn" not in student_popup

    def test_leaderboards_blacklist_validation_and_roles(self, tmp_path):
        roster_id = self._setup(tmp_path)
        url = f"/leaderboards/blacklist?roster={roster_id}"
        assert self.client.post(url, json={"student_id": "101", "board": "nope", "action": "blacklist"}).status_code == 400
        assert self.client.post(url, json={"student_id": "", "board": "commits", "action": "blacklist"}).status_code == 400
        assert self.client.post(url, json={"student_id": "101", "board": "commits", "action": "ban"}).status_code == 400
        assert self.client.post("/leaderboards/blacklist", json={"student_id": "101", "board": "commits", "action": "blacklist"}).status_code == 400
        # Non-admins are refused, and see no button.
        student_client = TestClient(app)
        make_user(student_client, "student")
        assert student_client.post(url, json={"student_id": "101", "board": "commits", "action": "blacklist"}).status_code == 403
        faculty_client = TestClient(app)
        make_user(faculty_client, "faculty")
        assert faculty_client.post(url, json={"student_id": "101", "board": "commits", "action": "blacklist"}).status_code == 403
        student_popup = student_client.get(f"/leaderboards?roster={roster_id}&select=101").text
        assert 'class="blacklist-dropdown"' not in student_popup

    def test_leaderboards_name_opens_profile_popup(self, tmp_path):
        roster_id = self._setup(tmp_path)
        body = self.client.get(f"/leaderboards?roster={roster_id}").text
        # Leaderboard names link to the same popup profile as Students.
        assert 'class="leader-link"' in body
        assert "&select=101" in body
        popup = self.client.get(f"/leaderboards?roster={roster_id}&select=101").text
        assert 'id="student-modal-backdrop"' in popup
        assert "Student Profile" in popup
        assert "Alice Example" in popup
        assert "Repositories (" in popup
        # Closing returns to this leaderboard view (filters preserved).
        assert '/leaderboards?roster=' in popup
        assert 'class="student-modal-close"' in popup
        # Unknown ids render no popup.
        assert 'id="student-modal-backdrop"' not in self.client.get(
            f"/leaderboards?roster={roster_id}&select=999"
        ).text

    def test_verification_routes_removed(self, tmp_path):
        roster_id = self._setup(tmp_path)
        assert self.client.get(f"/verification?roster={roster_id}").status_code == 404
        assert self.client.get(f"/verification/export?roster={roster_id}").status_code == 404

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
        self.monkeypatch.setattr(auth, "USERS_DB", tmp_path / "users.db")
        self.client = TestClient(app)
        make_user(self.client, "admin")
        res = self.client.get("/overview/partial")
        assert res.status_code == 404

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
        self.monkeypatch.setattr(auth, "USERS_DB", tmp_path / "users.db")
        self.client = TestClient(app)
        make_user(self.client, "admin")
        data = upload_roster(self.client)
        roster_id = data["roster_id"]
        for path in ("students", "repositories", "leaderboards", "issues"):
            body = self.client.get(f"/{path}?roster={roster_id}").text
            assert "populates after you upload a roster" in body
        body = self.client.get("/students").text
        assert "populates after you upload a roster" in body

    def test_export_without_analysis_404(self, tmp_path):
        self.monkeypatch.setattr(storage, "DB_PATH", tmp_path / "analytics_history.db")
        self.monkeypatch.setattr(auth, "USERS_DB", tmp_path / "users.db")
        self.client = TestClient(app)
        make_user(self.client, "admin")
        data = upload_roster(self.client)
        response = self.client.get(f"/students/export?roster={data['roster_id']}")
        assert response.status_code == 404


class TestVercelEntrypoint:
    """api/index.py wraps the FastAPI app with a Vercel-prefix stripper so the
    rewrite  /(.*) -> /api/index  still routes to '/', '/students', etc."""

    def _client(self, tmp_path, monkeypatch):
        from api.index import wrapped

        monkeypatch.setattr(storage, "DB_PATH", tmp_path / "vercel.db")
        monkeypatch.setattr(auth, "USERS_DB", tmp_path / "users.db")
        client = TestClient(wrapped, raise_server_exceptions=False)
        make_user(client, "admin")
        return client

    def test_prefix_stripped_for_root(self, tmp_path, monkeypatch):
        from api.index import Mangum, wrapped

        client = self._client(tmp_path, monkeypatch)
        assert client.get("/api/index").status_code == 200
        assert "Student Analytics" in client.get("/api/index").text or "student" in client.get("/api/index").text.lower()

    def test_prefix_stripped_for_pages(self, tmp_path, monkeypatch):
        from api.index import wrapped

        client = self._client(tmp_path, monkeypatch)
        assert client.get("/api/index/history").status_code == 200
        assert client.get("/api/index/students").status_code == 200

    def test_real_paths_unaffected(self, tmp_path, monkeypatch):
        from api.index import wrapped

        client = self._client(tmp_path, monkeypatch)
        assert client.get("/history").status_code == 200

    def test_no_prefix_stripped_from_deep_static(self, tmp_path, monkeypatch):
        from api.index import wrapped

        client = self._client(tmp_path, monkeypatch)
        css = client.get("/api/index/static/style.css")
        assert css.status_code in (200, 404)

    def test_py_function_path_forms(self, tmp_path, monkeypatch):
        from api.index import wrapped

        client = self._client(tmp_path, monkeypatch)
        assert client.get("/api/index.py").status_code == 200
        assert client.get("/api/index.py/history").status_code == 200
        assert client.get("/api/history").status_code == 200


class TestSettingsPage:
    """BUG-085 — Settings was dropped in the new stack; the gear link was a dead
    #settings fragment with no selector/route/JS. /settings now renders a storage
    health card + theme toggle, and the gear targets the real route."""

    def _client(self, tmp_path, monkeypatch):
        monkeypatch.setattr(storage, "DB_PATH", tmp_path / "settings.db")
        monkeypatch.setattr(auth, "USERS_DB", tmp_path / "users.db")
        client = TestClient(app, raise_server_exceptions=True)
        make_user(client, "admin")
        return client

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
        monkeypatch.setattr(auth, "USERS_DB", tmp_path / "users.db")
        client = TestClient(app)
        make_user(client, "faculty")
        settings = client.get("/settings").text
        assert "Test User" in settings               # signed-in account shown
        assert "Faculty" in settings                 # role status from context, not hardcoded brand
        assert "Open Access" not in settings         # anonymous footer replaced
        assert "Sign out" in settings                # auth footer offers logout

    def test_sidebar_admin_label_has_no_dot(self, tmp_path, monkeypatch):
        monkeypatch.setattr(storage, "DB_PATH", tmp_path / "ident.db")
        monkeypatch.setattr(auth, "USERS_DB", tmp_path / "users.db")
        client = TestClient(app)
        make_user(client, "admin")
        body = client.get("/settings").text
        assert '<div class="user-role-line">Admin</div>' in body

    def test_base_template_uses_context_variables(self):
        base = Path(__file__).resolve().parent.parent / "app" / "templates" / "base.html"
        source = base.read_text(encoding="utf-8")
        assert "{{ auth_status }}" in source
        assert "{{ auth_logout }}" in source
        assert "{{ profile_href }}" in source
        assert "{{ avatar_initial }}" in source
        assert "{{ sidebar_avatar_url }}" in source
        assert "{{ sidebar_handle }}" in source
        assert "{{ auth_user }}" not in source     # sidebar card shows avatar + role only, no username
        for hardcoded in (
            "Faculty Workspace",
            "GitHub Platform",
            "brand-edition",
            "sidebar-foot",
            "brand-switcher",
            "sidebar-user-footer",
        ):
            assert hardcoded not in source

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

    def test_nested_typo_route_returns_friendly_404_html(self, tmp_path, monkeypatch):
        monkeypatch.setattr(storage, "DB_PATH", tmp_path / "nested404.db")
        monkeypatch.setattr(auth, "USERS_DB", tmp_path / "users.db")
        client = TestClient(app)
        make_user(client, "admin")
        res = client.get("/students/typo/unknown")
        assert res.status_code == 404
        assert "text/html" in res.headers.get("content-type", "")
        assert "Page Not Found" in res.text
        assert "Go to Overview" in res.text

    def test_overview_route_renders_overview_page(self, tmp_path, monkeypatch):
        monkeypatch.setattr(storage, "DB_PATH", tmp_path / "overview.db")
        monkeypatch.setattr(auth, "USERS_DB", tmp_path / "users.db")
        client = TestClient(app)
        make_user(client, "student")
        res = client.get("/overview")
        assert res.status_code == 200
        assert "Overview" in res.text
        assert "Student Analytics Workspace" in res.text
        assert "No student data loaded yet" in res.text