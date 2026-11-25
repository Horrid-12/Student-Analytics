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
    profile_source         TEXT NOT NULL DEFAULT '',
    -- BUG-141: the public LinkedIn slug the student supplied (OIDC only
    -- returns a display name, which must never be used as a profile URL).
    linkedin_username      TEXT NOT NULL DEFAULT ''
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
-- BUG-141: LinkedIn profile slug captured from the student (sign-up/Settings).
ALTER TABLE users ADD COLUMN IF NOT EXISTS linkedin_username TEXT NOT NULL DEFAULT '';

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

-- 8d. Student privacy prefs (student_id = roster PRN; 0/1 flags for fields a
--     student hides from OTHER students — faculty/admins always see all).
--     Hiding GitHub/HackerRank also excludes the student from the connected
--     leaderboards (merged into the blacklist at render time).
CREATE TABLE IF NOT EXISTS privacy_prefs (
    student_id    TEXT PRIMARY KEY,
    hide_prn      INTEGER NOT NULL DEFAULT 0,
    hide_github   INTEGER NOT NULL DEFAULT 0,
    hide_linkedin INTEGER NOT NULL DEFAULT 0,
    hide_hackerrank INTEGER NOT NULL DEFAULT 0,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 8e. HackerRank snapshots ({lowercase handle: {practice_score, total_solved,
--     badges, display_name, synced_at}} — progressively cached from
--     profile-view fetches; the HackerRank leaderboards rank from this).
CREATE TABLE IF NOT EXISTS hackerrank_snapshots (
    handle      TEXT PRIMARY KEY,
    state       JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
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
ALTER TABLE support_tickets ADD COLUMN IF NOT EXISTS attachment_name TEXT NOT NULL DEFAULT '';
ALTER TABLE support_tickets ADD COLUMN IF NOT EXISTS attachment_data BYTEA;
ALTER TABLE support_tickets ADD COLUMN IF NOT EXISTS student_attachment_name TEXT NOT NULL DEFAULT '';
ALTER TABLE support_tickets ADD COLUMN IF NOT EXISTS student_attachment_data BYTEA;
ALTER TABLE support_tickets ADD COLUMN IF NOT EXISTS student_reply TEXT NOT NULL DEFAULT '';
ALTER TABLE support_tickets ADD COLUMN IF NOT EXISTS followup_question TEXT NOT NULL DEFAULT '';
ALTER TABLE support_tickets ADD COLUMN IF NOT EXISTS reply_attachment_name TEXT NOT NULL DEFAULT '';
ALTER TABLE support_tickets ADD COLUMN IF NOT EXISTS reply_attachment_data BYTEA;

-- Useful indexes (created only once even under IF NOT EXISTS).
-- Lag Fix phase 1: the hot read paths compare lower(...), so a plain btree on
-- the raw column is never used. Function indexes match those predicates
-- exactly (lower() is IMMUTABLE). users.onboarding_status is a plain column
-- predicate ('approved' / 'none' / = ANY(...)).

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
ALTER TABLE users ADD COLUMN IF NOT EXISTS semester TEXT NOT NULL DEFAULT 'Semester 1';
ALTER TABLE users ADD COLUMN IF NOT EXISTS hackerrank_username TEXT NOT NULL DEFAULT '';

-- Backfill sign-up specialisation track (sign-up phase 2/4 finalization).
ALTER TABLE users ADD COLUMN IF NOT EXISTS specialisation TEXT NOT NULL DEFAULT '';

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
ALTER TABLE account_snapshots ADD COLUMN IF NOT EXISTS team_repos_json JSONB NOT NULL DEFAULT '[]'::jsonb;

-- 13. (Removed: Verification page + reference_sheets table. Pre-existing
--     deployments keep the orphan table; no code reads it anymore.)

-- 14. Notifications (Real-time student alerts for support ticket updates)
--     ticket_id is nullable: weekly announcements are fan-out notifications
--     with no ticket (BUG-143); ticket notifications still carry a real id.
CREATE TABLE IF NOT EXISTS notifications (
    id          SERIAL PRIMARY KEY,
    user_id     TEXT NOT NULL,
    ticket_id   INTEGER REFERENCES support_tickets(id) ON DELETE CASCADE,
    type        TEXT NOT NULL,
    title       TEXT NOT NULL,
    message     TEXT NOT NULL,
    is_read     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- BUG-143: relax existing deployments (CREATE TABLE IF NOT EXISTS cannot
-- change a column's nullability). Announcements insert ticket_id NULL.
ALTER TABLE notifications ALTER COLUMN ticket_id DROP NOT NULL;

-- 15. Fleet-mode fix: blacklist/hidden keys are TEXT ('fleet' + roster UUIDs),
--     not UUID FKs. Fresh tables above are already TEXT; these migrate
--     live DBs created before the fix (UUID PK + FK to rosters — a table since
--     removed — rejected the 'fleet' key with "invalid input syntax for type
--     uuid", so every
--     fleet-mode POST returned ok but the next GET read back {}).
ALTER TABLE leaderboard_blacklist DROP CONSTRAINT IF EXISTS leaderboard_blacklist_roster_id_fkey;
ALTER TABLE leaderboard_hidden_repos DROP CONSTRAINT IF EXISTS leaderboard_hidden_repos_roster_id_fkey;
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
    profile_source          TEXT NOT NULL DEFAULT '',
    teaching_json           TEXT NOT NULL DEFAULT '{}'
);
ALTER TABLE faculty ADD COLUMN IF NOT EXISTS teaching_json TEXT NOT NULL DEFAULT '{}';

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

CREATE INDEX IF NOT EXISTS idx_support_tickets_creator ON support_tickets (created_by);
CREATE INDEX IF NOT EXISTS idx_support_tickets_status ON support_tickets (status);
CREATE INDEX IF NOT EXISTS idx_audit_log_type           ON audit_log (event_type);
CREATE INDEX IF NOT EXISTS idx_support_tickets_creator_lower ON support_tickets (lower(created_by));
CREATE INDEX IF NOT EXISTS idx_users_onboarding          ON users (onboarding_status);
CREATE INDEX IF NOT EXISTS idx_account_snapshots_synced ON account_snapshots (synced_at);
CREATE INDEX IF NOT EXISTS idx_notifications_ticket_id ON notifications (ticket_id);
CREATE INDEX IF NOT EXISTS idx_notifications_is_read ON notifications (is_read);
CREATE INDEX IF NOT EXISTS idx_notifications_user_lower   ON notifications (lower(user_id));

-- 19. Student certificates (Phase 19 / Row 78).
--     Queued by students on their profile (/me), verified by admins on /verification.
CREATE TABLE IF NOT EXISTS certificates (
    id              SERIAL PRIMARY KEY,
    student_email   TEXT NOT NULL,
    student_id      TEXT NOT NULL DEFAULT '',
    student_name    TEXT NOT NULL DEFAULT '',
    title           TEXT NOT NULL,
    issuer          TEXT NOT NULL,
    issue_date      TEXT NOT NULL DEFAULT '',
    credential_url  TEXT NOT NULL DEFAULT '',
    credential_id   TEXT NOT NULL DEFAULT '',
    skills          TEXT NOT NULL DEFAULT '',
    attachment_name TEXT NOT NULL DEFAULT '',
    attachment_data BYTEA,
    status          TEXT NOT NULL DEFAULT 'pending',
    admin_notes     TEXT NOT NULL DEFAULT '',
    verified_by     TEXT NOT NULL DEFAULT '',
    verified_at     TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_certificates_student_email ON certificates (lower(student_email));
CREATE INDEX IF NOT EXISTS idx_certificates_student_id ON certificates (student_id);
CREATE INDEX IF NOT EXISTS idx_certificates_status ON certificates (status);

-- 20. Student AutoCAD DWG uploads (metadata + file, no grading in the MVP).
CREATE TABLE IF NOT EXISTS autocad_submissions (
    id              SERIAL PRIMARY KEY,
    student_email   TEXT NOT NULL,
    student_id      TEXT NOT NULL DEFAULT '',
    student_name    TEXT NOT NULL DEFAULT '',
    filename        TEXT NOT NULL,
    file_size       INTEGER NOT NULL,
    file_data       BYTEA NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_autocad_submissions_email
    ON autocad_submissions (lower(student_email));
CREATE INDEX IF NOT EXISTS idx_autocad_submissions_created
    ON autocad_submissions (created_at);

-- 21. Sign-up drafts are no longer stored: the wizard's in-progress payload
--     (email, PRN, branch, linked handles) travels sealed in the gsad_signup
--     cookie and the account is created once, in phase 4. Drop the old table
--     so no sign-up data is kept server-side.
DROP TABLE IF EXISTS signup_progress;
