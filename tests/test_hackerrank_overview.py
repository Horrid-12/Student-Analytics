"""Feature 87 — Overview/Students platform toggle tests.

The Overview metric panel and the Students roster gain a GitHub | HackerRank
switch: both summaries are rendered server-side and the client only flips
visibility (persisted in localStorage). HackerRank numbers come from the
progressive snapshot cache (row 73): tombstones count as not-synced and
contribute nothing, unsynced profiles sit in `linked` but not `synced`.
"""

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import accounts, auth, views
from app.main import app, roster_store

A_EMAIL = "hr-toggle-a@college.edu"
B_EMAIL = "hr-toggle-b@college.edu"
A_PRN = "4000000101"
B_PRN = "4000000102"

A_SNAPSHOT = {
    "Student_ID": A_PRN, "Student Name": "Hr A",
    "Division": "3", "Batch": "1", "Semester": "Semester 3",
    "GitHub_Username": "hra-gh", "Roster_Email": A_EMAIL,
    "Avatar_URL": "https://avatars.example/a.png",
    "Profile_URL": "https://github.com/hra-gh",
    "HackerRank_Username": "hra_hr",
    "HackerRank_URL": "https://www.hackerrank.com/profile/hra_hr",
}
B_SNAPSHOT = {
    "Student_ID": B_PRN, "Student Name": "Hr B",
    "Division": "4", "Batch": "1", "Semester": "Semester 3",
    "GitHub_Username": "hrb-gh", "Roster_Email": B_EMAIL,
    "Avatar_URL": "https://avatars.example/b.png",
    "Profile_URL": "https://github.com/hrb-gh",
    "HackerRank_Username": "hrb_hr",
    "HackerRank_URL": "https://www.hackerrank.com/profile/hrb_hr",
}

SNAPS = {
    "hra_hr": {"practice_score": 500, "total_solved": 50, "badges": 3},
    "hrb_hr": {"practice_score": 900, "total_solved": 30, "badges": 5},
}


@pytest.fixture
def fleet2():
    """Two approved + synced students, each with a distinct HR handle."""
    accounts.init_db()
    for email, name, prn, division, handle in (
        (A_EMAIL, "Hr A", A_PRN, "3", "hra_hr"),
        (B_EMAIL, "Hr B", B_PRN, "4", "hrb_hr"),
    ):
        if auth.get_account(email) is None:
            assert auth.create_user(email, "secret123", "student", name)
        ok, err = auth.submit_onboarding(
            email, prn, "Core", division,
            main_batch="Batch 2022", practical_batch="1", semester="Semester 3",
            hackerrank_username=handle,
        )
        assert ok, err
        ok, reason = auth.set_onboarding_status(email, "approved")
        assert ok, reason
        accounts.save_snapshot(
            email, username=handle.replace("_hr", "-gh"), status="ok",
            student=dict(A_SNAPSHOT if email == A_EMAIL else B_SNAPSHOT),
            repos=[], synced_at="2026-10-04 10:00:00 UTC",
        )
    for handle, snap in SNAPS.items():
        roster_store.put_hr_snapshot(handle, dict(snap))


def login(email=A_EMAIL, password="secret123"):
    client = TestClient(app)
    r = client.post("/login", data={"email": email, "password": password})
    assert r.status_code in (200, 302)
    return client


def _grid(html, platform):
    marker = f'data-platform-grid="{platform}"'
    start = html.find(marker)
    assert start != -1, f"{platform} grid missing"
    return html[start : start + 5000]


class TestHrSummary:
    def test_counts_linked_synced_and_totals(self):
        students = pd.DataFrame([
            {"HackerRank_Username": "alice_hr"},
            {"HackerRank_Username": "bob_hr"},
            {"HackerRank_Username": "https://www.hackerrank.com/profile/cara_hr"},
            {"HackerRank_Username": None},
            {"HackerRank_Username": ""},
        ])
        summary = views._hr_summary(students, {
            "alice_hr": {"practice_score": 500, "total_solved": 50, "badges": 3},
            # Tombstone: counts as linked but never as synced / totals.
            "bob_hr": {"practice_score": 900, "total_solved": 30, "badges": 5, "invalid": True},
            "cara_hr": {"practice_score": 12, "total_solved": 0, "badges": 1},
        })
        assert summary == {"linked": 3, "synced": 2, "solved": 50, "practice": 512, "badges": 4}

    def test_missing_handles_and_empty_frame(self):
        assert views._hr_summary(None, SNAPS)["linked"] == 0
        assert views._hr_summary(pd.DataFrame(), SNAPS)["linked"] == 0
        empty = pd.DataFrame([{"HackerRank_Username": None}])
        assert views._hr_summary(empty, SNAPS)["linked"] == 0

    def test_unknown_snapshot_counts_as_linked_only(self):
        students = pd.DataFrame([{"HackerRank_Username": "never-synced"}])
        assert views._hr_summary(students, SNAPS) == {
            "linked": 1, "synced": 0, "solved": 0, "practice": 0, "badges": 0,
        }


class TestOverviewToggle:
    def test_payload_carries_hr_summary(self, fleet2):
        payload = views.overview_payload(views.fleet_view(), hr_snapshots=SNAPS)
        assert payload["hackerrank"] == {
            "linked": 2, "synced": 2, "solved": 80, "practice": 1400, "badges": 8,
        }

    def test_payload_respects_cohort_filter(self, fleet2):
        payload = views.overview_payload(views.fleet_view(), division="3", hr_snapshots=SNAPS)
        assert payload["hackerrank"] == {
            "linked": 1, "synced": 1, "solved": 50, "practice": 500, "badges": 3,
        }

    def test_page_renders_switch_and_both_grids(self, fleet2):
        body = login().get("/overview", headers={"Accept": "text/html"}).text
        assert 'data-platform-switch' in body
        assert 'data-platform="github"' in body
        assert 'data-platform="hackerrank"' in body
        assert 'class="platform-switch-btn is-active" role="tab" aria-selected="true" data-platform="github"' in body
        grid = _grid(body, "hackerrank")
        assert "Linked Profiles" in grid
        assert "Practice Score" in grid
        assert "Problems Solved" in grid
        assert "Badges Earned" in grid
        assert ">1400<" in grid and ">80<" in grid and ">8<" in grid
        # Inactive by default; the client-side script reveals it on switch.
        assert 'data-platform-grid="hackerrank" hidden' in body


class TestStudentsStrip:
    def test_payload_summary(self, fleet2):
        payload = views.students_payload(views.fleet_view(), hr_snapshots=SNAPS)
        assert payload["summary"]["github"] == {
            "students": 2, "github_linked": 2, "repos": 0, "active": 0,
        }
        assert payload["summary"]["hackerrank"] == {
            "linked": 2, "synced": 2, "solved": 80, "practice": 1400, "badges": 8,
        }

    def test_payload_summary_respects_filters(self, fleet2):
        payload = views.students_payload(views.fleet_view(), division="3", hr_snapshots=SNAPS)
        assert payload["summary"]["github"]["students"] == 1
        assert payload["summary"]["hackerrank"]["linked"] == 1
        assert payload["summary"]["hackerrank"]["solved"] == 50

    def test_page_renders_strip_and_both_grids(self, fleet2):
        body = login().get("/students", headers={"Accept": "text/html"}).text
        assert "stat-strip" in body
        assert 'data-platform-switch' in body
        github = _grid(body, "github")
        assert "GitHub Linked" in github
        assert "Total Repositories" in github
        hr = _grid(body, "hackerrank")
        assert "HR Linked" in hr
        assert "Practice Score" in hr
        assert "Problems Solved" in hr
        assert ">1400<" in hr and ">80<" in hr and ">8<" in hr
        assert 'data-platform-grid="hackerrank" hidden' in body

    def test_handle_accent_classes(self, fleet2):
        body = login().get("/students", headers={"Accept": "text/html"}).text
        assert "handle-github" in body
        assert "handle-hackerrank" in body
