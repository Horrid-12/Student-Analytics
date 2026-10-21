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


def linkedin_profile_url(username) -> str:
    if pd.isna(username) or not str(username).strip():
        return ""
    return f"https://www.linkedin.com/in/{str(username).strip().strip('/')}"


def hackerrank_profile_url(username) -> str:
    if pd.isna(username) or not str(username).strip():
        return ""
    return f"https://www.hackerrank.com/profile/{str(username).strip().lstrip('@')}"


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


def apply_value_filter(df: pd.DataFrame, column: str, value) -> pd.DataFrame:
    if column not in df.columns:
        return df
    if value is None:
        return df
    # Multi-select: list/tuple/set of values (empty or containing "All" = no filter).
    if isinstance(value, (list, tuple, set)):
        vals: list[str] = []
        for item in value:
            for part in str(item or "").split(","):
                text = part.strip()
                if text and text.lower() != "all":
                    vals.append(text)
        if not vals:
            return df
        return df[df[column].astype(str).isin(vals)]
    text = str(value or "").strip()
    if text == "" or text.lower() == "all":
        return df
    # Comma-joined multi value passed as a single string ("1,2").
    if "," in text:
        vals = [p.strip() for p in text.split(",") if p.strip() and p.strip().lower() != "all"]
        if not vals:
            return df
        return df[df[column].astype(str).isin(vals)]
    return df[df[column].astype(str) == text]


def apply_pair_filter(df: pd.DataFrame, pairs) -> pd.DataFrame:
    """Restrict ``df`` to an explicit set of (Division, Batch) pairs.

    ``pairs`` is an iterable of ``"DIV|BATCH"`` strings (repeated
    ``?pair=`` params or comma-joined) or ``(division, batch)`` tuples.
    Empty / None / ["All"] means no filtering. A row is kept when its
    (Division, Batch) matches ANY pair (OR of pairs — the same primitive
    the Compare picker and faculty teaching scope use).
    """
    if pairs is None:
        return df
    if isinstance(pairs, str):
        pairs = [pairs]
    wanted: list[tuple[str, str]] = []
    for item in pairs:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            div, bat = str(item[0] or "").strip(), str(item[1] or "").strip()
            if div and bat and div.lower() != "all" and bat.lower() != "all":
                wanted.append((div, bat))
            continue
        for part in str(item or "").split(","):
            text = part.strip()
            if not text or text.lower() == "all" or "|" not in text:
                continue
            div, _, bat = text.partition("|")
            div, bat = div.strip(), bat.strip()
            if div and bat:
                wanted.append((div, bat))
    if not wanted or "Division" not in df.columns or "Batch" not in df.columns:
        return df
    try:
        divs = df["Division"].astype(str)
        bats = df["Batch"].astype(str)
        mask = pd.Series(False, index=df.index)
        for div, bat in wanted:
            mask = mask | (divs.eq(div) & bats.eq(bat))
        return df[mask]
    except Exception:
        return df


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
