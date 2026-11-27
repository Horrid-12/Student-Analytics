"""Overview: a student is locked to their own class.

The fleet view holds every approved account, but the student Overview must
scope cards, graphs, radar and profile lookups to ONE (Division, Batch) class
and expose no cohort filter (including the whole-college "All students" scope).
Faculty/admin keep the full fleet and all filters.
"""

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import auth
import app.main as main
from app.main import app


def _fleet() -> dict:
    rows = [
        {
            "Student_ID": "1111111111", "Student Name": "Alice Example",
            "Email address": "alice@college.edu", "GitHub_Username": "alice-dev",
            "Division": "1", "Batch": "1", "Semester": "Semester 3",
            "Followers": 12, "Repository_Count": 2, "Combined_Repos": 3,
            "Contributed_Repos_Count": 1, "Pull_Requests": 3, "Issues_Opened": 1,
            "Owned_Commits": 40, "Team_Commits": 5, "HackerRank_Username": "",
        },
        {
            "Student_ID": "2222222222", "Student Name": "Bob Other",
            "Email address": "bob@college.edu", "GitHub_Username": "bob-dev",
            "Division": "2", "Batch": "2", "Semester": "Semester 3",
            "Followers": 30, "Repository_Count": 5, "Combined_Repos": 5,
            "Pull_Requests": 2, "Issues_Opened": 1,
            "Owned_Commits": 90, "Team_Commits": 0, "HackerRank_Username": "bob-hr",
        },
    ]
    students = pd.DataFrame(rows)
    repos = pd.DataFrame([
        {
            "Username": "alice-dev", "Repository": "alice-dev/notes-app", "Language": "Python",
            "Stars": 8, "Forks": 1, "Updated": "2026-11-01", "Created": "2026-09-01",
            "Commits": 24, "Pull_Requests": 2, "Has_README": 0,
            "Repository_Quality_Score": 62, "Quality_Band": "developing",
            "Maintenance_Status": "Active", "Description": "A notes app",
        },
        {
            "Username": "alice-dev", "Repository": "alice-dev/cli-tool", "Language": "Python",
            "Stars": 3, "Forks": 0, "Updated": "2026-08-04", "Created": "2026-07-01",
            "Commits": 16, "Pull_Requests": 1, "Has_README": 1,
            "Repository_Quality_Score": 74, "Quality_Band": "strong signals",
            "Maintenance_Status": "Aging", "Description": "A small CLI",
        },
        {
            "Username": "bob-dev", "Repository": "bob-dev/api", "Language": "JavaScript",
            "Stars": 20, "Forks": 4, "Updated": "2026-10-15", "Created": "2026-06-01",
            "Commits": 90, "Pull_Requests": 2, "Has_README": 1,
            "Repository_Quality_Score": 88, "Quality_Band": "exceptional / open source ready",
            "Maintenance_Status": "Active", "Description": "An API",
        },
    ])
    team_repos = pd.DataFrame([
        {
            "Username": "alice-dev", "Team_Repo": "bob-dev/api", "Last_Active_At": "2026-10-15",
            "Commits": 5, "PR_Events": 1, "Language": "JavaScript", "Stars": 20, "Forks": 4,
        }
    ])
    return {
        "roster_id": "fleet",
        "students": students,
        "repos": repos,
        "team_repos": team_repos,
        "records": rows,
        "state": {"status": "complete", "valid": 2},
    }


@pytest.fixture
def fleet(monkeypatch):
    monkeypatch.setattr(main, "_fleet_view", lambda request, roster="": _fleet())


def _login(tmp_path, monkeypatch, email: str, role: str) -> TestClient:
    monkeypatch.setattr(auth, "USERS_DB", str(tmp_path / "users.db"))
    auth.create_user(email, "secret123", role, "Test User")
    client = TestClient(app)
    resp = client.post("/login", data={"email": email, "password": "secret123"})
    assert resp.status_code in (200, 302)
    return client


def _student_count(body: str) -> bool:
    compact = " ".join(body.split())
    return (
        '<div class="metric-value">1</div> '
        '<div class="metric-label"><span class="badge-blue metric-pill">Number of Students</span></div>'
    ) in compact


def test_student_overview_shows_only_own_class(fleet, tmp_path, monkeypatch):
    client = _login(tmp_path, monkeypatch, "alice@college.edu", "student")
    body = client.get("/overview").text

    assert "Showing your class:" in body
    assert "Division 1" in body and "Batch 1" in body
    assert "Alice Example" in body
    assert "Bob Other" not in body
    assert _student_count(body)
    # Every cohort control is gone, including the "All students" baseline.
    assert 'name="pair"' not in body
    assert 'name="semester"' not in body
    assert 'id="cf-btn"' not in body
    assert 'id="radar-compare-btn"' not in body
    assert 'data-compare-key="overall"' not in body


def test_student_query_params_cannot_change_class(fleet, tmp_path, monkeypatch):
    client = _login(tmp_path, monkeypatch, "alice@college.edu", "student")
    body = client.get(
        "/overview?division=2&pair=2%7C2&semester=Semester+5&q=bob"
    ).text

    assert _student_count(body)
    assert "Showing your class:" in body
    assert "Division 1" in body and "Batch 1" in body
    assert "Bob Other" not in body


def test_faculty_overview_keeps_full_fleet_and_filters(fleet, tmp_path, monkeypatch):
    client = _login(tmp_path, monkeypatch, "teacher@college.edu", "faculty")
    body = client.get("/overview").text

    assert "Showing your class:" not in body
    assert "Bob Other" in body
    assert 'name="pair"' in body
    assert 'name="semester"' in body
    assert 'id="radar-compare-btn"' in body
    # The six personal displays are student-only.
    assert 'id="student-activity"' not in body
    assert "Your activity over time" not in body


def test_student_overview_adds_all_six_personal_displays(fleet, tmp_path, monkeypatch):
    client = _login(tmp_path, monkeypatch, "alice@college.edu", "student")
    body = client.get("/overview").text

    for marker in (
        'id="student-activity"',
        'id="student-stand"',
        'id="student-language"',
        'id="student-contribution"',
        'id="student-distribution"',
        "Next best actions",
    ):
        assert marker in body
    assert "Your activity over time" in body
    assert "Where you stand" in body
    assert "Your language mix" in body
    assert "Contribution breakdown" in body
    assert "Class distribution + your marker" in body
    # Own-data markers: Alice's Python repos, her missing README and HR gap.
    assert '"name": "Python"' in body
    assert "Add a README" in body
    assert "Link HackerRank" in body
    assert "Bob Other" not in body
    assert "bob-dev/api" not in body


def test_student_insights_use_only_own_repositories(fleet):
    from app import views

    view = _fleet()
    own = views.find_own_student_row(view["students"], "alice@college.edu")
    insights = views.student_overview_insights(view, own)

    assert insights["languages"] == [{"name": "Python", "value": 2}]
    assert insights["contribution"][0] == {"name": "Repositories", "owned": 2, "team": 1}
    assert insights["distribution"]["own_bin"] == 3  # own mean quality 68
    assert insights["distribution"]["students"] == 2
    assert insights["stand"][0]["you"] == 3  # owned + contributed repos
    assert insights["stand"][1]["you"] == 68.0
    assert {action["title"] for action in insights["actions"]} >= {
        "Add a README", "Refresh stale repos", "Link HackerRank"
    }
    # No classmate names or handles leak into any student-only data.
    assert "Bob" not in str(insights)
    assert "bob-dev" not in str(insights)


def test_student_actions_carry_repo_drilldown(fleet):
    from app import views

    view = _fleet()
    own = views.find_own_student_row(view["students"], "alice@college.edu")
    insights = views.student_overview_insights(view, own)
    by_title = {action["title"]: action for action in insights["actions"]}
    assert len(by_title) == 4

    docs = by_title["Add a README"]
    assert [repo["name"] for repo in docs["repos"]] == ["alice-dev/notes-app"]
    assert docs["repos"][0]["readme"] is False
    assert docs["stats"] == {
        "count": 1, "stars": 8, "commits": 24, "prs": 2, "avg_score": 62.0,
    }

    maintenance = by_title["Refresh stale repos"]
    assert [repo["name"] for repo in maintenance["repos"]] == ["alice-dev/cli-tool"]
    assert maintenance["repos"][0]["status"] == "Aging"

    quality = by_title["Quality scores look healthy"]
    assert quality["repos"] == [] and quality["stats"]["count"] == 0
    assert quality["empty_message"]

    link = by_title["Link HackerRank"]
    assert [repo["prs"] for repo in link["repos"]] == [1, 2]
    # Contributed repo identities never enter this student-only payload.
    assert "bob-dev" not in str(insights)

    # With HackerRank linked and no PRs, the fourth card becomes Collaboration
    # and still carries the same repo drill-down plus a privacy note.
    linked = _fleet()
    own_mask = linked["students"]["GitHub_Username"] == "alice-dev"
    linked["students"].loc[own_mask, "HackerRank_Username"] = "alice-hr"
    linked["students"].loc[own_mask, "Pull_Requests"] = 0
    linked_own = views.find_own_student_row(linked["students"], "alice@college.edu")
    linked_actions = {a["title"]: a for a in views.student_overview_insights(linked, linked_own)["actions"]}
    collab = linked_actions["Open a pull request"]
    assert len(collab["repos"]) == 2
    assert "teammate" in collab["note"].lower()
    assert "bob-dev" not in str(collab)


def test_student_action_cards_expand_to_repo_tables(fleet, tmp_path, monkeypatch):
    client = _login(tmp_path, monkeypatch, "alice@college.edu", "student")
    body = client.get("/overview").text

    assert body.count("data-insight-action=") == 4
    assert body.count('id="insight-action-panel-') == 4
    assert 'aria-controls="insight-action-panel-1"' in body
    assert 'id="insight-action-panel-1"' in body
    assert "View 1 repo" in body
    assert "alice-dev/notes-app" in body
    assert "alice-dev/cli-tool" in body
    assert "README missing" in body
    assert "<strong>8</strong> stars" in body
    assert "Refresh stale repos" in body
    assert "have been quiet for over 180 days" in body
    assert "bob-dev/api" not in body
    assert "Bob Other" not in body
