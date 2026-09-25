-- Phase 4.9 Postgres schema — idempotent (CREATE TABLE IF NOT EXISTS).
-- Follows the legacy "no Alembic, no migrations" convention (AGENTS.md).

-- 1. User accounts (replaces SQLite users.db).
CREATE TABLE IF NOT EXISTS users (
    id              SERIAL PRIMARY KEY,
    email           TEXT NOT NULL UNIQUE,
    password_hash   TEXT,
    role            TEXT NOT NULL DEFAULT 'student',
    name            TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    auth_source     TEXT NOT NULL DEFAULT 'password',
    google_sub      TEXT NOT NULL DEFAULT '',
    github_username TEXT NOT NULL DEFAULT '',
    linkedin_sub    TEXT NOT NULL DEFAULT '',
    -- 4.11 (e): linked GitHub/LinkedIn identities + confirmed display source.
    linked_github_username TEXT NOT NULL DEFAULT '',
    linked_github_avatar   TEXT NOT NULL DEFAULT '',
    linked_linkedin_name   TEXT NOT NULL DEFAULT '',
    linked_linkedin_avatar TEXT NOT NULL DEFAULT '',
    profile_source         TEXT NOT NULL DEFAULT ''
);

-- Backfill for databases created before the OAuth-identity columns.
ALTER TABLE users ADD COLUMN IF NOT EXISTS github_username TEXT NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS linkedin_sub TEXT NOT NULL DEFAULT '';
-- 4.11 (e): backfill for databases created before the linked-identity columns.
ALTER TABLE users ADD COLUMN IF NOT EXISTS linked_github_username TEXT NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS linked_github_avatar TEXT NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS linked_linkedin_name TEXT NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS linked_linkedin_avatar TEXT NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_source TEXT NOT NULL DEFAULT '';

-- 2. Roster uploads (one row per uploaded Excel).
CREATE TABLE IF NOT EXISTS rosters (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    filename        TEXT NOT NULL,
    file_hash       TEXT NOT NULL,
    uploaded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    student_count   INTEGER NOT NULL,
    invalid_count   INTEGER NOT NULL DEFAULT 0
);

-- 3. Per-roster student records (original row data for batch re-analysis).
--    raw_json stores the full dict including the 3 optional repo-link columns
--    so batch.analyze_records receives exactly what RosterStore.get() returns today.
CREATE TABLE IF NOT EXISTS students (
    id              SERIAL PRIMARY KEY,
    roster_id       UUID NOT NULL REFERENCES rosters(id) ON DELETE CASCADE,
    student_id      TEXT NOT NULL,
    student_name    TEXT NOT NULL DEFAULT '',
    division        TEXT NOT NULL DEFAULT '',
    batch           TEXT NOT NULL DEFAULT '',
    academic_year   TEXT,
    semester        TEXT,
    github_username TEXT,
    submitted_github_username TEXT,
    raw_json        JSONB NOT NULL,
    UNIQUE (roster_id, student_id)
);

-- 4. Live analysis progress (replaces the in-cache analysis: state dict).
--    One row per roster, updated per batch; contains the live counters and the
--    `recorded` flag so record_analysis_run_if_fresh remains idempotent.
CREATE TABLE IF NOT EXISTS run_summary (
    roster_id   UUID PRIMARY KEY REFERENCES rosters(id) ON DELETE CASCADE,
    file_hash   TEXT,
    status      TEXT NOT NULL DEFAULT 'running',
    total       INTEGER NOT NULL DEFAULT 0,
    done        INTEGER NOT NULL DEFAULT 0,
    valid       INTEGER NOT NULL DEFAULT 0,
    invalid     INTEGER NOT NULL DEFAULT 0,
    errors      INTEGER NOT NULL DEFAULT 0,
    recorded    BOOLEAN NOT NULL DEFAULT FALSE,
    recorded_at TIMESTAMPTZ,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 5. Dashboard results (one row per student per roster).
--    Stores the full 29-column analysis_results output (+ 4 Sep-2026 profile
--    columns: LinkedIn/HackerRank handles + URLs for the Students tab, + 12
--    team-activity columns for group-project contributions).
CREATE TABLE IF NOT EXISTS analysis_results (
    id                          SERIAL PRIMARY KEY,
    roster_id                   UUID NOT NULL REFERENCES rosters(id) ON DELETE CASCADE,
    student_id                  TEXT NOT NULL,
    student_name                TEXT,
    division                    TEXT,
    batch                       TEXT,
    academic_year               TEXT,
    semester                    TEXT,
    github_username             TEXT,
    submitted_github_username   TEXT,
    username_changed            BOOLEAN,
    public_repos                INTEGER,
    repository_count            INTEGER,
    active_repositories         INTEGER,
    repo_fetch_status           TEXT,
    pull_requests               INTEGER,
    open_prs                    INTEGER,
    closed_prs                  INTEGER,
    issues_opened               INTEGER,
    open_issues                 INTEGER,
    external_prs                INTEGER,
    contrib_fetch_status        TEXT,
    team_commits                INTEGER NOT NULL DEFAULT 0,
    team_push_events            INTEGER NOT NULL DEFAULT 0,
    team_pr_events              INTEGER NOT NULL DEFAULT 0,
    team_total_events           INTEGER NOT NULL DEFAULT 0,
    team_commits_30d            INTEGER NOT NULL DEFAULT 0,
    team_total_events_30d       INTEGER NOT NULL DEFAULT 0,
    team_active_dates           TEXT NOT NULL DEFAULT '',
    team_active_repos           INTEGER NOT NULL DEFAULT 0,
    contributed_repos_count     INTEGER NOT NULL DEFAULT 0,
    contributed_repos           TEXT NOT NULL DEFAULT '',
    team_last_active_at         TEXT NOT NULL DEFAULT '',
    team_activity_fetch_status  TEXT NOT NULL DEFAULT 'Loaded',
    followers                   INTEGER,
    following                   INTEGER,
    account_age_years           REAL,
    repos_per_account_year      REAL,
    followers_per_account_year  REAL,
    following_per_account_year  REAL,
    primary_language            TEXT,
    avatar_url                  TEXT,
    profile_url                 TEXT,
    linkedin_username           TEXT,
    linkedin_url                TEXT,
    hackerrank_username         TEXT,
    hackerrank_url              TEXT,
    outcome                     TEXT,
    UNIQUE (roster_id, student_id)
);
-- Sep-2026 migration for pre-existing databases (init_schema runs this file
-- on every startup; ADD COLUMN IF NOT EXISTS is a no-op when already present).
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS linkedin_username TEXT;
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS linkedin_url TEXT;
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS hackerrank_username TEXT;
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS hackerrank_url TEXT;
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS team_commits INTEGER NOT NULL DEFAULT 0;
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS team_push_events INTEGER NOT NULL DEFAULT 0;
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS team_pr_events INTEGER NOT NULL DEFAULT 0;
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS team_total_events INTEGER NOT NULL DEFAULT 0;
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS team_commits_30d INTEGER NOT NULL DEFAULT 0;
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS team_total_events_30d INTEGER NOT NULL DEFAULT 0;
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS team_active_dates TEXT NOT NULL DEFAULT '';
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS team_active_repos INTEGER NOT NULL DEFAULT 0;
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS contributed_repos_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS contributed_repos TEXT NOT NULL DEFAULT '';
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS team_last_active_at TEXT NOT NULL DEFAULT '';
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS team_activity_fetch_status TEXT NOT NULL DEFAULT 'Loaded';

-- 6. Repository inventory (per-roster snapshot; pruned by retention helper).
CREATE TABLE IF NOT EXISTS roster_repositories (
    id                          SERIAL PRIMARY KEY,
    roster_id                   UUID NOT NULL REFERENCES rosters(id) ON DELETE CASCADE,
    username                    TEXT NOT NULL,
    repository                  TEXT NOT NULL,
    language                    TEXT,
    stars                       INTEGER,
    forks                       INTEGER,
    description                 TEXT,
    license                     TEXT,
    created_at                  TIMESTAMPTZ,
    updated_at                  TIMESTAMPTZ,
    repository_url              TEXT,
    maintenance_status          TEXT,
    repository_quality_score    INTEGER,
    quality_band                TEXT,
    UNIQUE (roster_id, username, repository_url)
);

-- 6b. Team-contributed repos (per-roster snapshot of external-repo activity
--     derived from the public events API — the group-project fix).
CREATE TABLE IF NOT EXISTS roster_team_repos (
    id              SERIAL PRIMARY KEY,
    roster_id       UUID NOT NULL REFERENCES rosters(id) ON DELETE CASCADE,
    username        TEXT NOT NULL,
    team_repo       TEXT NOT NULL,
    team_repo_url   TEXT NOT NULL DEFAULT '',
    commits         INTEGER NOT NULL DEFAULT 0,
    push_events     INTEGER NOT NULL DEFAULT 0,
    pr_events       INTEGER NOT NULL DEFAULT 0,
    total_events    INTEGER NOT NULL DEFAULT 0,
    last_active_at  TEXT NOT NULL DEFAULT '',
    language        TEXT,
    stars           INTEGER NOT NULL DEFAULT 0,
    forks           INTEGER NOT NULL DEFAULT 0,
    description     TEXT,
    UNIQUE (roster_id, username, team_repo_url)
);
ALTER TABLE roster_team_repos ADD COLUMN IF NOT EXISTS language TEXT;
ALTER TABLE roster_team_repos ADD COLUMN IF NOT EXISTS stars INTEGER NOT NULL DEFAULT 0;
ALTER TABLE roster_team_repos ADD COLUMN IF NOT EXISTS forks INTEGER NOT NULL DEFAULT 0;
ALTER TABLE roster_team_repos ADD COLUMN IF NOT EXISTS description TEXT;

-- 7. Issues (per-roster).
CREATE TABLE IF NOT EXISTS roster_issues (
    id                      SERIAL PRIMARY KEY,
    roster_id               UUID NOT NULL REFERENCES rosters(id) ON DELETE CASCADE,
    student_id              TEXT,
    student_name            TEXT,
    division                TEXT,
    batch                   TEXT,
    github_account_link     TEXT,
    github_username         TEXT,
    issue                   TEXT
);

-- 8. Workflow state (editable issue follow-up state; JSONB mirrors the RosterStore dict).
CREATE TABLE IF NOT EXISTS workflow_state (
    roster_id   UUID PRIMARY KEY REFERENCES rosters(id) ON DELETE CASCADE,
    state       JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- 9. Historical analysis runs (mirrors the legacy analytics_history.db schema).
CREATE TABLE IF NOT EXISTS analysis_runs (
    id                  SERIAL PRIMARY KEY,
    roster_id           UUID,
    run_timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status              TEXT NOT NULL,
    total_students      INTEGER,
    valid_accounts      INTEGER,
    invalid_accounts    INTEGER,
    error_accounts      INTEGER,
    repos_found         INTEGER,
    active_repos        INTEGER,
    avg_quality_score   REAL,
    elapsed_seconds     REAL,
    source_file_hash    TEXT
);

-- 10. Audit log (event trail — same semantics as legacy storage.log_event).
CREATE TABLE IF NOT EXISTS audit_log (
    id              SERIAL PRIMARY KEY,
    event_timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    event_type      TEXT NOT NULL,
    detail          TEXT
);

-- 11. Support tickets (student-raised help requests triaged by staff).
CREATE TABLE IF NOT EXISTS support_tickets (
    id              SERIAL PRIMARY KEY,
    created_by      TEXT NOT NULL,
    student_name    TEXT NOT NULL DEFAULT '',
    subject         TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT 'General',
    message         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'Open',
    admin_reply     TEXT NOT NULL DEFAULT '',
    followup_question TEXT NOT NULL DEFAULT '',
    student_reply   TEXT NOT NULL DEFAULT '',
    reply_attachment_name TEXT NOT NULL DEFAULT '',
    reply_attachment_data BYTEA,
    attachment_name TEXT NOT NULL DEFAULT '',
    attachment_data BYTEA,
    student_attachment_name TEXT NOT NULL DEFAULT '',
    student_attachment_data BYTEA,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_support_tickets_creator ON support_tickets (created_by);
CREATE INDEX IF NOT EXISTS idx_support_tickets_status ON support_tickets (status);
ALTER TABLE support_tickets ADD COLUMN IF NOT EXISTS attachment_name TEXT NOT NULL DEFAULT '';
ALTER TABLE support_tickets ADD COLUMN IF NOT EXISTS attachment_data BYTEA;
ALTER TABLE support_tickets ADD COLUMN IF NOT EXISTS student_attachment_name TEXT NOT NULL DEFAULT '';
ALTER TABLE support_tickets ADD COLUMN IF NOT EXISTS student_attachment_data BYTEA;
ALTER TABLE support_tickets ADD COLUMN IF NOT EXISTS student_reply TEXT NOT NULL DEFAULT '';
ALTER TABLE support_tickets ADD COLUMN IF NOT EXISTS followup_question TEXT NOT NULL DEFAULT '';
ALTER TABLE support_tickets ADD COLUMN IF NOT EXISTS reply_attachment_name TEXT NOT NULL DEFAULT '';
ALTER TABLE support_tickets ADD COLUMN IF NOT EXISTS reply_attachment_data BYTEA;

-- Useful indexes (created only once even under IF NOT EXISTS).
CREATE INDEX IF NOT EXISTS idx_students_roster        ON students (roster_id);
CREATE INDEX IF NOT EXISTS idx_analysis_results_roster ON analysis_results (roster_id);
CREATE INDEX IF NOT EXISTS idx_roster_repos_roster     ON roster_repositories (roster_id);
CREATE INDEX IF NOT EXISTS idx_roster_repos_user       ON roster_repositories (username);
CREATE INDEX IF NOT EXISTS idx_team_repos_roster       ON roster_team_repos (roster_id);
CREATE INDEX IF NOT EXISTS idx_team_repos_user         ON roster_team_repos (username);
CREATE INDEX IF NOT EXISTS idx_roster_issues_roster    ON roster_issues (roster_id);
CREATE INDEX IF NOT EXISTS idx_analysis_runs_timestamp ON analysis_runs (run_timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_log_type           ON audit_log (event_type);

-- Backfill columns for GitHub/LinkedIn OAuth (Phase 4.7 extension).
ALTER TABLE users ADD COLUMN IF NOT EXISTS github_username TEXT NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS linkedin_sub TEXT NOT NULL DEFAULT '';
