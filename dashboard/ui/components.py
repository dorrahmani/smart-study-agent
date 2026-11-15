"""
dashboard/ui/components.py — small HTML building blocks, styled by theme.py.

All helpers escape their text arguments (Hebrew titles can contain quotes,
<, & ...). Arguments whose name ends in `_html` are trusted markup.
HTML is emitted on a single line: indented multi-line HTML is treated as a
markdown code block by Streamlit.
"""
from __future__ import annotations

import html as _html
from typing import Iterable

import streamlit as st


def esc(s) -> str:
    return _html.escape("" if s is None else str(s), quote=True)


def html(markup: str) -> None:
    st.markdown(markup, unsafe_allow_html=True)


# ── headers ──────────────────────────────────────────────────────────────────

def page_header(title: str, subtitle: str | None = None, icon: str | None = None,
                eyebrow: str | None = None, right_html: str = "") -> None:
    icon_html = f'<div class="ssa-ph-icon" aria-hidden="true">{esc(icon)}</div>' if icon else ""
    eb = f'<div class="ssa-eyebrow">{esc(eyebrow)}</div>' if eyebrow else ""
    sub = f'<div class="ssa-ph-sub">{esc(subtitle)}</div>' if subtitle else ""
    html(
        f'<div class="ssa-ph"><div class="ssa-ph-main">{icon_html}<div>{eb}'
        f'<div class="ssa-ph-title" role="heading" aria-level="1">{esc(title)}</div>{sub}</div></div>'
        f'<div>{right_html}</div></div>'
    )


def section(title: str, subtitle: str | None = None, icon: str | None = None,
            right_html: str = "") -> None:
    ic = f'<span aria-hidden="true">{esc(icon)}</span>' if icon else ""
    sub = f'<span class="ssa-sec-sub">{esc(subtitle)}</span>' if subtitle else ""
    html(
        f'<div class="ssa-sec"><div class="ssa-sec-title" role="heading" aria-level="2">{ic}{esc(title)}</div>'
        f'<div>{sub}{right_html}</div></div>'
    )


# ── atoms ────────────────────────────────────────────────────────────────────

def pill(text: str, tone: str = "", icon: str | None = None, title: str | None = None) -> str:
    ic = f'<span aria-hidden="true">{esc(icon)}</span>' if icon else ""
    t = f' title="{esc(title)}"' if title else ""
    return f'<span class="ssa-pill {esc(tone)}"{t}>{ic}{esc(text)}</span>'


def dot(tone: str = "") -> str:
    return f'<span class="ssa-dot {esc(tone)}" aria-hidden="true"></span>'


def meter(frac: float, tone: str = "", tick: float | None = None, label: str = "") -> str:
    pct = max(0.0, min(1.0, float(frac or 0))) * 100
    tk = ""
    if tick is not None:
        # RTL: bar fills from the right, so the tick is measured from the right
        tk = f'<i class="ssa-tick" style="right:{max(0, min(100, tick * 100)):.1f}%"></i>'
    aria = f' role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="{pct:.0f}" aria-label="{esc(label)}"'
    return f'<div class="ssa-meter {esc(tone)}"{aria}><span style="width:{pct:.1f}%"></span>{tk}</div>'


def stat_card(label: str, value: str, sub: str | None = None, tone: str = "",
              icon: str | None = None, extra_html: str = "", value_html: str | None = None) -> str:
    ic = f'<div class="ssa-stat-icon" aria-hidden="true">{esc(icon)}</div>' if icon else ""
    sb = f'<div class="ssa-stat-sub">{esc(sub)}</div>' if sub else ""
    val = value_html if value_html is not None else esc(value)
    return (
        f'<div class="ssa-card ssa-stat ssa-tone-{esc(tone or "neutral")}">'
        f'<div class="ssa-stat-top"><div class="ssa-stat-label">{esc(label)}</div>{ic}</div>'
        f'<div class="ssa-stat-value">{val}</div>{extra_html}{sb}</div>'
    )


def grid(cards: Iterable[str], min_width: int = 220) -> None:
    html(f'<div class="ssa-grid" style="--min:{int(min_width)}px">{"".join(cards)}</div>')


def empty_state(icon: str, title: str, text: str = "") -> None:
    tx = f'<div class="ssa-empty-text">{esc(text)}</div>' if text else ""
    html(
        f'<div class="ssa-empty"><div class="ssa-empty-icon" aria-hidden="true">{esc(icon)}</div>'
        f'<div class="ssa-empty-title">{esc(title)}</div>{tx}</div>'
    )


def callout(body_html: str, tone: str = "info", icon: str | None = None) -> None:
    ic = f'<div class="ssa-callout-icon" aria-hidden="true">{esc(icon)}</div>' if icon else ""
    html(f'<div class="ssa-callout {esc(tone)}">{ic}<div>{body_html}</div></div>')


def list_block(rows_html: Iterable[str], head_html: str = "") -> None:
    head = f'<div class="ssa-list-head">{head_html}</div>' if head_html else ""
    html(f'<div class="ssa-list">{head}{"".join(rows_html)}</div>')


def row(title: str, sub: str = "", date: str = "", lead_html: str = "", end_html: str = "") -> str:
    d = f'<span class="ssa-row-date">{esc(date)}</span>' if date else ""
    sb = f'<div class="ssa-row-sub">{esc(sub)}</div>' if sub else ""
    return (
        f'<div class="ssa-row">{lead_html}{d}<div class="ssa-row-main">'
        f'<div class="ssa-row-title" title="{esc(title)}">{esc(title)}</div>{sb}</div>'
        f'<div class="ssa-row-end">{end_html}</div></div>'
    )


def score_card(score: str, summary: str = "", footer: str = "", good: bool = False,
               prefix: str = "") -> None:
    color = "var(--success)" if good else "var(--danger)"
    ft = f'<div class="ssa-stat-sub" style="margin-top:8px">{esc(footer)}</div>' if footer else ""
    html(
        f'<div class="ssa-score"><div class="ssa-score-value" style="color:{color}">{esc(prefix)}{esc(score)}</div>'
        f'<div class="ssa-score-sub">{esc(summary)}</div>{ft}</div>'
    )


BRAND_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M22 10 12 5 2 10l10 5 10-5Z"/><path d="M6 12v5c3 2 9 2 12 0v-5"/>'
    '<path d="M22 10v6"/></svg>'
)
