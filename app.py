import hashlib
import html
import io
import os
import time
from datetime import datetime

import altair as alt
import pandas as pd
import streamlit as st

from services import RateLimitError, run_analysis


st.set_page_config(
    page_title="GitHub Student Analytics Platform",
    layout="wide",
    initial_sidebar_state="expanded",
)

ACCENT = "#3B82F6"
SUCCESS = "#22C55E"
WARNING = "#F59E0B"
DANGER = "#EF4444"
PURPLE = "#8B5CF6"


def inject_theme() -> None:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

        :root {
            --bg: #09090B;
            --sidebar: #111114;
            --card: #18181B;
            --card-soft: #1F1F24;
            --border: #2A2A30;
            --text: #FFFFFF;
            --muted: #A1A1AA;
            --blue: #3B82F6;
            --green: #22C55E;
            --amber: #F59E0B;
            --red: #EF4444;
            --purple: #8B5CF6;
        }

        html, body, [class*="css"] {
            font-family: 'Inter', system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
            font-size: 15px;
            line-height: 1.45;
        }

        .stApp {
            background:
                radial-gradient(circle at top left, rgba(59, 130, 246, 0.12), transparent 28rem),
                radial-gradient(circle at top right, rgba(139, 92, 246, 0.10), transparent 28rem),
                var(--bg);
            color: var(--text);
        }

        [data-testid="stSidebar"] {
            background: linear-gradient(180deg, #111114 0%, #0D0D10 100%);
            border-right: 1px solid var(--border);
            width: 260px !important;
        }

        [data-testid="stSidebar"] * {
            color: var(--text);
        }

        [data-testid="stSidebar"] .stRadio label {
            color: var(--muted);
            font-size: 15px;
            font-weight: 650;
        }

        [data-testid="stSidebar"] [role="radiogroup"] > label {
            padding: 11px 12px;
            border-radius: 12px;
            margin-bottom: 5px;
            transition: 180ms ease;
        }

        [data-testid="stSidebar"] [role="radiogroup"] > label:hover {
            background: rgba(255, 255, 255, 0.04);
        }

        [data-testid="stSidebar"] [role="radiogroup"] > label[data-checked="true"] {
            background: rgba(59, 130, 246, 0.13);
            border: 1px solid rgba(59, 130, 246, 0.26);
        }

        .main .block-container {
            max-width: 1600px;
            padding: 22px 32px 42px;
            margin: 0 auto;
        }

        [data-testid="stHorizontalBlock"] {
            gap: 0.9rem;
        }

        h1, h2, h3 {
            letter-spacing: 0;
        }

        .platform-brand {
            display: flex;
            align-items: center;
            gap: 13px;
            padding: 12px 2px 20px;
        }

        .brand-mark {
            width: 42px;
            height: 42px;
            display: grid;
            place-items: center;
            border-radius: 12px;
            background: linear-gradient(135deg, #2A2A30, #18181B);
            border: 1px solid var(--border);
            box-shadow: 0 14px 30px rgba(0, 0, 0, 0.35);
        }

        .brand-title {
            font-size: 16px;
            font-weight: 700;
            line-height: 1.2;
        }

        .brand-subtitle {
            color: var(--muted);
            font-size: 13px;
            margin-top: 4px;
        }

        .sidebar-footer {
            margin-top: 24px;
            padding: 13px;
            border: 1px solid var(--border);
            border-radius: 16px;
            background: rgba(24, 24, 27, 0.72);
        }

        .status-row {
            display: flex;
            justify-content: space-between;
            gap: 12px;
            color: var(--muted);
            font-size: 14px;
            margin: 8px 0;
        }

        .badge, .badge-green, .badge-amber, .badge-red, .badge-blue, .badge-purple {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            border-radius: 999px;
            padding: 5px 10px;
            font-size: 13px;
            font-weight: 600;
            border: 1px solid var(--border);
            white-space: nowrap;
        }

        .badge-green { color: #86EFAC; background: rgba(34, 197, 94, 0.10); border-color: rgba(34, 197, 94, 0.25); }
        .badge-amber { color: #FCD34D; background: rgba(245, 158, 11, 0.10); border-color: rgba(245, 158, 11, 0.25); }
        .badge-red { color: #FCA5A5; background: rgba(239, 68, 68, 0.10); border-color: rgba(239, 68, 68, 0.25); }
        .badge-blue { color: #93C5FD; background: rgba(59, 130, 246, 0.10); border-color: rgba(59, 130, 246, 0.25); }
        .badge-purple { color: #C4B5FD; background: rgba(139, 92, 246, 0.10); border-color: rgba(139, 92, 246, 0.25); }

        .external-link-button {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            min-height: 42px;
            padding: 0 16px;
            border-radius: 12px;
            border: 1px solid rgba(34, 197, 94, 0.28);
            color: #86EFAC !important;
            background: rgba(34, 197, 94, 0.10);
            font-size: 15px;
            font-weight: 750;
            text-decoration: none !important;
            transition: 180ms ease;
        }

        .external-link-button:hover {
            transform: translateY(-1px);
            border-color: rgba(34, 197, 94, 0.48);
            background: rgba(34, 197, 94, 0.15);
        }

        .external-link-button svg {
            width: 17px;
            height: 17px;
        }

        .topbar {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 16px;
            padding: 15px 17px;
            margin-bottom: 18px;
            border: 1px solid rgba(42, 42, 48, 0.9);
            border-radius: 18px;
            background: rgba(17, 17, 20, 0.78);
            backdrop-filter: blur(18px);
            box-shadow: 0 18px 45px rgba(0, 0, 0, 0.22);
        }

        .topbar-title {
            font-size: 24px;
            font-weight: 800;
            line-height: 1.15;
        }

        .topbar-meta {
            display: flex;
            flex-wrap: wrap;
            gap: 9px;
            align-items: center;
            color: var(--muted);
            font-size: 14px;
        }

        .faculty-avatar {
            width: 38px;
            height: 38px;
            border-radius: 50%;
            display: grid;
            place-items: center;
            color: #FFFFFF;
            background: linear-gradient(135deg, var(--blue), var(--purple));
            font-size: 14px;
            font-weight: 800;
        }

        .hero {
            margin: 2px 0 18px;
        }

        .hero h1 {
            font-size: 36px;
            line-height: 1.1;
            margin: 0;
            font-weight: 800;
        }

        .hero p {
            color: var(--muted);
            font-size: 16px;
            margin: 7px 0 0;
        }

        .metric-card, .panel, .empty-state, .profile-panel {
            border: 1px solid var(--border);
            border-radius: 16px;
            background: linear-gradient(180deg, rgba(24, 24, 27, 0.96), rgba(17, 17, 20, 0.96));
            box-shadow: 0 18px 40px rgba(0, 0, 0, 0.24);
        }

        .metric-card {
            min-height: 146px;
            padding: 16px;
            position: relative;
            overflow: hidden;
            transition: transform 180ms ease, border-color 180ms ease, box-shadow 180ms ease;
        }

        .metric-card:hover {
            transform: translateY(-2px);
            border-color: rgba(59, 130, 246, 0.45);
            box-shadow: 0 24px 55px rgba(0, 0, 0, 0.34);
        }

        .metric-card::after {
            content: "";
            position: absolute;
            inset: auto -20% -45% 20%;
            height: 110px;
            border-radius: 999px;
            background: var(--glow);
            filter: blur(34px);
            opacity: 0.52;
        }

        .metric-top {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            position: relative;
            z-index: 1;
        }

        .metric-icon {
            width: 40px;
            height: 40px;
            border-radius: 12px;
            display: grid;
            place-items: center;
            background: rgba(255, 255, 255, 0.06);
            border: 1px solid rgba(255, 255, 255, 0.08);
        }

        .metric-value {
            position: relative;
            z-index: 1;
            font-size: 46px;
            line-height: 1;
            font-weight: 800;
            margin-top: 15px;
        }

        .metric-label {
            position: relative;
            z-index: 1;
            color: var(--muted);
            font-size: 16px;
            font-weight: 700;
            margin-top: 7px;
        }

        .metric-trend {
            position: relative;
            z-index: 1;
            color: #C7D2FE;
            font-size: 14px;
            font-weight: 600;
            margin-top: 9px;
        }

        .panel {
            padding: 15px;
            margin-bottom: 16px;
        }

        .panel-title {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 10px;
            margin-bottom: 10px;
        }

        .panel-title h3 {
            font-size: 22px;
            margin: 0;
            font-weight: 800;
        }

        .panel-title span {
            color: var(--muted);
            font-size: 14px;
        }

        .pipeline {
            display: grid;
            gap: 7px;
        }

        .pipeline-step {
            display: flex;
            align-items: center;
            gap: 10px;
            color: var(--text);
            font-size: 15px;
            padding: 8px 10px;
            border-radius: 12px;
            border: 1px solid rgba(42, 42, 48, 0.8);
            background: rgba(255, 255, 255, 0.025);
        }

        .dot {
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background: var(--green);
            box-shadow: 0 0 0 5px rgba(34, 197, 94, 0.10);
        }

        .progress-shell {
            height: 11px;
            border-radius: 999px;
            background: #27272A;
            overflow: hidden;
            margin: 11px 0 9px;
        }

        .progress-bar {
            height: 100%;
            width: 100%;
            border-radius: inherit;
            background: linear-gradient(90deg, var(--blue), var(--green));
            animation: breathe 2s ease-in-out infinite;
        }

        @keyframes breathe {
            0%, 100% { opacity: 0.72; }
            50% { opacity: 1; }
        }

        .system-grid {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 8px;
        }

        .system-item {
            padding: 10px 11px;
            border-radius: 14px;
            border: 1px solid var(--border);
            background: rgba(255, 255, 255, 0.025);
        }

        .system-label {
            color: var(--muted);
            font-size: 14px;
        }

        .system-value {
            color: var(--text);
            font-size: 17px;
            font-weight: 700;
            margin-top: 3px;
        }

        .empty-state {
            min-height: 380px;
            display: flex;
            align-items: center;
            justify-content: center;
            text-align: center;
            padding: 34px;
        }

        .empty-illustration {
            width: 86px;
            height: 86px;
            margin: 0 auto 20px;
            border-radius: 26px;
            display: grid;
            place-items: center;
            color: #FFFFFF;
            background: linear-gradient(135deg, rgba(59, 130, 246, 0.82), rgba(139, 92, 246, 0.78));
            box-shadow: 0 24px 70px rgba(59, 130, 246, 0.20);
        }

        .empty-state h2 {
            margin: 0;
            font-size: 26px;
        }

        .empty-state p {
            color: var(--muted);
            margin: 8px 0 0;
            font-size: 16px;
        }

        .profile-panel {
            padding: 15px;
            position: sticky;
            top: 20px;
        }

        .profile-header {
            display: flex;
            gap: 13px;
            align-items: center;
            margin-bottom: 13px;
        }

        .profile-header img {
            width: 68px;
            height: 68px;
            border-radius: 18px;
            border: 1px solid var(--border);
        }

        .profile-name {
            font-size: 20px;
            font-weight: 800;
        }

        .profile-sub {
            color: var(--muted);
            font-size: 14px;
            margin-top: 3px;
        }

        .repo-card {
            padding: 13px;
            border: 1px solid var(--border);
            border-radius: 16px;
            background: rgba(24, 24, 27, 0.88);
            transition: 180ms ease;
            min-height: 118px;
        }

        .repo-card:hover {
            transform: translateY(-2px);
            border-color: rgba(59, 130, 246, 0.45);
        }

        .repo-name {
            font-weight: 700;
            font-size: 16px;
            margin-bottom: 8px;
        }

        .repo-meta {
            color: var(--muted);
            font-size: 14px;
            display: flex;
            flex-wrap: wrap;
            gap: 9px;
            margin-top: 9px;
        }

        .leader-card {
            padding: 12px;
            border: 1px solid var(--border);
            border-radius: 16px;
            background: linear-gradient(180deg, rgba(24, 24, 27, 0.96), rgba(17, 17, 20, 0.96));
            margin-bottom: 8px;
        }

        .leader-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
        }

        .leader-rank {
            width: 30px;
            height: 30px;
            display: grid;
            place-items: center;
            border-radius: 10px;
            background: rgba(59, 130, 246, 0.12);
            color: #BFDBFE;
            font-weight: 800;
            font-size: 13px;
        }

        .leader-title {
            flex: 1;
            font-size: 15px;
            font-weight: 700;
        }

        .leader-score {
            font-size: 22px;
            font-weight: 800;
        }

        .stButton > button, .stDownloadButton > button, [data-testid="stFileUploader"] button {
            border-radius: 12px !important;
            border: 1px solid var(--border) !important;
            background: linear-gradient(180deg, #27272A, #18181B) !important;
            color: var(--text) !important;
            font-weight: 700 !important;
            font-size: 15px !important;
            min-height: 42px !important;
            padding: 0 16px !important;
            transition: 180ms ease !important;
        }

        .stButton > button:hover, .stDownloadButton > button:hover, [data-testid="stFileUploader"] button:hover {
            border-color: rgba(59, 130, 246, 0.6) !important;
            transform: translateY(-1px);
        }

        .stTextInput input, .stSelectbox div[data-baseweb="select"] > div, .stNumberInput input {
            border-radius: 12px !important;
            border-color: var(--border) !important;
            background: #111114 !important;
            color: var(--text) !important;
            font-size: 15px !important;
            min-height: 42px !important;
        }

        [data-testid="stDataFrame"] {
            border: 1px solid var(--border);
            border-radius: 16px;
            overflow: hidden;
            font-size: 15px;
        }

        [data-testid="stDataFrame"] * {
            font-size: 15px !important;
            line-height: 1.4 !important;
        }

        [data-testid="stDataFrame"] [role="columnheader"] * {
            font-size: 14px !important;
            font-weight: 800 !important;
            color: var(--text) !important;
        }

        [data-testid="stDataFrame"] [role="row"] {
            min-height: 38px !important;
        }

        .stAlert {
            border-radius: 16px;
            border: 1px solid var(--border);
            background: rgba(24, 24, 27, 0.94);
            font-size: 15px;
        }

        @media (max-width: 1100px) {
            .topbar {
                align-items: flex-start;
                flex-direction: column;
            }
            .system-grid {
                grid-template-columns: 1fr;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def icon_svg(name: str) -> str:
    icons = {
        "github": """
            <svg width="21" height="21" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                <path d="M12 .5a12 12 0 0 0-3.79 23.39c.6.11.82-.26.82-.58v-2.03c-3.34.73-4.04-1.61-4.04-1.61-.55-1.38-1.33-1.75-1.33-1.75-1.09-.74.08-.73.08-.73 1.2.09 1.84 1.24 1.84 1.24 1.07 1.83 2.8 1.3 3.49.99.11-.78.42-1.3.76-1.6-2.67-.31-5.47-1.34-5.47-5.93 0-1.31.47-2.38 1.24-3.22-.12-.3-.54-1.53.12-3.18 0 0 1.01-.32 3.3 1.23a11.5 11.5 0 0 1 6 0c2.29-1.55 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.77.84 1.23 1.91 1.23 3.22 0 4.61-2.81 5.62-5.49 5.92.43.37.81 1.1.81 2.22v3.29c0 .32.22.7.83.58A12 12 0 0 0 12 .5Z"/>
            </svg>
        """,
        "users": '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="m22 21-3-3m0 0a4 4 0 1 0-5.66-5.66A4 4 0 0 0 19 18Z"/></svg>',
        "check": '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6 9 17l-5-5"/></svg>',
        "alert": '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>',
        "repo": '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M4 4v15.5"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2Z"/></svg>',
        "activity": '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>',
        "chart": '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 3v18h18"/><path d="m19 9-5 5-4-4-3 3"/></svg>',
        "spark": '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m12 3 1.9 5.8L20 12l-6.1 3.2L12 21l-1.9-5.8L4 12l6.1-3.2L12 3Z"/></svg>',
        "upload": '<svg width="42" height="42" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m17 8-5-5-5 5"/><path d="M12 3v12"/></svg>',
        "external": '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/></svg>',
    }
    return icons.get(name, icons["activity"])


def escape(value) -> str:
    if pd.isna(value):
        return ""
    return html.escape(str(value))


def github_profile_url(username) -> str:
    if pd.isna(username) or not str(username).strip():
        return ""
    return f"https://github.com/{str(username).strip()}"


def github_button(url: str, label: str) -> str:
    if not url:
        return '<span class="badge-amber">Unavailable</span>'
    return (
        f'<a class="external-link-button" href="{escape(url)}" '
        f'target="_blank" rel="noopener noreferrer">{icon_svg("github")}{escape(label)}{icon_svg("external")}</a>'
    )


def get_token() -> str:
    token = os.getenv("GITHUB_TOKEN", "")
    try:
        token = token or st.secrets.get("GITHUB_TOKEN", "")
    except Exception:
        pass
    return token or ""


def dataframe_to_excel(df: pd.DataFrame) -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False)
    return output.getvalue()


def filter_text(df: pd.DataFrame, query: str, columns: list[str]) -> pd.DataFrame:
    if not query:
        return df
    mask = pd.Series(False, index=df.index)
    for column in columns:
        if column in df.columns:
            mask = mask | df[column].astype(str).str.contains(
                query,
                case=False,
                na=False,
                regex=False,
            )
    return df[mask]


def apply_value_filter(df: pd.DataFrame, column: str, value: str) -> pd.DataFrame:
    if value == "All" or column not in df.columns:
        return df
    return df[df[column].astype(str) == value]


def format_number(value) -> str:
    try:
        numeric = float(value)
    except Exception:
        return str(value)
    if numeric >= 1_000_000:
        return f"{numeric / 1_000_000:.1f}M"
    if numeric >= 1_000:
        return f"{numeric / 1_000:.1f}K"
    if numeric.is_integer():
        return f"{int(numeric):,}"
    return f"{numeric:.1f}"


def render_sidebar(token_present: bool) -> str:
    with st.sidebar:
        st.markdown(
            f"""
            <div class="platform-brand">
                <div class="brand-mark">{icon_svg("github")}</div>
                <div>
                    <div class="brand-title">GitHub Student<br>Analytics Platform</div>
                    <div class="brand-subtitle">Faculty intelligence suite</div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        page = st.radio(
            "Navigation",
            ["Overview", "Students", "Repositories", "Leaderboards", "Issues", "Verification", "Settings"],
            label_visibility="collapsed",
            key="sidebar_nav",
        )
        connection = "Connected" if token_present else "Token missing"
        badge_class = "badge-green" if token_present else "badge-amber"
        st.markdown(
            f"""
            <div class="sidebar-footer">
                <div class="status-row"><span>GitHub</span><span class="{badge_class}">{connection}</span></div>
                <div class="status-row"><span>Mode</span><span class="badge-purple">Faculty</span></div>
                <div class="status-row"><span>Version</span><span>v1.0.0</span></div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    return page


def render_topbar(last_analysis: str, token_present: bool) -> tuple[object, bool, bool, int | None]:
    status = "Healthy" if token_present else "Needs token"
    status_class = "badge-green" if token_present else "badge-amber"
    st.markdown(
        f"""
        <div class="topbar">
            <div>
                <div class="topbar-title">GitHub Student Analytics Platform</div>
                <div class="topbar-meta">
                    <span>{datetime.now().strftime("%A, %d %B %Y")}</span>
                    <span>Last analysis: {escape(last_analysis)}</span>
                    <span class="{status_class}">GitHub API {status}</span>
                    <span class="badge-blue">Dark mode</span>
                </div>
            </div>
            <div class="topbar-meta">
                <span class="badge-purple">Faculty</span>
                <div class="faculty-avatar">FM</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    controls = st.columns([4, 1.15, 1.15, 1.15, 1.0])
    uploaded_file = controls[0].file_uploader(
        "Excel Upload",
        type=["xlsx"],
        label_visibility="collapsed",
    )
    sample_enabled = controls[1].checkbox("Sample", value=False)
    sample_size = None
    if sample_enabled:
        sample_size = controls[2].number_input("Rows", min_value=1, max_value=735, value=50, step=1)
    else:
        controls[2].markdown("<div style='height: 38px'></div>", unsafe_allow_html=True)
    run_button = controls[3].button("Run Analysis", type="primary", use_container_width=True)
    refresh_button = controls[4].button("Refresh", use_container_width=True)
    return uploaded_file, run_button, refresh_button, int(sample_size) if sample_size else None


def render_empty_state() -> None:
    st.markdown(
        f"""
        <div class="empty-state">
            <div>
                <div class="empty-illustration">{icon_svg("upload")}</div>
                <h2>No Excel Uploaded</h2>
                <p>Upload Excel to begin analysis.</p>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def metric_card(label: str, value: str, trend: str, icon: str, glow: str) -> None:
    st.markdown(
        f"""
        <div class="metric-card" style="--glow: {glow};">
            <div class="metric-top">
                <div class="metric-icon">{icon_svg(icon)}</div>
                <span class="badge-blue">Live</span>
            </div>
            <div class="metric-value">{escape(value)}</div>
            <div class="metric-label">{escape(label)}</div>
            <div class="metric-trend">{escape(trend)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_log(log_lines: list[str]) -> None:
    if not log_lines:
        return
    st.markdown('<div class="panel"><div class="panel-title"><h3>Run Log</h3><span>Pipeline events</span></div>', unsafe_allow_html=True)
    for line in log_lines:
        st.markdown(f'<div class="pipeline-step"><span class="dot"></span>{escape(line)}</div>', unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)


def account_status_df(result) -> pd.DataFrame:
    total = len(result.source_df)
    valid = len(result.valid_users)
    missing = int(result.source_df["GitHub_Username"].isna().sum()) if "GitHub_Username" in result.source_df.columns else 0
    invalid = max(total - valid - missing, 0)
    return pd.DataFrame(
        [
            {"Status": "Connected", "Count": valid},
            {"Status": "Invalid", "Count": invalid},
            {"Status": "Missing", "Count": missing},
        ]
    )


def chart_readability(chart: alt.Chart) -> alt.Chart:
    return (
        chart.configure_axis(
            labelColor="#A1A1AA",
            titleColor="#A1A1AA",
            labelFontSize=14,
            titleFontSize=15,
            labelFont="Inter",
            titleFont="Inter",
            gridColor="rgba(42, 42, 48, 0.55)",
            domainColor="#2A2A30",
            tickColor="#2A2A30",
        )
        .configure_legend(
            labelColor="#A1A1AA",
            titleColor="#FFFFFF",
            labelFontSize=14,
            titleFontSize=15,
            labelFont="Inter",
            titleFont="Inter",
            symbolSize=130,
        )
        .configure_view(strokeWidth=0)
    )


def render_donut_chart(data: pd.DataFrame) -> None:
    chart = (
        alt.Chart(data)
        .mark_arc(innerRadius=68, outerRadius=104, stroke="#18181B", strokeWidth=3)
        .encode(
            theta=alt.Theta("Count:Q"),
            color=alt.Color(
                "Status:N",
                scale=alt.Scale(domain=["Connected", "Invalid", "Missing"], range=[SUCCESS, DANGER, WARNING]),
                legend=alt.Legend(labelColor="#A1A1AA", titleColor="#FFFFFF", labelFontSize=14, titleFontSize=15),
            ),
            tooltip=["Status", "Count"],
        )
        .properties(height=260)
    )
    st.altair_chart(chart_readability(chart), use_container_width=True, theme=None)


def render_bar_chart(data: pd.DataFrame, x: str, y: str, color: str = ACCENT, height: int = 285) -> None:
    if data.empty:
        st.info("No data available.")
        return
    chart = (
        alt.Chart(data)
        .mark_bar(cornerRadiusTopLeft=6, cornerRadiusTopRight=6, color=color)
        .encode(
            x=alt.X(x, sort="-y", axis=alt.Axis(labelColor="#A1A1AA", titleColor="#A1A1AA", labelFontSize=14, titleFontSize=15)),
            y=alt.Y(y, axis=alt.Axis(labelColor="#A1A1AA", titleColor="#A1A1AA", labelFontSize=14, titleFontSize=15)),
            tooltip=list(data.columns),
        )
        .properties(height=max(height - 18, 180))
    )
    st.altair_chart(chart_readability(chart), use_container_width=True, theme=None)


def render_area_chart(data: pd.DataFrame, x: str, y: str, color: str = PURPLE) -> None:
    if data.empty:
        st.info("No data available.")
        return
    chart = (
        alt.Chart(data)
        .mark_area(
            line={"color": color, "strokeWidth": 2},
            color=alt.Gradient(
                gradient="linear",
                stops=[
                    alt.GradientStop(color=color, offset=0),
                    alt.GradientStop(color="rgba(139,92,246,0.05)", offset=1),
                ],
                x1=1,
                x2=1,
                y1=1,
                y2=0,
            ),
        )
        .encode(
            x=alt.X(x, axis=alt.Axis(labelColor="#A1A1AA", titleColor="#A1A1AA", labelFontSize=14, titleFontSize=15)),
            y=alt.Y(y, axis=alt.Axis(labelColor="#A1A1AA", titleColor="#A1A1AA", labelFontSize=14, titleFontSize=15)),
            tooltip=list(data.columns),
        )
        .properties(height=260)
    )
    st.altair_chart(chart_readability(chart), use_container_width=True, theme=None)


def render_heatmap(data: pd.DataFrame) -> None:
    if data.empty:
        st.info("No division data available.")
        return
    chart = (
        alt.Chart(data)
        .mark_rect(cornerRadius=4)
        .encode(
            x=alt.X("Batch:N", axis=alt.Axis(labelColor="#A1A1AA", titleColor="#A1A1AA", labelFontSize=14, titleFontSize=15)),
            y=alt.Y("Division:N", axis=alt.Axis(labelColor="#A1A1AA", titleColor="#A1A1AA", labelFontSize=14, titleFontSize=15)),
            color=alt.Color(
                "Repository_Count:Q",
                scale=alt.Scale(range=["#18181B", "#1D4ED8", "#22C55E"]),
                legend=alt.Legend(labelColor="#A1A1AA", titleColor="#FFFFFF", labelFontSize=14, titleFontSize=15),
            ),
            tooltip=["Division", "Batch", "Repository_Count"],
        )
        .properties(height=300)
    )
    st.altair_chart(chart_readability(chart), use_container_width=True, theme=None)


def render_pipeline_card(result) -> None:
    steps = [
        "Excel Uploaded",
        "Reading Spreadsheet",
        "Extracting GitHub Usernames",
        "Validating GitHub Accounts",
        "Fetching Repository Metadata",
        "Generating Analytics",
        "Dashboard Ready",
    ]
    st.markdown('<div class="panel"><div class="panel-title"><h3>Pipeline Status</h3><span>Completed workflow</span></div>', unsafe_allow_html=True)
    st.markdown('<div class="progress-shell"><div class="progress-bar"></div></div>', unsafe_allow_html=True)
    st.markdown('<div class="pipeline">', unsafe_allow_html=True)
    for step in steps:
        st.markdown(f'<div class="pipeline-step"><span class="dot"></span>{escape(step)}</div>', unsafe_allow_html=True)
    st.markdown("</div></div>", unsafe_allow_html=True)


def render_system_status(result, elapsed: float, last_analysis: str) -> None:
    st.markdown(
        f"""
        <div class="panel">
            <div class="panel-title"><h3>System Status</h3><span>Operational snapshot</span></div>
            <div class="system-grid">
                <div class="system-item"><div class="system-label">GitHub API</div><div class="system-value">Healthy</div></div>
                <div class="system-item"><div class="system-label">Excel</div><div class="system-value">Loaded</div></div>
                <div class="system-item"><div class="system-label">Analysis</div><div class="system-value">Completed</div></div>
                <div class="system-item"><div class="system-label">Rows Processed</div><div class="system-value">{len(result.source_df):,}</div></div>
                <div class="system-item"><div class="system-label">Repositories Found</div><div class="system-value">{len(result.repo_df):,}</div></div>
                <div class="system-item"><div class="system-label">Execution Time</div><div class="system-value">{elapsed:.1f}s</div></div>
                <div class="system-item"><div class="system-label">Last Updated</div><div class="system-value">{escape(last_analysis)}</div></div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_validation_summary_card(result) -> None:
    profiles_verified = len(result.valid_users)
    profiles_invalid = len(result.invalid_users)
    st.markdown(
        f"""
        <div class="panel">
            <div class="panel-title"><h3>Validation</h3><span>GitHub account audit</span></div>
            <div class="system-grid">
                <div class="system-item"><div class="system-label">Profiles Verified</div><div class="system-value">{profiles_verified:,}</div></div>
                <div class="system-item"><div class="system-label">Profiles Invalid</div><div class="system-value">{profiles_invalid:,}</div></div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if st.button("Open GitHub Links", use_container_width=True):
        st.session_state.sidebar_nav = "Verification"
        st.rerun()


def render_overview(result, elapsed: float, last_analysis: str) -> None:
    dashboard_df = result.dashboard_df
    repo_df = result.repo_df
    total_students = len(result.source_df)
    valid_accounts = len(result.valid_users)
    invalid_accounts = len(result.invalid_users)
    submission_rate = (valid_accounts / total_students * 100) if total_students else 0
    most_used_language = "Unknown"
    if not repo_df.empty:
        most_used_language = str(repo_df["Language"].fillna("Unknown").mode().iloc[0])

    st.markdown(
        """
        <div class="hero">
            <h1>Faculty Analytics Dashboard</h1>
            <p>Monitor GitHub-based student project submissions and repository insights.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    metric_cols = st.columns(4)
    with metric_cols[0]:
        metric_card("Total Students", format_number(total_students), "Rows processed from Excel", "users", "rgba(59,130,246,0.75)")
    with metric_cols[1]:
        metric_card("Valid GitHub Accounts", format_number(valid_accounts), "Validated against GitHub API", "check", "rgba(34,197,94,0.72)")
    with metric_cols[2]:
        metric_card("Invalid Accounts", format_number(invalid_accounts), "Requires faculty follow-up", "alert", "rgba(239,68,68,0.68)")
    with metric_cols[3]:
        metric_card("Submission %", f"{submission_rate:.1f}%", "Connected accounts over roster", "activity", "rgba(139,92,246,0.75)")

    metric_cols = st.columns(4)
    with metric_cols[0]:
        metric_card("Repositories Found", format_number(len(repo_df)), "Fetched repository metadata", "repo", "rgba(59,130,246,0.72)")
    with metric_cols[1]:
        avg_repos = dashboard_df["Repository_Count"].mean() if not dashboard_df.empty else 0
        metric_card("Average Repository Count", f"{avg_repos:.1f}", "Per validated student", "chart", "rgba(34,197,94,0.66)")
    with metric_cols[2]:
        avg_followers = dashboard_df["Followers"].mean() if not dashboard_df.empty else 0
        metric_card("Average Followers", f"{avg_followers:.1f}", "GitHub social signal", "users", "rgba(245,158,11,0.65)")
    with metric_cols[3]:
        metric_card("Most Used Language", most_used_language, "Mode across repositories", "spark", "rgba(139,92,246,0.70)")

    status_cols = st.columns([1.1, 1])
    with status_cols[0]:
        render_pipeline_card(result)
    with status_cols[1]:
        render_system_status(result, elapsed, last_analysis)
        render_validation_summary_card(result)

    chart_cols = st.columns([0.9, 1.3])
    with chart_cols[0]:
        st.markdown('<div class="panel"><div class="panel-title"><h3>GitHub Account Status</h3><span>Connected, invalid, missing</span></div>', unsafe_allow_html=True)
        render_donut_chart(account_status_df(result))
        st.markdown("</div>", unsafe_allow_html=True)
    with chart_cols[1]:
        st.markdown('<div class="panel"><div class="panel-title"><h3>Programming Language Distribution</h3><span>Top 10 languages</span></div>', unsafe_allow_html=True)
        language_counts = repo_df["Language"].fillna("Unknown").value_counts().head(10).reset_index() if not repo_df.empty else pd.DataFrame()
        if not language_counts.empty:
            language_counts.columns = ["Language", "Repositories"]
        render_bar_chart(language_counts, "Language:N", "Repositories:Q", ACCENT)
        st.markdown("</div>", unsafe_allow_html=True)

    chart_cols = st.columns(2)
    repo_distribution = pd.DataFrame()
    followers_distribution = pd.DataFrame()
    if not dashboard_df.empty:
        repo_distribution = dashboard_df["Repository_Count"].value_counts().sort_index().reset_index()
        repo_distribution.columns = ["Repository Count", "Students"]
        followers_distribution = dashboard_df["Followers"].value_counts().sort_index().reset_index()
        followers_distribution.columns = ["Followers", "Students"]
    with chart_cols[0]:
        st.markdown('<div class="panel"><div class="panel-title"><h3>Repository Count Distribution</h3><span>Students by repo count</span></div>', unsafe_allow_html=True)
        render_area_chart(repo_distribution, "Repository Count:Q", "Students:Q", ACCENT)
        st.markdown("</div>", unsafe_allow_html=True)
    with chart_cols[1]:
        st.markdown('<div class="panel"><div class="panel-title"><h3>Followers Distribution</h3><span>Students by follower count</span></div>', unsafe_allow_html=True)
        render_area_chart(followers_distribution, "Followers:Q", "Students:Q", PURPLE)
        st.markdown("</div>", unsafe_allow_html=True)

    heatmap = pd.DataFrame()
    if not dashboard_df.empty:
        heatmap = (
            dashboard_df.groupby(["Division", "Batch"], dropna=False)["Repository_Count"]
            .sum()
            .reset_index()
        )
    st.markdown('<div class="panel"><div class="panel-title"><h3>Division-wise Repository Statistics</h3><span>Repository totals by academic group</span></div>', unsafe_allow_html=True)
    render_heatmap(heatmap)
    st.markdown("</div>", unsafe_allow_html=True)


def render_student_profile(student: pd.Series, repo_df: pd.DataFrame) -> None:
    username = student.get("GitHub_Username", "")
    repos = repo_df[repo_df["Username"] == username].copy() if not repo_df.empty else pd.DataFrame()
    avatar = escape(student.get("Avatar_URL", ""))
    name = escape(student.get("Student Name", ""))
    profile_url = student.get("Profile_URL", "") or github_profile_url(username)
    st.markdown(
        f"""
        <div class="profile-panel">
            <div class="profile-header">
                <img src="{avatar}" alt="">
                <div>
                    <div class="profile-name">{name}</div>
                    <div class="profile-sub">{escape(student.get("Division", ""))} / {escape(student.get("Batch", ""))}</div>
                    <div style="margin-top: 8px;"><span class="badge-blue">{escape(username)}</span></div>
                </div>
            </div>
            <div class="system-grid">
                <div class="system-item"><div class="system-label">Followers</div><div class="system-value">{format_number(student.get("Followers", 0))}</div></div>
                <div class="system-item"><div class="system-label">Following</div><div class="system-value">{format_number(student.get("Following", 0))}</div></div>
                <div class="system-item"><div class="system-label">Repositories</div><div class="system-value">{format_number(student.get("Repository_Count", 0))}</div></div>
                <div class="system-item"><div class="system-label">Primary Language</div><div class="system-value">{escape(student.get("Primary_Language", "Unknown"))}</div></div>
            </div>
            <div style="margin-top: 14px;">
                <div class="system-label" style="margin-bottom: 8px;">GitHub Profile</div>
                {github_button(profile_url, "Open GitHub Profile")}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown('<div class="panel"><div class="panel-title"><h3>Repository Summary</h3><span>Recent repositories</span></div>', unsafe_allow_html=True)
    if repos.empty:
        st.info("No repositories found for this student.")
    else:
        languages = repos["Language"].fillna("Unknown").value_counts().head(5).reset_index()
        languages.columns = ["Language", "Repositories"]
        render_bar_chart(languages, "Language:N", "Repositories:Q", SUCCESS, 180)
        for _, repo in repos.sort_values("Updated", ascending=False).head(5).iterrows():
            st.markdown(
                f"""
                <div class="repo-card">
                    <div class="repo-name">{escape(repo.get("Repository", ""))}</div>
                    <span class="badge-purple">{escape(repo.get("Language", "Unknown"))}</span>
                    <div class="repo-meta">
                        <span>Stars {format_number(repo.get("Stars", 0))}</span>
                        <span>Forks {format_number(repo.get("Forks", 0))}</span>
                        <span>Updated {escape(repo.get("Updated", ""))}</span>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
    st.markdown("</div>", unsafe_allow_html=True)


def render_students(result) -> None:
    df = result.dashboard_df.copy()
    if df.empty:
        render_empty_state()
        return

    st.markdown('<div class="hero"><h1>Student Explorer</h1><p>Search, filter, and inspect validated GitHub student profiles.</p></div>', unsafe_allow_html=True)
    controls = st.columns([2, 1, 1, 1])
    query = controls[0].text_input("Search", placeholder="Name, username, language")
    division = controls[1].selectbox("Division", ["All"] + sorted(df["Division"].dropna().astype(str).unique()))
    batch = controls[2].selectbox("Batch", ["All"] + sorted(df["Batch"].dropna().astype(str).unique()))
    page_size = controls[3].selectbox("Rows", [15, 25, 50, 100], index=1)

    filtered = filter_text(df, query, ["Student Name", "GitHub_Username", "Primary_Language"])
    filtered = apply_value_filter(filtered, "Division", division)
    filtered = apply_value_filter(filtered, "Batch", batch)
    filtered["Status"] = "Connected"
    filtered["GitHub Profile"] = filtered["GitHub_Username"].apply(github_profile_url)
    selected_student = filtered.iloc[0] if not filtered.empty else None

    left, right = st.columns([1.55, 0.85])
    with left:
        st.markdown('<div class="panel"><div class="panel-title"><h3>Students</h3><span>Validated GitHub accounts</span></div>', unsafe_allow_html=True)
        display_cols = [
            "Avatar_URL",
            "Student Name",
            "Division",
            "Batch",
            "GitHub_Username",
            "GitHub Profile",
            "Followers",
            "Following",
            "Public_Repos",
            "Repository_Count",
            "Primary_Language",
            "Status",
            "Profile_URL",
        ]
        display_df = filtered[display_cols].head(page_size).reset_index(drop=True)
        table_event = st.dataframe(
            display_df,
            use_container_width=True,
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            column_config={
                "Avatar_URL": st.column_config.ImageColumn("Avatar", width="small"),
                "GitHub Profile": st.column_config.LinkColumn("GitHub Profile", display_text="↗ Open Profile"),
                "Profile_URL": st.column_config.LinkColumn("GitHub"),
            },
        )
        if table_event.selection.rows:
            selected_student = filtered.head(page_size).reset_index(drop=True).iloc[table_event.selection.rows[0]]
        export_cols = [column for column in display_cols if column != "Avatar_URL"]
        export_a, export_b = st.columns(2)
        export_a.download_button("Export CSV", filtered[export_cols].to_csv(index=False).encode("utf-8"), "github_students.csv", "text/csv")
        export_b.download_button("Export Excel", dataframe_to_excel(filtered[export_cols]), "github_students.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        st.markdown("</div>", unsafe_allow_html=True)
    with right:
        student_names = filtered["Student Name"].fillna("Unknown").astype(str).tolist()
        if student_names:
            selected = st.selectbox("Open student profile", student_names)
            if not table_event.selection.rows:
                selected_student = filtered[filtered["Student Name"].astype(str) == selected].iloc[0]
        if selected_student is not None:
            render_student_profile(selected_student, result.repo_df)


def render_repositories(result) -> None:
    repo_df = result.repo_df.copy()
    st.markdown('<div class="hero"><h1>Repository Explorer</h1><p>Browse repository metadata collected from validated GitHub profiles.</p></div>', unsafe_allow_html=True)
    if repo_df.empty:
        render_empty_state()
        return
    repo_df["Language"] = repo_df["Language"].fillna("Unknown")
    controls = st.columns([2, 1, 1])
    query = controls[0].text_input("Search repositories", placeholder="Repository name or owner")
    language = controls[1].selectbox("Language", ["All"] + sorted(repo_df["Language"].dropna().astype(str).unique()))
    view = controls[2].selectbox("View", ["Cards", "Table"])
    filtered = filter_text(repo_df, query, ["Username", "Repository", "Language"])
    filtered = apply_value_filter(filtered, "Language", language)
    filtered["Repository URL"] = filtered["Repository_URL"]

    if view == "Table":
        st.markdown('<div class="panel"><div class="panel-title"><h3>Repositories</h3><span>Sortable metadata table</span></div>', unsafe_allow_html=True)
        st.dataframe(
            filtered[["Repository", "Username", "Language", "Stars", "Forks", "Created", "Updated", "Repository URL"]],
            use_container_width=True,
            hide_index=True,
            column_config={"Repository URL": st.column_config.LinkColumn("Repository URL", display_text="↗ Open Repository")},
        )
        st.markdown("</div>", unsafe_allow_html=True)
        return

    cols = st.columns(3)
    for index, (_, repo) in enumerate(filtered.head(30).iterrows()):
        with cols[index % 3]:
            st.markdown(
                f"""
                <div class="repo-card">
                    <div class="repo-name">{escape(repo.get("Repository", ""))}</div>
                    <span class="badge-blue">{escape(repo.get("Username", ""))}</span>
                    <span class="badge-purple">{escape(repo.get("Language", "Unknown"))}</span>
                    <div class="repo-meta">
                        <span>Stars {format_number(repo.get("Stars", 0))}</span>
                        <span>Forks {format_number(repo.get("Forks", 0))}</span>
                        <span>Updated {escape(repo.get("Updated", ""))}</span>
                    </div>
                    <div style="margin-top: 12px;">{github_button(repo.get("Repository_URL", ""), "Open Repository")}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )


def leaderboard_card(rank: int, title: str, score: str, badge: str) -> None:
    st.markdown(
        f"""
        <div class="leader-card">
            <div class="leader-row">
                <div class="leader-rank">{rank}</div>
                <div class="leader-title">{escape(title)}<br><span class="badge-blue">{escape(badge)}</span></div>
                <div class="leader-score">{escape(score)}</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_leaderboards(result) -> None:
    dashboard_df = result.dashboard_df
    repo_df = result.repo_df
    st.markdown('<div class="hero"><h1>Leaderboards</h1><p>Identify top contributors and dominant technology patterns.</p></div>', unsafe_allow_html=True)
    if dashboard_df.empty:
        render_empty_state()
        return
    cols = st.columns(4)
    sections = [
        ("Top Repository Owners", dashboard_df.sort_values("Repository_Count", ascending=False).head(10), "Repository_Count"),
        ("Top Followers", dashboard_df.sort_values("Followers", ascending=False).head(10), "Followers"),
        ("Top Public Repos", dashboard_df.sort_values("Public_Repos", ascending=False).head(10), "Public_Repos"),
    ]
    for col, (title, data, score_col) in zip(cols[:3], sections):
        with col:
            st.markdown(f'<div class="panel"><div class="panel-title"><h3>{escape(title)}</h3><span>Top 10</span></div>', unsafe_allow_html=True)
            for rank, (_, row) in enumerate(data.iterrows(), start=1):
                leaderboard_card(rank, row.get("Student Name", "Unknown"), format_number(row.get(score_col, 0)), row.get("GitHub_Username", ""))
            st.markdown("</div>", unsafe_allow_html=True)
    with cols[3]:
        st.markdown('<div class="panel"><div class="panel-title"><h3>Top Languages</h3><span>Repository frequency</span></div>', unsafe_allow_html=True)
        if repo_df.empty:
            st.info("No language data available.")
        else:
            languages = repo_df["Language"].fillna("Unknown").value_counts().head(10)
            for rank, (language, count) in enumerate(languages.items(), start=1):
                leaderboard_card(rank, language, format_number(count), "Language")
        st.markdown("</div>", unsafe_allow_html=True)


def render_issues(result) -> None:
    issues = result.invalid_issues_df.copy()
    st.markdown('<div class="hero"><h1>Follow-up Queue</h1><p>Students requiring attention for invalid, missing, or failed GitHub submissions.</p></div>', unsafe_allow_html=True)
    if issues.empty:
        st.markdown(
            '<div class="empty-state"><div><div class="empty-illustration">' + icon_svg("check") + '</div><h2>No Follow-up Required</h2><p>All visible submissions passed validation.</p></div></div>',
            unsafe_allow_html=True,
        )
        return
    issue_filter = st.selectbox("Issue type", ["All"] + sorted(issues["Issue"].dropna().astype(str).unique()))
    filtered = apply_value_filter(issues, "Issue", issue_filter)
    filtered["Action"] = "Faculty follow-up"
    st.markdown('<div class="panel"><div class="panel-title"><h3>Students Requiring Attention</h3><span>Warning badges and action queue</span></div>', unsafe_allow_html=True)
    st.dataframe(filtered, hide_index=True, use_container_width=True)
    st.download_button("Export Issues CSV", filtered.to_csv(index=False).encode("utf-8"), "github_invalid_issues.csv", "text/csv")
    st.markdown("</div>", unsafe_allow_html=True)


def build_validation_audit_df(result, last_analysis: str) -> pd.DataFrame:
    source = result.source_df.copy()
    stats_cols = [
        "GitHub_Username",
        "Repository_Count",
        "Followers",
        "Following",
    ]
    stats = result.dashboard_df[stats_cols].copy() if not result.dashboard_df.empty else pd.DataFrame(columns=stats_cols)
    audit = source[["Student Name", "Division", "GitHub_Username"]].merge(
        stats,
        on="GitHub_Username",
        how="left",
    )
    valid_users = set(result.valid_users)

    def validation_status(username) -> str:
        if pd.isna(username) or not str(username).strip():
            return "Missing"
        if username in valid_users:
            return "Verified"
        return "Invalid"

    audit["GitHub Profile"] = audit["GitHub_Username"].apply(github_profile_url)
    audit["Validation Status"] = audit["GitHub_Username"].apply(validation_status)
    audit["Repositories Found"] = audit["Repository_Count"].fillna(0).astype(int)
    audit["Followers"] = audit["Followers"].fillna(0).astype(int)
    audit["Following"] = audit["Following"].fillna(0).astype(int)
    audit["Last Updated"] = last_analysis
    return audit[
        [
            "Student Name",
            "Division",
            "GitHub_Username",
            "GitHub Profile",
            "Validation Status",
            "Repositories Found",
            "Followers",
            "Following",
            "Last Updated",
        ]
    ]


def render_verification(result, last_analysis: str) -> None:
    st.markdown(
        '<div class="hero"><h1>Verification</h1><p>Audit the exact GitHub profiles used during analysis.</p></div>',
        unsafe_allow_html=True,
    )
    audit = build_validation_audit_df(result, last_analysis)
    controls = st.columns([2, 1, 1])
    query = controls[0].text_input("Search verification records", placeholder="Student name or GitHub username")
    status = controls[1].selectbox("Validation Status", ["All"] + sorted(audit["Validation Status"].dropna().unique()))
    page_size = controls[2].selectbox("Rows", [25, 50, 100, 250], index=1)
    filtered = filter_text(audit, query, ["Student Name", "GitHub_Username", "Division"])
    filtered = apply_value_filter(filtered, "Validation Status", status)

    st.markdown(
        '<div class="panel"><div class="panel-title"><h3>Profile Verification Audit</h3><span>Faculty review table</span></div>',
        unsafe_allow_html=True,
    )
    st.dataframe(
        filtered.head(page_size),
        use_container_width=True,
        hide_index=True,
        column_config={
            "GitHub_Username": st.column_config.TextColumn("GitHub Username"),
            "GitHub Profile": st.column_config.LinkColumn("GitHub Profile", display_text="✔ Open Profile"),
        },
    )
    st.download_button(
        "Export Verification CSV",
        filtered.to_csv(index=False).encode("utf-8"),
        "github_profile_verification.csv",
        "text/csv",
    )
    st.markdown("</div>", unsafe_allow_html=True)


def render_settings(token_present: bool, file_hash: str | None) -> None:
    st.markdown('<div class="hero"><h1>Settings</h1><p>Runtime configuration for the internal analytics platform.</p></div>', unsafe_allow_html=True)
    st.markdown(
        f"""
        <div class="panel">
            <div class="panel-title"><h3>Application Status</h3><span>Current runtime</span></div>
            <div class="system-grid">
                <div class="system-item"><div class="system-label">GitHub Token</div><div class="system-value">{"Available" if token_present else "Missing"}</div></div>
                <div class="system-item"><div class="system-label">Faculty Mode</div><div class="system-value">Enabled</div></div>
                <div class="system-item"><div class="system-label">Uploaded File Hash</div><div class="system-value">{escape(file_hash[:12] if file_hash else "None")}</div></div>
                <div class="system-item"><div class="system-label">Theme</div><div class="system-value">Dark Enterprise</div></div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


inject_theme()

token = get_token()
token_present = bool(token)
page = render_sidebar(token_present)

if "analysis_result" not in st.session_state:
    st.session_state.analysis_result = None
    st.session_state.analysis_elapsed = 0.0
    st.session_state.analysis_file_hash = None
    st.session_state.last_analysis_time = "Never"

uploaded_file, run_button, refresh_button, sample_size = render_topbar(
    st.session_state.last_analysis_time,
    token_present,
)

if refresh_button:
    st.rerun()

file_bytes = uploaded_file.getvalue() if uploaded_file is not None else None
file_hash = hashlib.sha256(file_bytes).hexdigest() if file_bytes else None

if file_hash and st.session_state.analysis_file_hash not in (None, file_hash):
    st.session_state.analysis_result = None
    st.session_state.analysis_elapsed = 0.0
    st.session_state.analysis_file_hash = None
    st.session_state.last_analysis_time = "Never"

if run_button:
    if uploaded_file is None:
        st.warning("Upload Excel to begin analysis.")
    else:
        progress_bar = st.progress(0)
        status_text = st.empty()
        run_log: list[str] = []
        start = time.perf_counter()

        def progress_callback(stage: str, index: int, total: int, username: str) -> None:
            if total <= 0:
                return
            if stage == "validate":
                fraction = index / total * 0.55
                label = f"Validating accounts ({index}/{total}): {username}"
            else:
                fraction = 0.55 + (index / total * 0.4)
                label = f"Fetching repositories ({index}/{total}): {username}"
            progress_bar.progress(min(fraction, 0.98))
            status_text.write(label)

        try:
            result = run_analysis(
                io.BytesIO(file_bytes),
                token,
                sample_size,
                progress_callback,
            )
            elapsed = time.perf_counter() - start
            progress_bar.progress(1.0)
            status_text.write("Dashboard Ready")
            run_log = result.log
            st.session_state.analysis_result = result
            st.session_state.analysis_elapsed = elapsed
            st.session_state.analysis_file_hash = file_hash
            st.session_state.last_analysis_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        except RateLimitError as exc:
            reset_text = "later"
            if exc.reset_epoch:
                try:
                    reset_text = datetime.fromtimestamp(int(exc.reset_epoch)).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    reset_text = "later"
            st.error(f"GitHub API rate limit reached. Try again at {reset_text}.")
        except Exception as exc:
            st.error(str(exc))
        render_log(run_log)

result = st.session_state.analysis_result

if result is None:
    render_empty_state()
else:
    if page == "Overview":
        render_overview(result, st.session_state.analysis_elapsed, st.session_state.last_analysis_time)
    elif page == "Students":
        render_students(result)
    elif page == "Repositories":
        render_repositories(result)
    elif page == "Leaderboards":
        render_leaderboards(result)
    elif page == "Issues":
        render_issues(result)
    elif page == "Verification":
        render_verification(result, st.session_state.last_analysis_time)
    else:
        render_settings(token_present, st.session_state.analysis_file_hash)
