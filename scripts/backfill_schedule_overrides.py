"""
scripts/backfill_schedule_overrides.py — the lesson changes the university announced before agent/schedule existed (16/11).

Reads the university updates the owner was actually sent (data/wa_outgoing.jsonl: "🚨 *חשוב מהאוניברסיטה*" and
"📬 *עדכונים מהאוניברסיטה*", each item "📧 *who* — subject ⏎ summary") over the last 3 weeks, keeps the ones the
relevance rule says are HIS session (integrations/univ_relevance.classify → 'yes'), and records what they cancel / move /
change room in data/schedule_overrides.json. No LLM, idempotent (one row per session-date), nothing is sent.

    venv/bin/python3 scripts/backfill_schedule_overrides.py            # dry run: what would be recorded
    venv/bin/python3 scripts/backfill_schedule_overrides.py --apply
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HEADS = ('📬 *עדכונים מהאוניברסיטה*', '🚨 *חשוב מהאוניברסיטה*', '🚨 *מייל שנראה דחוף*')
ITEM_RX = re.compile(r'^📧 \*([^*]+)\* — (.+)$')


def items(log: Path, since: date):
    """(ref day, who, subject, summary) for each university item he was sent since `since`."""
    try:
        lines = log.read_text(encoding='utf-8').splitlines()
    except OSError:
        return
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        text, ts = str(r.get('text') or ''), str(r.get('ts') or '')
        if not text.startswith(HEADS) or ts[:10] < since.isoformat():
            continue
        ref = date.fromisoformat(ts[:10])
        for block in text.split('\n\n')[1:]:
            rows = block.splitlines()
            m = ITEM_RX.match(rows[0]) if rows else None
            if not m:
                continue
            summary = ' '.join(x for x in rows[1:] if not x.startswith(('👉', '🎯', '❔')))
            yield ref, m.group(1).strip(), m.group(2).strip(), summary.strip()


def main(argv=None, log: Path | None = None, store: Path | None = None, today: date | None = None) -> list[dict]:
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--days', type=int, default=21)
    a = ap.parse_args(argv)
    from agent import schedule
    from integrations import univ_relevance as ur
    from whatsapp.outbox import SENT_LOG
    today = today or date.today()
    found = []
    for ref, who, subject, summary in items(log or SENT_LOG, today - timedelta(days=a.days)):
        subj = re.sub(r'^\s*\d{6,}\s*-\s*', '', subject)
        ck = ur.course_of(f'{who} {subject} {summary}')
        if not ck or ur.classify(ck, subj, summary, ref)['rel'] != 'yes':
            continue
        src = 'הודעה במודל' if 'moodle' in who.lower() or 'מודל' in who else f'מייל מ{who}'
        text = f'{subj}\n{summary}'
        if a.apply:
            new = schedule.add(ck, text, ref, src, note=summary, path=store)
        else:
            new = [p for p in schedule.plan(ck, text, ref) if p['date'] >= today.isoformat()]
        for p in new:
            print(f"{'+' if a.apply else '?'} {p['date']} {p['start']} {p['ck']} {p['kind']} {p['status']} ← {subj[:70]}")
        found += new
    print(f"{len(found)} {'recorded' if a.apply else 'would be recorded (--apply to write)'}")
    return found


if __name__ == '__main__':
    main()
