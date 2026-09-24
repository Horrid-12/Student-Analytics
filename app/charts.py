"""Plotly figure builders — mirrors the legacy Altair chart specs
(app.py render_*_chart) with the site palette (static/theme.css), fonts and
readable axis labels. Each returns ``fig.to_dict()`` for Jinja ``| tojson``
embedding.

Trace colours below mirror the LIGHT theme tokens (the HTML default); the
charts.html macro remaps them per active theme at render time.  The primary
ACCENT is the sidebar-active red (#BE342B) in light mode, swapped to the
bright blue (#5B9CF6) in dark mode by the JS macro.
"""

import plotly.graph_objects as go

# light-theme site tokens (static/theme.css [data-theme="light"])
# ACCENT is the hero colour — red in light mode (sidebar active), remapped to
# blue (#5B9CF6) in dark mode by the charts.html JS macro.
ACCENT    = "#BE342B"  # --red   (light-mode sidebar active accent)
SECONDARY = "#60738A"  # --blue  (light-mode button / secondary accent)
SUCCESS   = "#3d7a7a"  # --green (dusty teal)
WARNING   = "#A66B25"  # --amber
PURPLE    = "#9AAABD"  # --purple-slate
MUTED     = "#566271"  # --muted

DONUT_COLORS = [ACCENT, SECONDARY, SUCCESS, WARNING, PURPLE, MUTED]

_AXIS = dict(
    # BUG-086: neutral placeholders — theme-aware values injected by charts.html macro at render time.
    # Use rgba(0,0,0,0) instead of "transparent" — Plotly's Python validator rejects the CSS keyword.
    gridcolor="rgba(0,0,0,0)",
    color="rgba(0,0,0,0)",
    tickfont=dict(color="rgba(0,0,0,0)", size=13, family="Inter"),
)


def _layout(fig, title, height):
    fig.update_layout(
        title=title,
        height=height,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="rgba(0,0,0,0)", family="Inter"),
        margin=dict(l=10, r=10, t=40 if title else 8, b=10),
        showlegend=False,
        # BUG-086: hoverlabel colours injected by JS — placeholder keeps structure valid.
        hoverlabel=dict(bgcolor="rgba(0,0,0,0)", bordercolor="rgba(0,0,0,0)", font=dict(color="rgba(0,0,0,0)")),
    )
    return fig


def donut(labels, values, title=None, colors=None):
    """Shared pie/donut matching the legacy opacity/legend styling."""
    values = list(values)
    fig = go.Figure(
        go.Pie(
            labels=list(labels),
            values=values,
            hole=0.62,
            sort=False,
            marker=dict(colors=colors or DONUT_COLORS[: len(values)]),
            textinfo="label+percent",
            textfont=dict(color="#A1A1AA", size=12),
            hovertemplate="%{label}: %{value}<extra></extra>",
        )
    )
    fig.update_layout(
        title=title,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#A1A1AA", family="Inter"),
        margin=dict(l=10, r=10, t=40 if title else 12, b=10),
        showlegend=True,
        height=260,
        hoverlabel=dict(bgcolor="rgba(0,0,0,0)", bordercolor="rgba(0,0,0,0)", font=dict(color="rgba(0,0,0,0)")),
        legend=dict(orientation="h", yanchor="bottom", y=-0.12),
    )
    return fig.to_dict()


def bar(labels, values, color=ACCENT, height=285, title=None):
    """Horizontal-first bar chart (x sorted descending, like legacy sort='-y')."""
    fig = go.Figure(
        go.Bar(
            y=list(labels),
            x=list(values),
            orientation="h",
            marker=dict(color=color, cornerradius=6),
            text=list(values),
            textposition="outside",
            cliponaxis=False,
            hovertemplate="%{y}: %{x}<extra></extra>",
        )
    )
    fig.update_xaxes(**_AXIS, zeroline=False)
    fig.update_yaxes(**_AXIS, categoryorder="total ascending")
    _layout(fig, title, max(height - 18, 180))
    fig.update_layout(margin=dict(l=10, r=70, t=40 if title else 8, b=10))
    return fig.to_dict()


def area(x, y, color=ACCENT, height=260, title=None):
    """Distribution chart — gradient-to-transparent area (legacy mark_area)."""
    # Accent tint uses the --red rgba (190,52,43) light / remapped to (91,156,246)
    # dark by the JS macro (charts.html rewrites the rgb triple for the active
    # theme). SECONDARY keeps the blue-grey tint; purple stays the same.
    fills = {
        ACCENT: "rgba(190, 52, 43, 0.15)",
        SECONDARY: "rgba(96, 115, 138, 0.15)",
        PURPLE: "rgba(154, 170, 189, 0.08)",
    }
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=list(x),
            y=list(y),
            mode="lines",
            line=dict(color=color, width=2),
            fill="tozeroy",
            fillcolor=fills.get(color, "rgba(190, 52, 43, 0.15)"),
            hovertemplate="%{x}: %{y}<extra></extra>",
        )
    )
    fig.update_xaxes(**_AXIS, zeroline=False)
    fig.update_yaxes(**_AXIS, zeroline=False)
    return _layout(fig, title, height).to_dict()


def heatmap(batches, divisions, values, title=None):
    """Division × Batch repo-count heatmap (site palette, remapped per theme)."""
    fig = go.Figure(
        go.Heatmap(
            x=list(batches),
            y=list(divisions),
            z=list(values),
            colorscale=[
                [0.0, "#f5f1e9"],
                [0.5, "#BE342B"],
                [1.0, "#21845D"],
            ],
            hovertemplate="%{y} · Batch %{x}: %{z} repos<extra></extra>",
        )
    )
    fig.update_xaxes(**_AXIS)
    fig.update_yaxes(**_AXIS)
    return _layout(fig, title, 300).to_dict()


def line(x, series, title=None, height=260, colors=None):
    """Multi-series time line (history trends: Valid Accounts / Active Repos)."""
    fig = go.Figure()
    palette = colors or [ACCENT, SUCCESS]
    for index, (name, values) in enumerate(series):
        fig.add_trace(
            go.Scatter(
                x=list(x),
                y=list(values),
                mode="lines+markers",
                name=name,
                line=dict(color=palette[index % len(palette)], width=2),
                marker=dict(size=7),
                hovertemplate="%{x}: %{y} %{fullData.name}<extra></extra>",
            )
        )
    fig.update_xaxes(**_AXIS, zeroline=False)
    fig.update_yaxes(**_AXIS, zeroline=False, title=None)
    _layout(fig, title, height)
    fig.update_layout(
        showlegend=True,
        legend=dict(orientation="h", yanchor="bottom", y=-0.2),
    )
    return fig.to_dict()
