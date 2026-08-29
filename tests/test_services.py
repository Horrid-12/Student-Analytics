import io
import os
import subprocess
import sys

import pandas as pd
import pytest

import services
from services import EXCEL_COLUMNS, GITHUB_API_BASE, GITHUB_COL, REQUIRED_EXCEL_COLUMNS, STUDENT_ID_COL


def make_roster_xlsx(rows: list[dict]) -> io.BytesIO:
    df = pd.DataFrame(rows, columns=EXCEL_COLUMNS)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False)
    buf.name = "roster.xlsx"
    buf.seek(0)
    return buf


def make_roster_csv(rows: list[dict]) -> io.BytesIO:
    df = pd.DataFrame(rows, columns=EXCEL_COLUMNS)
    buf = io.BytesIO(df.to_csv(index=False).encode("utf-8"))
    buf.name = "roster.csv"
    buf.seek(0)
    return buf


def roster_rows() -> list[dict]:
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
    def __init__(self, users, repos, contributions=None):
        self.users = users
        self.repos = repos
        self.contributions = contributions or {}
        self.calls = []

    def __call__(self, url, token, timeout=None):
        self.calls.append(url)
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


def prepared_student_df() -> pd.DataFrame:
    df = services.load_excel(make_roster_xlsx(roster_rows()))
    return services.prepare_students(df)[0]


class TestExtractUsername:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("https://github.com/octocat", "octocat"),
            ("http://github.com/octocat", "octocat"),
            ("https://www.github.com/octocat", "octocat"),
            ("github.com/octocat", "octocat"),
            ("https://GITHUB.COM/octocat", "octocat"),
            ("https://github.com/octocat?tab=repositories", "octocat"),
            ("https://github.com/octocat#frag", "octocat"),
            ("https://github.com/octocat/repo-name", "octocat"),
            ("https://github.com/OctoCat_1-x", "OctoCat_1-x"),
            ("octocat", "octocat"),
            ("octo_cat-123", "octo_cat-123"),
            ("octocat,", "octocat"),
            ("octocat;", "octocat"),
            ("  octocat  ", "octocat"),
        ],
    )
    def test_extracts(self, text, expected):
        assert services.extract_username(text) == expected

    @pytest.mark.parametrize(
        "text",
        [None, float("nan"), "", "   ", "https://example.com/octocat", "https://gitlab.com/octocat", "octocat.", "!!!", "not a url dot"],
    )
    def test_rejects(self, text):
        assert services.extract_username(text) is None


class TestNormalizeStudentId:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("123.0", "123"),
            (123.0, "123"),
            ("123", "123"),
            ("0012", "0012"),
            (" 42 ", "42"),
            (None, None),
            (float("nan"), None),
            ("", None),
        ],
    )
    def test_normalizes(self, value, expected):
        assert services.normalize_student_id(value) == expected


class TestAcademicPeriods:
    def test_periods(self):
        df = pd.DataFrame({"Timestamp": ["2025-08-01 10:00:00", "2026-02-01 10:00:00", None]})
        out = services.add_academic_periods(df)
        assert list(out["Academic_Year"]) == ["2025-26", "2025-26", "Unknown"]
        assert list(out["Semester"]) == ["Semester 1", "Semester 2", "Unknown"]


class TestSchema:
    def test_required_columns_ok(self):
        services.validate_excel_schema(pd.DataFrame(columns=REQUIRED_EXCEL_COLUMNS))

    def test_missing_required_column_raises(self):
        df = pd.DataFrame(columns=[c for c in REQUIRED_EXCEL_COLUMNS if c != "Batch"])
        with pytest.raises(ValueError, match="Batch"):
            services.validate_excel_schema(df)

    def test_normalize_excel_headers_canonical_untouched(self):
        df = pd.DataFrame(columns=["Actual GitHub Account Link:"])
        out = services.normalize_excel_headers(df)
        assert list(out.columns) == ["Actual GitHub Account Link:"]

    def test_normalize_excel_headers_renames_messy(self):
        df = pd.DataFrame(columns=["actual github account link "])
        out = services.normalize_excel_headers(df)
        assert list(out.columns) == ["Actual GitHub Account Link:"]

    def test_normalize_excel_headers_first_claim_wins(self):
        df = pd.DataFrame(columns=["GitHub : Repository 1 Link :", "GitHub: Repository 1 Link :"])
        out = services.normalize_excel_headers(df)
        assert list(out.columns) == ["GitHub : Repository 1 Link :", "GitHub: Repository 1 Link :"]

    def test_excel_columns_keep_trailing_space_header(self):
        assert EXCEL_COLUMNS[8] == "GitHub : Repository 3 Link : "


class TestLoadExcel:
    def test_xlsx(self):
        df = services.load_excel(make_roster_xlsx(roster_rows()))
        assert list(df.columns) == EXCEL_COLUMNS
        assert len(df) == 2

    def test_csv(self):
        df = services.load_excel(make_roster_csv(roster_rows()))
        assert list(df.columns) == EXCEL_COLUMNS
        assert len(df) == 2


class TestPrepareStudents:
    def test_extracts_ids_and_usernames(self):
        prepared = prepared_student_df()
        assert list(prepared[STUDENT_ID_COL]) == ["101", "202"]
        assert list(prepared["GitHub_Username"]) == ["alice-dev", "bob-cat"]

    def test_invalid_format_marked(self):
        rows = [
            {
                "Timestamp": "2025-08-01 10:00:00",
                "PRN No": "1.0",
                "Student Name": "A",
                "Division": "A",
                "Batch": "2026",
                "Actual GitHub Account Link:": "https://example.com/alice",
            }
        ]
        prepared, invalid = services.prepare_students(services.load_excel(make_roster_xlsx(rows)))
        assert len(invalid) == 1
        assert invalid.iloc[0]["Issue"] == "Invalid format"
        assert list(prepared["GitHub_Username"]) == [None]


class TestRateLimit:
    def test_primary_limit(self):
        with pytest.raises(services.RateLimitError) as excinfo:
            services.check_rate_limit_parts(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1700000000"})
        assert excinfo.value.reset_epoch == "1700000000"

    def test_secondary_limit(self):
        with pytest.raises(services.RateLimitError):
            services.check_rate_limit_parts(403, {"Retry-After": "60"})

    def test_no_limit_no_raise(self):
        services.check_rate_limit_parts(403, {})
        services.check_rate_limit_parts(200, {})

    def test_get_user_propagates_rate_limit(self, monkeypatch):
        monkeypatch.setattr(
            services,
            "_cached_get_json",
            lambda url, token, timeout=None: (403, {"X-RateLimit-Remaining": "0"}, None),
        )
        with pytest.raises(services.RateLimitError):
            services.get_user("alice", "t")

    def test_validate_users_propagates_rate_limit(self, monkeypatch):
        monkeypatch.setattr(
            services,
            "_cached_get_json",
            lambda url, token, timeout=None: (403, {"X-RateLimit-Remaining": "0"}, None),
        )
        with pytest.raises(services.RateLimitError):
            services.validate_users(["alice"], "t")


class TestErrorClassification:
    def test_kinds(self):
        import requests

        assert services.classify_api_error(exc=requests.Timeout()) == "timeout"
        assert services.classify_api_error(exc=requests.ConnectionError()) == "network"
        assert services.classify_api_error(status_code=401) == "auth"
        assert services.classify_api_error(status_code=503) == "server"
        assert services.classify_api_error(status_code=403) == "rate_limit"
        assert services.classify_api_error() == "unknown"


class TestResolveRole:
    def test_most_privileged_first(self):
        import hashlib

        digest = hashlib.sha256(b"pw").hexdigest()
        configured = {"ADMIN_PASSWORD_SHA256": digest, "FACULTY_PASSWORD_SHA256": digest}
        assert services.resolve_role(digest, configured) == "admin"

    def test_case_insensitive(self):
        assert services.resolve_role("ABC", {"FACULTY_PASSWORD_SHA256": "abc"}) == "faculty"

    def test_no_match(self):
        assert services.resolve_role("deadbeef", {"FACULTY_PASSWORD_SHA256": "abc"}) is None

    def test_empty(self):
        assert services.resolve_role("", {}) is None
        assert services.resolve_role(None, {}) is None


class TestReposPagination:
    def test_paginates_until_short_page(self, monkeypatch):
        def fake(url, token, timeout=None):
            page = int(url.split("&page=")[1])
            count = 100 if page < 3 else 10
            return 200, {}, [{"name": f"r{page}-{i}"} for i in range(count)]

        monkeypatch.setattr(services, "_cached_get_json", fake)
        repos, ok = services.get_repos("alice", "t")
        assert ok
        assert len(repos) == 210
        assert repos[0]["name"] == "r1-0"
        assert repos[-1]["name"] == "r3-9"

    def test_loop_guard_stops_at_page_20(self, monkeypatch):
        requested = []

        def fake(url, token, timeout=None):
            requested.append(int(url.split("&page=")[1]))
            return 200, {}, [{"name": "r"} for _ in range(100)]

        monkeypatch.setattr(services, "_cached_get_json", fake)
        repos, ok = services.get_repos("alice", "t")
        assert ok
        assert len(repos) == 2000
        assert requested[-1] == 20

    def test_non_200_returns_empty(self, monkeypatch):
        monkeypatch.setattr(services, "_cached_get_json", lambda url, token, timeout=None: (404, {}, None))
        repos, ok = services.get_repos("alice", "t")
        assert repos == []
        assert ok is False


class TestSearchContributionsPagination:
    def test_paginates_until_short_page(self, monkeypatch):
        def fake(url, token, timeout=None):
            page = int(url.split("&page=")[1])
            count = 100 if page < 2 else 3
            return 200, {}, {"items": [{"id": i} for i in range(count)]}

        monkeypatch.setattr(services, "_cached_get_json", fake)
        items, ok = services.get_search_contributions("alice", "pr", "t")
        assert ok
        assert len(items) == 103

    def test_loop_guard_stops_at_page_10(self, monkeypatch):
        requested = []

        def fake(url, token, timeout=None):
            requested.append(int(url.split("&page=")[1]))
            return 200, {}, {"items": [{"id": i} for i in range(100)]}

        monkeypatch.setattr(services, "_cached_get_json", fake)
        items, ok = services.get_search_contributions("alice", "issue", "t")
        assert ok
        assert len(items) == 1000
        assert requested[-1] == 10

    def test_non_200_returns_empty(self, monkeypatch):
        monkeypatch.setattr(services, "_cached_get_json", lambda url, token, timeout=None: (400, {}, None))
        items, ok = services.get_search_contributions("alice", "pr", "t")
        assert items == []
        assert ok is False


class TestSummarizeContributions:
    def test_external_prs_and_states(self):
        prs = [
            {"state": "open", "repository_url": "https://api.github.com/repos/other/repo"},
            {"state": "closed", "repository_url": "https://api.github.com/repos/alice-dev/own"},
            {"state": "open", "repository_url": ""},
        ]
        issues = [{"state": "open"}, {"state": "closed"}]
        summary = services.summarize_user_contributions("Alice-Dev", prs, issues)
        assert summary["Pull_Requests"] == 3
        assert summary["Open_PRs"] == 2
        assert summary["Closed_PRs"] == 1
        assert summary["Issues_Opened"] == 2
        assert summary["Open_Issues"] == 1
        assert summary["External_PRs"] == 1


class TestQualityMetrics:
    def test_bands(self):
        repo_df = pd.DataFrame(
            [
                {"Username": "u", "Updated": "2026-07-01T00:00:00Z", "Description": "x", "Language": "Py", "License": "MIT"},
                {"Username": "u", "Updated": "2025-01-01T00:00:00Z", "Description": None, "Language": None, "License": None},
            ]
        )
        out = services.add_repository_quality_metrics(repo_df)
        assert list(out["Maintenance_Status"]) == ["Active", "Stale"]
        assert out["Repository_Quality_Score"].tolist() == [100, 10]
        assert list(out["Quality_Band"]) == ["Strong signals", "Needs attention"]

    def test_empty_stays_empty(self):
        out = services.add_repository_quality_metrics(pd.DataFrame())
        assert out.empty


class TestDashboard:
    github_stats = pd.DataFrame(
        [
            {
                "Submitted_GitHub_Username": "alice-dev",
                "GitHub_Username": "alice-dev",
                "Public_Repos": 3,
                "Followers": 1,
                "Following": 0,
                "Account_Created": "2020-01-01T00:00:00Z",
                "Account_Age_Years": 5.0,
                "Followers_Per_Account_Year": 0.2,
                "Following_Per_Account_Year": 0.0,
                "Profile_URL": "",
                "Avatar_URL": "",
            }
        ]
    )

    def _df(self):
        return pd.DataFrame(
            [
                {
                    STUDENT_ID_COL: "1",
                    "Submitted_GitHub_Username": "alice-dev",
                    "Student Name": "A",
                    "Division": "D",
                    "Batch": "B",
                    "Academic_Year": "2025-26",
                    "Semester": "Semester 1",
                }
            ]
        )

    def test_counts_mode_and_status(self):
        repo_df = pd.DataFrame(
            [
                {"Username": "alice-dev", "Language": "Python", "Updated": "2026-07-01T00:00:00Z"},
                {"Username": "alice-dev", "Language": "Python", "Updated": "2026-07-01T00:00:00Z"},
                {"Username": "alice-dev", "Language": "Go", "Updated": "2026-07-01T00:00:00Z"},
            ]
        )
        dash = services.build_dashboard_df(self._df(), self.github_stats, repo_df)
        row = dash.iloc[0]
        assert row[STUDENT_ID_COL] == "1"
        assert row["Repository_Count"] == 3
        assert row["Primary_Language"] == "Python"
        assert row["Active_Repositories"] == 3
        assert int(row["Pull_Requests"]) == 0
        assert row["Repo_Fetch_Status"] == "Loaded"
        assert row["Contrib_Fetch_Status"] == "Loaded"
        assert row["Username_Changed"] == False

    def test_zero_repos_unknown_language(self):
        dash = services.build_dashboard_df(self._df(), self.github_stats, pd.DataFrame())
        row = dash.iloc[0]
        assert row["Repository_Count"] == 0
        assert row["Primary_Language"] == "Unknown"
        assert int(row["Active_Repositories"]) == 0

    def test_username_changed_flag(self):
        stats = self.github_stats.copy()
        stats["GitHub_Username"] = "alice-RENAMED"
        dash = services.build_dashboard_df(self._df(), stats, pd.DataFrame())
        assert dash.iloc[0]["Username_Changed"] == True

    def test_empty_contract_exact_columns(self):
        expected = [
            STUDENT_ID_COL,
            "Student Name",
            "Division",
            "Batch",
            "Academic_Year",
            "Semester",
            "GitHub_Username",
            "Submitted_GitHub_Username",
            "Public_Repos",
            "Repository_Count",
            "Active_Repositories",
            "Repo_Fetch_Status",
            "Pull_Requests",
            "Open_PRs",
            "Closed_PRs",
            "Issues_Opened",
            "Open_Issues",
            "External_PRs",
            "Contrib_Fetch_Status",
            "Followers",
            "Following",
            "Account_Age_Years",
            "Repos_Per_Account_Year",
            "Followers_Per_Account_Year",
            "Following_Per_Account_Year",
            "Username_Changed",
            "Primary_Language",
            "Avatar_URL",
            "Profile_URL",
        ]
        dash = services.build_dashboard_df(self._df(), pd.DataFrame(), pd.DataFrame())
        assert list(dash.columns) == expected
        assert dash.empty


class TestIssues:
    def test_invalid_issues_categorizes(self):
        prepared = pd.DataFrame(
            [
                {STUDENT_ID_COL: "1", "Student Name": "A", "Division": "A", "Batch": "2026", GITHUB_COL: "https://github.com/aaa", "GitHub_Username": "aaa"},
                {STUDENT_ID_COL: "2", "Student Name": "B", "Division": "A", "Batch": "2026", GITHUB_COL: "https://github.com/bbb", "GitHub_Username": "bbb"},
                {STUDENT_ID_COL: "3", "Student Name": "C", "Division": "A", "Batch": "2026", GITHUB_COL: "not a link", "GitHub_Username": None},
            ]
        )
        issues = services.build_invalid_issues(prepared, invalid_users=["aaa"])
        by_id = dict(zip(issues[STUDENT_ID_COL], issues["Issue"]))
        assert by_id["1"] == "Failed GitHub validation"
        assert by_id["3"] == "Missing username"
        assert "2" not in by_id

    def test_duplicate_username_issues(self):
        df = pd.DataFrame(
            [
                {STUDENT_ID_COL: "1", "Student Name": "A", "Division": "A", "Batch": "2026", GITHUB_COL: "https://github.com/dup", "GitHub_Username": "dup"},
                {STUDENT_ID_COL: "2", "Student Name": "B", "Division": "A", "Batch": "2026", GITHUB_COL: "https://github.com/dup", "GitHub_Username": "dup"},
            ]
        )
        issues = services.build_duplicate_issues(df)
        assert len(issues) == 2
        assert set(issues["Issue"]) == {"Duplicate username"}

    def test_duplicate_student_issues(self):
        df = pd.DataFrame(
            [
                {STUDENT_ID_COL: "7", "Student Name": "A", "Division": "A", "Batch": "2026", GITHUB_COL: "https://github.com/a", "GitHub_Username": "a"},
                {STUDENT_ID_COL: "7", "Student Name": "A", "Division": "A", "Batch": "2026", GITHUB_COL: "https://github.com/b", "GitHub_Username": "b"},
            ]
        )
        issues = services.build_duplicate_student_issues(df)
        assert len(issues) == 2
        assert set(issues["Issue"]) == {"Duplicate student"}


class TestAnalysisStatus:
    def test_statuses(self):
        assert services.determine_analysis_status(["a"], []) == "Complete"
        assert services.determine_analysis_status([], ["e"]) == "Failed"
        assert services.determine_analysis_status(["a"], ["e"]) == "Partial"


class TestRunAnalysis:
    def test_end_to_end_complete(self, monkeypatch):
        monkeypatch.setattr(services.time, "sleep", lambda _: None)
        fake = FakeGitHub(
            users={"alice-dev": user_payload("alice-dev", public_repos=3), "bob-cat": user_payload("bob-cat", public_repos=2, followers=4, following=1)},
            repos={
                "alice-dev": [
                    repo_item("py1", "Python"),
                    repo_item("py2", "Python"),
                    repo_item("js1", "JavaScript"),
                ],
                "bob-cat": [repo_item("go1", "Go"), repo_item("go2", "Go")],
            },
            contributions={
                "alice-dev": (
                    [{"state": "open", "repository_url": "https://api.github.com/repos/other-org/thing"}],
                    [{"state": "closed", "repository_url": "https://api.github.com/repos/other-org/thing"}],
                ),
                "bob-cat": ([], []),
            },
        )
        monkeypatch.setattr(services, "_cached_get_json", fake)

        result = services.run_analysis(make_roster_xlsx(roster_rows()), token="test-token")

        assert result.status == "Complete"
        assert result.valid_users == ["alice-dev", "bob-cat"]
        assert result.invalid_users == []
        assert result.error_users == []
        assert result.log[-1] == "Complete"

        dash = result.dashboard_df
        assert set(dash[STUDENT_ID_COL]) == {"101", "202"}

        alice = dash[dash["GitHub_Username"] == "alice-dev"].iloc[0]
        assert alice["Repository_Count"] == 3
        assert alice["Primary_Language"] == "Python"
        assert alice["Active_Repositories"] == 3
        assert alice["Pull_Requests"] == 1
        assert alice["External_PRs"] == 1
        assert alice["Issues_Opened"] == 1
        assert alice["Repo_Fetch_Status"] == "Loaded"
        assert alice["Contrib_Fetch_Status"] == "Loaded"
        assert alice["Account_Age_Years"] > 1

        bob = dash[dash["GitHub_Username"] == "bob-cat"].iloc[0]
        assert bob["Repository_Count"] == 2
        assert bob["Primary_Language"] == "Go"

    def test_validate_users_retries_transient_error(self, monkeypatch):
        monkeypatch.setattr(services.time, "sleep", lambda _: None)
        calls = []

        def flaky(url, token, timeout=None):
            calls.append(url)
            if len(calls) == 1:
                return 503, {}, None
            return 200, {}, {"login": "alice"}

        monkeypatch.setattr(services, "_cached_get_json", flaky)
        valid, invalid, errors, payloads = services.validate_users(["alice"], "t")
        assert valid == ["alice"]
        assert invalid == []
        assert errors == []
        assert len(calls) == 2

    def test_validate_users_permanent_error(self, monkeypatch):
        monkeypatch.setattr(services.time, "sleep", lambda _: None)
        monkeypatch.setattr(
            services,
            "_cached_get_json",
            lambda url, token, timeout=None: (500, {}, None),
        )
        valid, invalid, errors, payloads = services.validate_users(["alice"], "t")
        assert valid == []
        assert invalid == []
        assert errors == ["alice"]


class TestStreamlitFreeImport:
    def test_importable_without_streamlit(self):
        code = (
            "import sys\n"
            "import os\n"
            "sys.modules['streamlit'] = None\n"
            "module_name = os.environ.get('MODULE_UNDER_TEST', '')\n"
            "if module_name:\n"
            "    import importlib\n"
            "    sys.modules['services'] = importlib.import_module(module_name)\n"
            "import services\n"
            "assert services.st is None\n"
            "assert services.clear_api_cache() is None\n"
            "assert callable(services._cached_get_json)\n"
            "print('OK')\n"
        )
        repo_root = str(__import__("pathlib").Path(__file__).resolve().parent.parent)
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=60,
            env=os.environ,
        )
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout