"""
whatsapp/summary_push.py — each new lecture/tutorial summary PDF to the owner on WhatsApp, once.

13/10/26: the daily pipeline sent summaries only by email + Telegram, while the owner lives on WhatsApp. Called from
pipelines/daily_full_update.py (step 9d). Sent paths are remembered in data/wa_summaries_sent.json. No LLM.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path


def _week_key(key: str) -> str:
    """'probability/lectures/2026-10-25 - הרצאה 2.pdf' → 'probability/lectures/week-2026-10-25' (the Sunday)."""
    import re
    m = re.match(r'([^/]+)/(lectures|tutorials)/(\d{4}-\d{2}-\d{2})', key)
    if not m:
        return ''
    d = datetime.fromisoformat(m.group(3))
    sunday = d - timedelta(days=(d.weekday() + 1) % 7)
    return f'{m.group(1)}/{m.group(2)}/week-{sunday:%Y-%m-%d}'


def push_new(log=print, now: datetime | None = None, summaries: Path | None = None, sent_path: Path | None = None,
             send=None, chat: str | None = None, limit: int = 6) -> int:
    from config.paths import SUMMARIES_DIR, COURSE_NAMES, DATA_DIR
    from whatsapp import outbox
    chat = chat if chat is not None else (outbox.owner_chat() if outbox.enabled() else '')
    if not chat:
        log('  → WhatsApp not set up — skipping')
        return 0
    send = send or outbox.send_document
    root = summaries or SUMMARIES_DIR
    sent_path = sent_path or DATA_DIR / 'wa_summaries_sent.json'
    try:
        sent = set(json.loads(sent_path.read_text(encoding='utf-8')))
    except (OSError, ValueError):
        sent = set()
    cutoff = (now or datetime.now()) - timedelta(hours=6)
    # 02/11 the owner: "אתה מסכם לי כמה הקצאות כמה פעמים" — a rebuilt summary (cut-off fix), the recording's summary after
    # the slides stand-in, a second group's tutorial: each came again as a new PDF. One summary per course, type
    # and week reaches WhatsApp; a newer version of the same lesson only updates the site / vault, silently.
    weeks = {k for k in sent if '/week-' in k}
    weeks |= {wk for k in sent if (wk := _week_key(k))}
    new = []
    for ck in COURSE_NAMES:
        for sub in ('lectures', 'tutorials'):
            d = root / ck / sub
            if not d.exists():
                continue
            for pdf in d.glob('*.pdf'):
                key = f'{ck}/{sub}/{pdf.name}'
                if key in sent or datetime.fromtimestamp(pdf.stat().st_mtime) < cutoff or pdf.stat().st_size < 10_000:
                    continue
                if _week_key(key) in weeks:
                    log(f'  → {key}: this week\'s {sub[:-1]} was already sent — not again')
                    sent.add(key)
                    continue
                new.append((ck, sub, pdf, key))
    if not new:
        log('  → no new summary PDFs for WhatsApp')
        return 0
    ok = 0
    for ck, sub, pdf, key in sorted(new, key=lambda x: x[2].name)[:limit]:
        cname = COURSE_NAMES.get(ck, ck)
        from utils.other_group import md_has, line_of, TAG     # 14/11: another group's recording says so
        md = pdf.with_suffix('.md')
        og = line_of(md.read_text(encoding='utf-8')) if md_has(md) else ''
        cap = (f"{'📝 תרגול' if sub == 'tutorials' else '📖 הרצאה'} — *{cname}*{' ' + TAG if og else ''}\n{pdf.stem}\n"
               + (f"{og}\n" if og else '') + "💬 אפשר לשאול עליו כאן")
        if _week_key(key) in weeks:                        # two of the same week in this very batch
            sent.add(key)
            continue
        if send(chat, pdf, cap):
            sent.add(key)
            weeks.add(_week_key(key))
            ok += 1
    sent_path.parent.mkdir(parents=True, exist_ok=True)
    sent_path.write_text(json.dumps(sorted(sent), ensure_ascii=False), encoding='utf-8')
    log(f'  📱 WhatsApp: {ok}/{len(new)} summaries sent')
    return ok
