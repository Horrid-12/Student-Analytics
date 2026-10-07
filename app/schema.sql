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
--    One row per roster, updated per batch; contains the live counters.
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
    team_commits_90d            INTEGER NOT NULL DEFAULT 0,
    team_total_events_30d       INTEGER NOT NULL DEFAULT 0,
    team_active_dates           TEXT NOT NULL DEFAULT '',
    team_active_repos           INTEGER NOT NULL DEFAULT 0,
    contributed_repos_count     INTEGER NOT NULL DEFAULT 0,
    contributed_repos           TEXT NOT NULL DEFAULT '',
    team_last_active_at         TEXT NOT NULL DEFAULT '',
    team_activity_fetch_status  TEXT NOT NULL DEFAULT 'Loaded',
    owned_commits               INTEGER NOT NULL DEFAULT 0,
    owned_commits_30d           INTEGER NOT NULL DEFAULT 0,
    owned_commits_90d           INTEGER NOT NULL DEFAULT 0,
    commit_fetch_status         TEXT NOT NULL DEFAULT 'Loaded',
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
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS team_commits_90d INTEGER NOT NULL DEFAULT 0;
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS team_total_events_30d INTEGER NOT NULL DEFAULT 0;
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS team_active_dates TEXT NOT NULL DEFAULT '';
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS team_active_repos INTEGER NOT NULL DEFAULT 0;
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS contributed_repos_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS contributed_repos TEXT NOT NULL DEFAULT '';
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS team_last_active_at TEXT NOT NULL DEFAULT '';
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS team_activity_fetch_status TEXT NOT NULL DEFAULT 'Loaded';
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS owned_commits INTEGER NOT NULL DEFAULT 0;
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS owned_commits_30d INTEGER NOT NULL DEFAULT 0;
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS owned_commits_90d INTEGER NOT NULL DEFAULT 0;
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS commit_fetch_status TEXT NOT NULL DEFAULT 'Loaded';

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
    commits                     INTEGER NOT NULL DEFAULT 0,
    commits_30d                 INTEGER NOT NULL DEFAULT 0,
    commits_90d                 INTEGER NOT NULL DEFAULT 0,
    pull_requests               INTEGER NOT NULL DEFAULT 0,
    issues                      INTEGER NOT NULL DEFAULT 0,
    contributors                INTEGER NOT NULL DEFAULT 0,
    has_readme                  INTEGER NOT NULL DEFAULT 0,
    topics_count                INTEGER NOT NULL DEFAULT 0,
    total_commits               INTEGER NOT NULL DEFAULT 0,
    UNIQUE (roster_id, username, repository_url)
);
ALTER TABLE roster_repositories ADD COLUMN IF NOT EXISTS commits INTEGER NOT NULL DEFAULT 0;
ALTER TABLE roster_repositories ADD COLUMN IF NOT EXISTS commits_30d INTEGER NOT NULL DEFAULT 0;
ALTER TABLE roster_repositories ADD COLUMN IF NOT EXISTS commits_90d INTEGER NOT NULL DEFAULT 0;
ALTER TABLE roster_repositories ADD COLUMN IF NOT EXISTS pull_requests INTEGER NOT NULL DEFAULT 0;
ALTER TABLE roster_repositories ADD COLUMN IF NOT EXISTS issues INTEGER NOT NULL DEFAULT 0;
ALTER TABLE roster_repositories ADD COLUMN IF NOT EXISTS contributors INTEGER NOT NULL DEFAULT 0;
ALTER TABLE roster_repositories ADD COLUMN IF NOT EXISTS has_readme INTEGER NOT NULL DEFAULT 0;
ALTER TABLE roster_repositories ADD COLUMN IF NOT EXISTS topics_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE roster_repositories ADD COLUMN IF NOT EXISTS total_commits INTEGER NOT NULL DEFAULT 0;

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
-- NOTE: roster_id is TEXT (not UUID FK) so the roster-less fleet/account views
-- can share the single 'fleet' key alongside real roster UUIDs.
CREATE TABLE IF NOT EXISTS workflow_state (
    roster_id   TEXT PRIMARY KEY,
    state       JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- 8b. Leaderboard blacklist ({student_id: [boards]} — students excluded from
--     specific leaderboards by an admin; mirrors the RosterStore dict).
-- NOTE: TEXT key (not UUID FK) — fleet mode stores under 'fleet'.
CREATE TABLE IF NOT EXISTS leaderboard_blacklist (
    roster_id   TEXT PRIMARY KEY,
    state       JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- 8c. Hidden leaderboard repositories ({student_id: [repo keys]} — single
--     repositories excluded from every leaderboard by an admin).
-- NOTE: TEXT key (not UUID FK) — fleet mode stores under 'fleet'.
CREATE TABLE IF NOT EXISTS leaderboard_hidden_repos (
    roster_id   TEXT PRIMARY KEY,
    state       JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- 8d. HackerRank snapshots ({lowercase handle: {practice_score, total_solved,
--     badges, display_name, synced_at}} — progressively cached from
--     profile-view fetches; the HackerRank leaderboards rank from this).
CREATE TABLE IF NOT EXISTS hackerrank_snapshots (
    handle      TEXT PRIMARY KEY,
    state       JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
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
-- Lag Fix phase 1: the hot read paths compare lower(...), so a plain btree on
-- the raw column is never used. Function indexes match those predicates
-- exactly (lower() is IMMUTABLE). users.onboarding_status is a plain column
-- predicate ('approved' / 'none' / = ANY(...)).
CREATE INDEX IF NOT EXISTS idx_notifications_user_lower   ON notifications (lower(user_id));
CREATE INDEX IF NOT EXISTS idx_support_tickets_creator_lower ON support_tickets (lower(created_by));
CREATE INDEX IF NOT EXISTS idx_users_onboarding          ON users (onboarding_status);

-- Backfill columns for GitHub/LinkedIn OAuth (Phase 4.7 extension).
ALTER TABLE users ADD COLUMN IF NOT EXISTS github_username TEXT NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS linkedin_sub TEXT NOT NULL DEFAULT '';

-- Backfill columns for academic onboarding (Phase 4.12 / account-driven follow-up).
ALTER TABLE users ADD COLUMN IF NOT EXISTS prn TEXT NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS degree_branch TEXT NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS division TEXT NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarding_status TEXT NOT NULL DEFAULT 'none';
ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarding_submitted_at TEXT NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS github_verified_at TEXT NOT NULL DEFAULT '';

-- Backfill batch/semester split (Phase 5.6). main_batch = admission cohort
-- (stored, not shown); practical_batch fills the dashboard "Batch" column.
ALTER TABLE users ADD COLUMN IF NOT EXISTS main_batch TEXT NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS practical_batch TEXT NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS semester TEXT NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS hackerrank_username TEXT NOT NULL DEFAULT '';

-- 12. Per-account analytics snapshots (Phase 5.1 account-driven redesign).
--     One row per synced account: JSONB holds the dashboard-shaped student
--     record + the REPO_COLS repository list + the TEAM_REPOS_COLS
--     contributed-repo list, so student pages rebuild the
--     analysis_view shape without re-fetching GitHub on every request.
CREATE TABLE IF NOT EXISTS account_snapshots (
    email        TEXT PRIMARY KEY,
    username     TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT '',
    student_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    repos_json   JSONB NOT NULL DEFAULT '[]'::jsonb,
    team_repos_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    synced_at    TEXT NOT NULL DEFAULT '',
    error        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_account_snapshots_synced ON account_snapshots (synced_at);
ALTER TABLE account_snapshots ADD COLUMN IF NOT EXISTS team_repos_json JSONB NOT NULL DEFAULT '[]'::jsonb;

-- 13. (Removed: Verification page + reference_sheets table. Pre-existing
--     deployments keep the orphan table; no code reads it anymore.)

-- 14. Notifications (Real-time student alerts for support ticket updates)
CREATE TABLE IF NOT EXISTS notifications (
    id          SERIAL PRIMARY KEY,
    user_id     TEXT NOT NULL,
    ticket_id   INTEGER NOT NULL REFERENCES support_tickets(id) ON DELETE CASCADE,
    type        TEXT NOT NULL,
    title       TEXT NOT NULL,
    message     TEXT NOT NULL,
    is_read     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_notifications_user_id ON notifications (user_id);
CREATE INDEX IF NOT EXISTS idx_notifications_ticket_id ON notifications (ticket_id);
CREATE INDEX IF NOT EXISTS idx_notifications_is_read ON notifications (is_read);

-- 15. Fleet-mode fix: blacklist/hidden/workflow keys are TEXT ('fleet' + roster
--     UUIDs), not UUID FKs. Fresh tables above are already TEXT; these migrate
--     live DBs created before the fix (UUID PK + REFERENCES rosters FK rejected
--     the 'fleet' key with "invalid input syntax for type uuid" so every
--     fleet-mode POST returned ok but the next GET read back {}).
ALTER TABLE workflow_state DROP CONSTRAINT IF EXISTS workflow_state_roster_id_fkey;
ALTER TABLE leaderboard_blacklist DROP CONSTRAINT IF EXISTS leaderboard_blacklist_roster_id_fkey;
ALTER TABLE leaderboard_hidden_repos DROP CONSTRAINT IF EXISTS leaderboard_hidden_repos_roster_id_fkey;
ALTER TABLE workflow_state ALTER COLUMN roster_id TYPE TEXT USING roster_id::text;
ALTER TABLE leaderboard_blacklist ALTER COLUMN roster_id TYPE TEXT USING roster_id::text;
ALTER TABLE leaderboard_hidden_repos ALTER COLUMN roster_id TYPE TEXT USING roster_id::text;

-- 16. Schema marker (Lag Fix phase 3).
--     SHA-256 of this file, written by db.init_schema() after a successful
--     run so warm startups can skip the whole DDL block (a full run over the
--     remote pooler costs 25-55 s on every cold start). Editing schema.sql
--     changes the hash and forces a re-run on the next boot.
CREATE TABLE IF NOT EXISTS schema_meta (
    id          SMALLINT PRIMARY KEY,
    schema_hash TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- Backfill for HackerRank on onboarding.
ALTER TABLE users ADD COLUMN IF NOT EXISTS hackerrank_username TEXT NOT NULL DEFAULT '';

-- 18. Faculty accounts live here — never in ``users``. Same identity columns
--     (password/avatar/linked OAuth identities) but no academic or onboarding
--     columns (faculty never onboard as students).
CREATE TABLE IF NOT EXISTS faculty (
    id                      SERIAL PRIMARY KEY,
    email                   TEXT NOT NULL UNIQUE,
    password_hash           TEXT,
    name                    TEXT NOT NULL DEFAULT '',
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    auth_source             TEXT NOT NULL DEFAULT 'password',
    google_sub              TEXT NOT NULL DEFAULT '',
    github_username         TEXT NOT NULL DEFAULT '',
    linkedin_sub            TEXT NOT NULL DEFAULT '',
    linked_github_username  TEXT NOT NULL DEFAULT '',
    linked_github_avatar    TEXT NOT NULL DEFAULT '',
    linked_linkedin_name    TEXT NOT NULL DEFAULT '',
    linked_linkedin_avatar  TEXT NOT NULL DEFAULT '',
    profile_source          TEXT NOT NULL DEFAULT ''
);

-- One-way migration from before the split: move legacy users.role='faculty'
-- rows into faculty, then drop them from users. Idempotent (CONFLICT-safe);
-- runs once when this file's hash changes forces a re-apply.
INSERT INTO faculty (email, password_hash, name, created_at, auth_source, google_sub,
    github_username, linkedin_sub, linked_github_username, linked_github_avatar,
    linked_linkedin_name, linked_linkedin_avatar, profile_source)
SELECT email, password_hash, name, created_at, auth_source, google_sub,
    github_username, linkedin_sub, linked_github_username, linked_github_avatar,
    linked_linkedin_name, linked_linkedin_avatar, profile_source
FROM users WHERE role = 'faculty'
ON CONFLICT (email) DO NOTHING;
DELETE FROM users WHERE role = 'faculty';

-- 17. Faculty invites: one-time pre-saved login credentials. The first successful
--     login with an unused invite forces the /faculty-setup flow, which registers
--     the real faculty account and flags the invite used=1 so the pre-saved
--     credential stops verifying (consumed env invites leave a used tombstone row).
CREATE TABLE IF NOT EXISTS faculty_invites (
    invite_email  TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL DEFAULT '',
    used          INTEGER NOT NULL DEFAULT 0,
    consumed_by   TEXT NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
