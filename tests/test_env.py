"""Phase 5.3 — .env.local/.env loader tests (BUG-114 follow-up).

The app auto-loads the `vercel env pull` file so Google OAuth, GitHub token and
DATABASE_URL share one gitignored secrets source instead of `.streamlit/
secrets.toml` + manual shell exports. Shell env must always win the merge.
"""

from app import env

MARKER = env.PYTEST_MARKER


def _no_marker(monkeypatch):
    monkeypatch.delenv(MARKER, raising=False)


def test_parse_strips_quotes_and_comments():
    text = (
        '# comment\n'
        'GOOGLE_CLIENT_ID = "abc"\n'
        'GITHUB_TOKEN=xyz\n'
        '\n'
        'EMPTY=\n'
        'export DATABASE_URL="postgres://u:p@h/db"\n'
    )
    assert env._parse(text) == [
        ("GOOGLE_CLIENT_ID", "abc"),
        ("GITHUB_TOKEN", "xyz"),
        ("EMPTY", ""),
        ("DATABASE_URL", "postgres://u:p@h/db"),
    ]


def test_loads_env_local_into_os_environ(tmp_path, monkeypatch):
    _no_marker(monkeypatch)
    monkeypatch.setattr(env, "_BASE", tmp_path)
    (tmp_path / ".env.local").write_text(
        'GOOGLE_CLIENT_ID = "who-am-i.apps.googleusercontent.com"\n'
        'GOOGLE_CLIENT_SECRET = "s3cret"\n',
        encoding="utf-8",
    )
    for key in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
        monkeypatch.delenv(key, raising=False)
    count = env.load_dotenv_local()
    assert count == 2
    assert env.os.environ["GOOGLE_CLIENT_ID"] == "who-am-i.apps.googleusercontent.com"
    assert env.os.environ["GOOGLE_CLIENT_SECRET"] == "s3cret"


def test_shell_env_wins_over_file(tmp_path, monkeypatch):
    _no_marker(monkeypatch)
    monkeypatch.setattr(env, "_BASE", tmp_path)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "shell-value")
    (tmp_path / ".env.local").write_text(
        'GOOGLE_CLIENT_ID = "file-value"\n', encoding="utf-8"
    )
    assert env.load_dotenv_local() == 0
    assert env.os.environ["GOOGLE_CLIENT_ID"] == "shell-value"


def test_env_local_wins_over_env(tmp_path, monkeypatch):
    _no_marker(monkeypatch)
    monkeypatch.setattr(env, "_BASE", tmp_path)
    (tmp_path / ".env").write_text("GITHUB_TOKEN=base\n", encoding="utf-8")
    (tmp_path / ".env.local").write_text("GITHUB_TOKEN=local\n", encoding="utf-8")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    env.load_dotenv_local()
    assert env.os.environ["GITHUB_TOKEN"] == "local"


def test_missing_files_are_ignored(tmp_path, monkeypatch):
    _no_marker(monkeypatch)
    monkeypatch.setattr(env, "_BASE", tmp_path)
    assert env.load_dotenv_local() == 0


def test_pytest_marker_disables_loading(tmp_path, monkeypatch):
    monkeypatch.setenv(MARKER, "1")
    monkeypatch.setattr(env, "_BASE", tmp_path)
    (tmp_path / ".env.local").write_text('GOOGLE_CLIENT_ID = "x"\n', encoding="utf-8")
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    assert env.load_dotenv_local() == 0
    assert "GOOGLE_CLIENT_ID" not in env.os.environ