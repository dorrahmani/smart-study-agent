"""System health ("🩺 מערכת") — one read-only table: crons, NotebookLM, Drive, WhatsApp, budget, deploy, security,
backup tests, bot latency. Green / yellow / red / grey per row + when the evidence was written.
Reads files only (utils/system_health.py): no LLM, no network, no secrets. Cached for 60 s so the tab opens fast."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import streamlit as st

from dashboard import core
from dashboard.ui.components import esc
from utils import system_health as sh

_DOT = {sh.OK: '🟢', sh.WARN: '🟡', sh.FAIL: '🔴', sh.NA: '⚪'}
_IL = ZoneInfo('Asia/Jerusalem')


@st.cache_data(ttl=60, show_spinner=False)
def _rows() -> tuple[list[dict], str]:
    return sh.collect(), datetime.now(_IL).strftime('%d/%m %H:%M:%S')


def _when(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone(_IL).strftime('%d/%m %H:%M')
    except ValueError:
        return '—'


def render() -> None:
    if core.user_role() != 'admin':
        st.info('העמוד זמין למנהל בלבד.')
        return
    rows, checked = _rows()
    over = sh.overall(rows)
    cnt = {k: sum(1 for r in rows if r['status'] == k) for k in (sh.OK, sh.WARN, sh.FAIL, sh.NA)}
    st.markdown('<div class="section-title">🩺 בריאות המערכת</div>', unsafe_allow_html=True)
    st.caption(f'{_DOT[over]} {cnt[sh.OK]} תקין · {cnt[sh.WARN]} אזהרה · {cnt[sh.FAIL]} כשל · {cnt[sh.NA]} אין נתון — '
               f'נבדק לאחרונה {checked} (מתרענן כל דקה) · קריאה בלבד')
    if st.button('רענן עכשיו', key='sys_refresh'):
        _rows.clear()
        st.rerun()
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r['group'], []).append(r)
    for g, items in groups.items():
        worst = sh.overall(items)
        with st.expander(f'{_DOT[worst]} {g} ({len(items)})', expanded=worst in (sh.FAIL, sh.WARN) or g != 'cron'):
            html = ['<table style="width:100%;border-collapse:collapse;direction:rtl;font-size:13px">',
                    '<tr style="color:var(--muted)"><th></th><th style="text-align:right">פריט</th>'
                    '<th style="text-align:right">מצב</th><th style="text-align:right">נבדק</th></tr>']
            for r in items:
                html.append(f'<tr style="border-top:1px solid var(--border,#3333)"><td>{_DOT[r["status"]]}</td>'
                            f'<td style="padding:4px 8px"><b>{esc(r["name"])}</b></td>'
                            f'<td style="padding:4px 8px">{esc(r["detail"])}</td>'
                            f'<td style="padding:4px 8px;white-space:nowrap">{esc(_when(r["checked"]))}</td></tr>')
            html.append('</table>')
            st.markdown(''.join(html), unsafe_allow_html=True)
    core.mark('system:render')
