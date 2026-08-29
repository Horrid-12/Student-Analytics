"""Small, framework-light helpers shared by Streamlit views.

Ported verbatim from the frozen repo-root ``ui_helpers.py`` for the FastAPI
stack (no Streamlit dependency, pandas only).
"""

import html
import io

import pandas as pd


def escape(value) -> str:
    if pd.isna(value):
        return ""
    return html.escape(str(value))


def github_profile_url(username) -> str:
    if pd.isna(username) or not str(username).strip():
        return ""
    return f"https://github.com/{str(username).strip()}"


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
            mask |= df[column].astype(str).str.contains(
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