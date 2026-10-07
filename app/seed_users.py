"""Bootstrap admin/faculty accounts: ``python -m app.seed_users EMAIL PASSWORD ROLE [NAME] [--invite]``

Public signup only ever creates students. Run this once (locally) to create
admin and faculty accounts; re-running with the same email updates the password
and role in place. Role must be one of: student, faculty, admin.

``--invite`` mints a ONE-TIME faculty invite instead of a real account: the
first login with the pre-saved EMAIL/PASSWORD is forced through /faculty-setup
(welcome popup → new email + password), which registers the permanent faculty
account and kills the invite credential. Only meaningful with ROLE=faculty.

On Vercel the users SQLite DB is read-only and does not persist — run the seed
against a host with a writable filesystem until Phase 4.8 moves accounts to
Postgres. The FACULTY_INVITE_EMAIL / FACULTY_INVITE_PASSWORD_HASH (or
FACULTY_INVITE_PASSWORD plaintext) env pair covers Vercel without DB writes.
"""

import sys


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    invite_mode = "--invite" in argv
    argv = [a for a in argv if a != "--invite"]
    if len(argv) < 3:
        print("usage: python -m app.seed_users EMAIL PASSWORD ROLE [NAME] [--invite]")
        print("       ROLE in {student, faculty, admin}  (--invite only with faculty)")
        return 2
    email, password, role, name = argv[0], argv[1], argv[2].strip().lower(), (argv[3] if len(argv) > 3 else "")

    from app import auth

    if role not in auth.ROLES:
        print(f"invalid role '{role}' — must be one of {', '.join(auth.ROLES)}")
        return 2

    if invite_mode:
        if role != "faculty":
            print("--invite is only valid with ROLE=faculty")
            return 2
        if not auth.create_faculty_invite(email, password):
            print(f"invite failed: {email} (already a user, already consumed, or DB unavailable)")
            return 1
        print(f"OK faculty invite: {email} (one-time — first login forces /faculty-setup)")
        return 0

    existing = auth.get_user(email)
    if existing is None:
        user = auth.create_user(email, password, role, name)
        if user is None:
            print(f"signup failed: {email} (database unavailable or email taken)")
            return 1
    else:
        if not auth.set_user_role(email, role):
            print(f"role update failed for {email}")
            return 1
        if not auth.set_user_password(email, password):
            print(f"password update failed for {email}")
            return 1
    print(f"OK: {email} -> {role}")


if __name__ == "__main__":
    raise SystemExit(main())