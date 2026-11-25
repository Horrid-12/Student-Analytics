"""Seed a local, fully synthetic demo fleet for poster screenshots.

No real student data: names, handles and repositories are invented. The
analytics shapes mirror app/sync.py's account snapshots so every fleet page
(Overview, Students, Repositories, Leaderboards, profile) renders with data.
"""

import os

# Keep the demo strictly on the local SQLite fallback: the workspace .env.local
# carries a real DATABASE_URL and must never receive these rows.
os.environ["DATABASE_URL"] = ""
os.environ["DATABASE_URL_UNPOOLED"] = ""
os.environ["UNPOOLED"] = ""
os.environ["POSTGRES_URL"] = ""
os.environ["TEST_DATABASE_URL"] = ""
os.environ["AUTH_SECRET"] = "poster-demo-local-secret"
os.environ["ALLOWED_OAUTH_DOMAINS"] = "mitwpu.edu.in"
os.environ["VERCEL"] = ""
os.environ["CRON_SECRET"] = ""

import random
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from app import accounts, auth, autocad  # noqa: E402

random.seed(2147)

PASSWORD = "PosterDemo!2026"
ADMIN_EMAIL = "admin@mitwpu.edu.in"

STUDENTS = [
    ("aarav.sharma@mitwpu.edu.in", "Aarav Sharma", "2024010101", "1", "A", "Semester 3", "aarav-builds", "aarav_sh"),
    ("isha.patil@mitwpu.edu.in", "Isha Patil", "2024010102", "1", "A", "Semester 3", "isha-codes", "isha_p"),
    ("rohan.mehta@mitwpu.edu.in", "Rohan Mehta", "2024010203", "1", "B", "Semester 3", "rohanm-dev", "rohan_m"),
    ("saanvi.iyer@mitwpu.edu.in", "Saanvi Iyer", "2024010204", "1", "B", "Semester 3", "saanvi-io", "saanvi_i"),
    ("kabir.nair@mitwpu.edu.in", "Kabir Nair", "2024010305", "1", "C", "Semester 3", "kabirn", "kabir_n"),
    ("meera.joshi@mitwpu.edu.in", "Meera Joshi", "2024010306", "1", "C", "Semester 3", "meera-j", "meera_j"),
    ("aditya.rao@mitwpu.edu.in", "Aditya Rao", "2024020107", "2", "A", "Semester 3", "adityarao", "aditya_r"),
    ("diya.kulkarni@mitwpu.edu.in", "Diya Kulkarni", "2024020108", "2", "A", "Semester 3", "diya-k", "diya_k"),
    ("vivaan.desai@mitwpu.edu.in", "Vivaan Desai", "2024020209", "2", "B", "Semester 3", "vivaan-dev", "vivaan_d"),
    ("ananya.gupta@mitwpu.edu.in", "Ananya Gupta", "2024020210", "2", "B", "Semester 3", "ananya-g", "ananya_g"),
    ("aryan.singh@mitwpu.edu.in", "Aryan Singh", "2024020311", "2", "C", "Semester 3", "aryan-s", "aryan_s"),
    ("riya.menon@mitwpu.edu.in", "Riya Menon", "2024020312", "2", "C", "Semester 3", "riya-menon", "riya_m"),
    ("neel.bhatt@mitwpu.edu.in", "Neel Bhatt", "2024030113", "3", "A", "Semester 5", "neelb", "neel_b"),
    ("tara.shetty@mitwpu.edu.in", "Tara Shetty", "2024030114", "3", "A", "Semester 5", "tara-s", "tara_s"),
    ("dev.malhotra@mitwpu.edu.in", "Dev Malhotra", "2024030215", "3", "B", "Semester 5", "devm", "dev_m"),
    ("kiara.pillai@mitwpu.edu.in", "Kiara Pillai", "2024030216", "3", "B", "Semester 5", "kiara-p", "kiara_p"),
    ("yash.thakkar@mitwpu.edu.in", "Yash Thakkar", "2024030317", "3", "C", "Semester 5", "yash-t", "yash_t"),
    ("zoya.khan@mitwpu.edu.in", "Zoya Khan", "2024030318", "3", "C", "Semester 5", "zoya-k", "zoya_k"),
    ("arnav.pandey@mitwpu.edu.in", "Arnav Pandey", "2024040119", "4", "A", "Semester 5", "arnav-p", "arnav_p"),
    ("naina.reddy@mitwpu.edu.in", "Naina Reddy", "2024040120", "4", "A", "Semester 5", "naina-r", "naina_r"),
    ("ishaan.verma@mitwpu.edu.in", "Ishaan Verma", "2024040221", "4", "B", "Semester 5", "ishaan-v", "ishaan_v"),
    ("aisha.shaikh@mitwpu.edu.in", "Aisha Shaikh", "2024040222", "4", "B", "Semester 5", "aisha-s", "aisha_s"),
    ("reyansh.jain@mitwpu.edu.in", "Reyansh Jain", "2024040323", "4", "C", "Semester 5", "reyansh-j", "reyansh_j"),
    ("myra.chavan@mitwpu.edu.in", "Myra Chavan", "2024040324", "4", "C", "Semester 5", "myra-c", "myra_c"),
]

AUTOCAD_FILES = [
    ("aarav.sharma@mitwpu.edu.in", "2024010101", "Aarav Sharma", "sharma-floor-plan-level-2.dwg", 1_182_464),
    ("isha.patil@mitwpu.edu.in", "2024010102", "Isha Patil", "patil-site-plan-annex-b.dwg", 684_032),
    ("rohan.mehta@mitwpu.edu.in", "2024010203", "Rohan Mehta", "mehta-column-layout.dwg", 421_888),
    ("saanvi.iyer@mitwpu.edu.in", "2024010204", "Saanvi Iyer", "iyer-staircase-section.dwg", 356_352),
    ("kabir.nair@mitwpu.edu.in", "2024010305", "Kabir Nair", "nair-electrical-schematic.dwg", 942_080),
    ("meera.joshi@mitwpu.edu.in", "2024010306", "Meera Joshi", "joshi-plumbing-layout.dwg", 512_000),
    ("aditya.rao@mitwpu.edu.in", "2024020107", "Aditya Rao", "rao-parking-plan.dwg", 774_144),
    ("diya.kulkarni@mitwpu.edu.in", "2024020108", "Diya Kulkarni", "kulkarni-elevation-front.dwg", 288_768),
]

REPO_POOL = [
    ("campus-connect", "TypeScript", "Campus event and club discovery app", "MIT", 46, 12),
    ("smart-attendance", "Python", "Face-recognition attendance workflow", "MIT", 38, 9),
    ("dsa-journal", "Java", "Daily data-structures practice log", "MIT", 22, 5),
    ("rail-mitra", "JavaScript", "IRCTC-style rail enquiry dashboard", "MIT", 18, 4),
    ("leaf-disease-detector", "Python", "CNN leaf-disease classifier for farmers", "Apache-2.0", 41, 11),
    ("portfolio-2026", "TypeScript", "Personal portfolio and case studies", "MIT", 9, 2),
    ("notes-hub", "JavaScript", "Semester notes with full-text search", "MIT", 14, 3),
    ("expense-flow", "Python", "Budget tracker with monthly insights", "MIT", 11, 2),
    ("library-portal", "PHP", "Library seat and book reservation portal", "GPL-3.0", 27, 6),
    ("ev-route-planner", "Python", "EV charging route optimiser", "MIT", 24, 5),
    ("quiz-forge", "TypeScript", "AI-assisted quiz builder for classrooms", "MIT", 16, 4),
    ("open-source-tracker", "Python", "Contribution streak dashboard", "MIT", 19, 4),
    ("canteen-queue", "JavaScript", "Live canteen queue and pre-order", "MIT", 8, 2),
    ("plant-parent", "Python", "IoT plant watering companion", "MIT", 6, 1),
    ("code-review-bot", "Python", "GitHub Action for review checklists", "Apache-2.0", 29, 7),
    ("city-air-map", "JavaScript", "Air-quality heatmap for Pune wards", "MIT", 21, 5),
    ("study-buddy", "TypeScript", "Peer study-group matcher", "MIT", 13, 3),
    ("sorting-visualiser", "C++", "Algorithm visualiser for first-year labs", "MIT", 12, 3),
    ("attendance-cli", "Go", "Fast CLI attendance exporter", "MIT", 7, 1),
    ("ml-notebooks", "Jupyter Notebook", "Machine-learning lab notebooks", "MIT", 10, 2),
]

TEAM_POOL = [
    ("campus-connect", "https://github.com/mitwpu-dev/campus-connect", "TypeScript", "Campus event and club discovery app", 46, 12),
    ("smart-attendance", "https://github.com/mitwpu-dev/smart-attendance", "Python", "Face-recognition attendance workflow", 38, 9),
    ("library-portal", "https://github.com/mitwpu-dev/library-portal", "PHP", "Library seat and book reservation portal", 27, 6),
    ("ev-route-planner", "https://github.com/mitwpu-dev/ev-route-planner", "Python", "EV charging route optimiser", 24, 5),
    ("code-review-bot", "https://github.com/mitwpu-dev/code-review-bot", "Python", "GitHub Action for review checklists", 29, 7),
]

DESCRIPTIONS = {name: (lang, desc, lic, stars, forks) for name, lang, desc, lic, stars, forks in REPO_POOL}
DESCRIPTIONS.update({name: (lang, desc, "", stars, forks) for name, _url, lang, desc, stars, forks in TEAM_POOL})


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def avatar_data_uri(initials, bg, fg="#EEE9DF"):
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
        f"<rect width='64' height='64' fill='{bg}'/>"
        f"<text x='32' y='43' font-family='Arial,Helvetica,sans-serif' font-size='26'"
        f" font-weight='700' fill='{fg}' text-anchor='middle'>{initials}</text></svg>"
    )
    from urllib.parse import quote

    return "data:image/svg+xml," + quote(svg)


AVATAR_COLORS = ["#BE342B", "#142235", "#60738A", "#21845D", "#A66B25", "#1B2633"]


def build_snapshots(now):
    made = []
    for idx, (email, name, prn, division, batch, semester, username, hr) in enumerate(STUDENTS):
        initials = "".join(part[0] for part in name.split()[:2]).upper()
        avatar = avatar_data_uri(initials, AVATAR_COLORS[idx % len(AVATAR_COLORS)])

        # Owned repositories: deterministic slice of the pool.
        count = 4 + (idx % 4)
        start = (idx * 3) % len(REPO_POOL)
        chosen = [REPO_POOL[(start + step) % len(REPO_POOL)] for step in range(count)]
        # Three activity profiles keep the Statistics donut and radar honest:
        # consistent contributors, bursty newcomers, and inactive accounts.
        stale = idx in {3, 9, 16, 21}
        bursty = idx in {5, 12, 18}
        repos = []
        for r_i, (repo_name, lang, desc, lic, stars, forks) in enumerate(chosen):
            star_jitter = max(0, stars + ((idx * 7 + r_i * 11) % 18) - 7)
            if stale:
                updated = now - timedelta(days=120 + (idx * 3 + r_i * 9) % 90)
            elif bursty:
                updated = now - timedelta(days=(idx * 2 + r_i * 5) % 26)
            else:
                updated = now - timedelta(days=(idx * 3 + r_i * 9) % 84)
            created = now - timedelta(days=420 - (idx * 5 + r_i * 13) % 300)
            if stale:
                score = 34 + ((idx * 7 + r_i * 5) % 18)
            elif bursty:
                score = 58 + ((idx * 5 + r_i * 7) % 14)
            else:
                score = 62 + ((idx * 13 + r_i * 17) % 36)
            band = "Excellent" if score >= 90 else "Strong" if score >= 70 else "Developing" if score >= 40 else "Needs work"
            if stale:
                commits_30 = 0
                commits_90 = 0
                commits_all = 8 + ((idx * 11 + r_i * 3) % 40)
            elif bursty:
                commits_30 = 6 + ((idx * 5 + r_i * 7) % 20)
                commits_90 = commits_30
                commits_all = commits_90 + 12 + ((idx * 3 + r_i * 5) % 30)
            else:
                commits_30 = 4 + ((idx * 5 + r_i * 7) % 26)
                commits_90 = commits_30 + 15 + ((idx * 3 + r_i * 5) % 55)
                commits_all = commits_90 + 30 + ((idx * 11 + r_i * 3) % 120)
            repos.append({
                "Username": username,
                "Repository": repo_name,
                "Language": lang,
                "Stars": star_jitter,
                "Forks": forks + ((idx + r_i) % 9),
                "Description": desc,
                "License": lic,
                "Created": iso(created),
                "Updated": iso(updated),
                "Repository_URL": f"https://github.com/{username}/{repo_name}",
                "Maintenance_Status": "Active" if (now - updated).days <= 45 else "Stale",
                "Repository_Quality_Score": float(score),
                "Quality_Band": band,
                "Commits": commits_all,
                "Commits_30d": commits_30,
                "Commits_90d": commits_90,
                "Pull_Requests": (idx + r_i) % 11,
                "Issues": (idx * 2 + r_i) % 8,
                "Contributors": 1 + (idx + r_i) % 5,
                "Has_README": True,
                "Topics_Count": 2 + (idx + r_i) % 6,
                "Total_Commits": commits_all,
                "Is_Fork": 0,
            })

        # Contributed repositories: two shared projects per student.
        team = []
        for t_i in range(2):
            team_name, url, lang, desc, stars, forks = TEAM_POOL[(idx + t_i * 2) % len(TEAM_POOL)]
            if stale:
                last = now - timedelta(days=130 + (idx * 2 + t_i * 6) % 60)
                team_commits = 0
                team_prs_row = 0
            elif bursty:
                last = now - timedelta(days=(idx + t_i * 4) % 22)
                team_commits = 5 + ((idx * 7 + t_i * 9) % 18)
                team_prs_row = 1 + (idx + t_i) % 3
            else:
                last = now - timedelta(days=(idx * 2 + t_i * 6) % 55)
                team_commits = 6 + ((idx * 7 + t_i * 9) % 34)
                team_prs_row = 1 + (idx + t_i) % 6
            team.append({
                "Username": username,
                "Team_Repo": team_name,
                "Team_Repo_URL": url,
                "Commits": team_commits,
                "Push_Events": team_commits + 4,
                "PR_Events": team_prs_row,
                "Total_Events": team_commits + 4 + team_prs_row,
                "Last_Active_At": iso(last),
                "Language": lang,
                "Stars": stars,
                "Forks": forks,
                "Description": desc,
            })

        owned_commits = sum(r["Commits"] for r in repos)
        owned_30 = sum(r["Commits_30d"] for r in repos)
        owned_90 = sum(r["Commits_90d"] for r in repos)
        team_commits = sum(t["Commits"] for t in team)
        team_prs = sum(t["PR_Events"] for t in team)
        team_30 = team_commits if bursty else team_commits // 2
        team_90 = team_commits
        if stale:
            active_dates = []
        elif bursty:
            active_dates = sorted({
                (now - timedelta(days=d)).strftime("%Y-%m-%d")
                for d in range(0, 30, 2 + (idx % 2))
            })
        else:
            active_dates = sorted({
                (now - timedelta(days=d)).strftime("%Y-%m-%d")
                for d in range(0, 110, 3 + (idx % 3))
            })
        age_years = round(2.4 + (idx % 9) * 0.35, 2)
        public_repos = len(repos) + 3
        followers = 7 + (idx * 13) % 120
        following = 5 + (idx * 7) % 60

        student = {
            "Student_ID": prn,
            "Student Name": name,
            "Division": division,
            "Batch": batch,
            "Academic_Year": "2026-27",
            "Semester": semester,
            "Roster_Email": email,
            "GitHub_Username": username,
            "Submitted_GitHub_Username": username,
            "Username_Changed": "No",
            "Public_Repos": public_repos,
            "Repository_Count": len(repos),
            "Active_Repositories": sum(1 for r in repos if (now - datetime.fromisoformat(r["Updated"].replace("Z", "+00:00"))).days <= 45),
            "Repo_Fetch_Status": "Loaded",
            "Pull_Requests": sum(r["Pull_Requests"] for r in repos) + team_prs,
            "Open_PRs": (idx % 4),
            "Closed_PRs": sum(r["Pull_Requests"] for r in repos) + team_prs - (idx % 4),
            "Issues_Opened": sum(r["Issues"] for r in repos),
            "Open_Issues": idx % 3,
            "External_PRs": (idx * 3) % 9,
            "Contrib_Fetch_Status": "Loaded",
            "Team_Commits": team_commits,
            "Team_Push_Events": team_commits + 8,
            "Team_PR_Events": team_prs,
            "Team_Total_Events": team_commits + team_prs + 8,
            "Team_Commits_30d": team_30,
            "Team_Commits_90d": team_90,
            "Team_Total_Events_30d": team_30 + 4,
            "Team_Active_Dates": ",".join(active_dates[-40:]),
            "Team_Active_Repos": 0 if stale else len(team),
            "Contributed_Repos_Count": len(team),
            "Contributed_Repos": ", ".join(t["Team_Repo"] for t in team),
            "Team_Last_Active_At": team[0]["Last_Active_At"],
            "Team_Activity_Fetch_Status": "Loaded",
            "Owned_Commits": owned_commits,
            "Owned_Commits_30d": owned_30,
            "Owned_Commits_90d": owned_90,
            "Owned_Active_Dates": ",".join(active_dates),
            "Commit_Fetch_Status": "Loaded",
            "Followers": followers,
            "Following": following,
            "Account_Created": iso(now - timedelta(days=int(age_years * 365.25))),
            "Account_Age_Years": age_years,
            "Repos_Per_Account_Year": round(len(repos) / age_years, 2),
            "Followers_Per_Account_Year": round(followers / age_years, 2),
            "Following_Per_Account_Year": round(following / age_years, 2),
            "Primary_Language": max({r["Language"] for r in repos}, key=lambda l: sum(1 for r in repos if r["Language"] == l)),
            "Avatar_URL": avatar,
            "Profile_URL": f"https://github.com/{username}",
            "LinkedIn_Username": "",
            "LinkedIn_URL": "",
            "HackerRank_Username": hr,
            "HackerRank_URL": f"https://www.hackerrank.com/profile/{hr}",
        }
        made.append((email, username, student, repos, team))
    return made


def upsert_student(email, name, prn, division, batch, semester, username, hr):
    user = auth.get_user(email)
    if user is None:
        user = auth.create_user(email, PASSWORD, "student", name)
    if user is None:
        raise SystemExit(f"create_user failed for {email}")
    with sqlite3.connect(ROOT / "users.db") as conn:
        conn.execute(
            "UPDATE users SET name=?, prn=?, degree_branch=?, division=?, main_batch=?, "
            "practical_batch=?, semester=?, hackerrank_username=?, github_username=?, "
            "onboarding_status='approved', onboarding_submitted_at=?, github_verified_at=?, "
            "role='student', auth_source='password' WHERE lower(email)=lower(?)",
            (
                name, prn, "B.Tech Computer Science", division, "Batch 2024",
                batch, semester, hr, username,
                "2026-08-12 09:00:00 UTC", "2026-08-12 09:02:00 UTC", email,
            ),
        )


def seed_autocad():
    """A handful of DWG rows so the staff inbox renders with real table content."""
    if autocad.list_submissions(limit=1):
        return 0
    for email, prn, name, filename, size in AUTOCAD_FILES:
        unit = b"AC1032" + b"poster-demo-dwg-" + filename.encode() + b"-payload-"
        payload = (unit * (size // len(unit) + 2))[:size]
        autocad.create_submission(email, prn, name, filename, payload)
    with sqlite3.connect(ROOT / "autocad.db") as conn:
        ids = [row[0] for row in conn.execute("SELECT id FROM autocad_submissions ORDER BY id")]
        for i, submission_id in enumerate(ids):
            stamp = (datetime.now(timezone.utc) - timedelta(hours=5 + i * 23)).strftime("%Y-%m-%dT%H:%M:%S")
            conn.execute("UPDATE autocad_submissions SET created_at=? WHERE id=?", (stamp, submission_id))
    return len(ids)


def main():
    now = datetime.now(timezone.utc)
    auth.init_db()

    admin = auth.get_user(ADMIN_EMAIL)
    if admin is None:
        admin = auth.create_user(ADMIN_EMAIL, PASSWORD, "admin", "Dr. Anjali Deshmukh")
    if admin is None:
        raise SystemExit("admin seed failed")
    with sqlite3.connect(ROOT / "users.db") as conn:
        conn.execute("UPDATE users SET role='admin', name='Dr. Anjali Deshmukh' WHERE lower(email)=lower(?)", (ADMIN_EMAIL,))

    faculty_email = "faculty@mitwpu.edu.in"
    if auth.get_faculty(faculty_email) is None:
        auth.create_faculty(faculty_email, PASSWORD, "Prof. Sameer Kulkarni")

    for row in STUDENTS:
        upsert_student(*row)

    accounts.init_db()
    for email, username, student, repos, team in build_snapshots(now):
        ok = accounts.save_snapshot(
            email, username=username, status="ok", student=student,
            repos=repos, team_repos=team, synced_at="2026-11-24 08:30:00 UTC",
        )
        if not ok:
            raise SystemExit(f"snapshot save failed for {email}")

    print(f"seeded {len(STUDENTS)} students + admin {ADMIN_EMAIL} | password {PASSWORD}")
    print("snapshots:", len(accounts.list_snapshots()))
    print("autocad submissions:", seed_autocad())


if __name__ == "__main__":
    main()
