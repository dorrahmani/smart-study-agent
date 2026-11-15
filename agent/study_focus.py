"""
agent/study_focus.py — what is worth studying today, per course (20/10, the "learning state" round).

    taught     the course units that this semester's lecture summaries cover (their headings, matched to the units
               of the exam map — data/exam_topics/<course>.json)
    weight     how often the unit comes up in the course's past exams (agent/exam_weight)
    practiced  tutor / quiz / practice answers on it in the last 30 days (utils/mastery events)
    → next_topic(): the taught unit with the most exam weight that hasn't been practiced (practiced ones count 40%).
The morning task page names it ("📚 30 דק׳ הסתברות — בייס (90% מהמבחנים) · 'תביא לי שאלה על בייס'"), and
"על מה כדאי לי ללמוד?" answers from it. No LLM.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASK_RX = re.compile(r'(?:על )?מה (?:כדאי|שווה|הכי חשוב|עדיף) (?:לי )?(?:ל)?(?:למוד|ללמוד|לחזור|לתרגל)|מה ללמוד (?:היום|עכשיו)|'
                    r'במה (?:כדאי|שווה) להתמקד|מה (?:הכי )?חשוב (?:ללמוד|לחזור)')


def taught(ck: str, since: date, summaries: Path | None = None) -> set[str]:
    """Units this semester's summaries of the course cover (headings matched to the exam map's units)."""
    from agent import exam_weight as ew
    rows = ew.load(ck).get('stats') or []
    if not rows:
        return set()
    d = (summaries or ROOT / 'data' / 'summaries') / ck
    out: set[str] = set()
    for md in d.glob('*/*.md') if d.exists() else []:
        if md.name[:10] < since.isoformat():
            continue
        try:
            heads = re.findall(r'^#{1,3}\s+(.+)$', md.read_text(encoding='utf-8')[:60000], re.M)
        except OSError:
            continue
        for h in heads:
            r = ew.find(h, rows, ck)
            if r:
                out.add(r['topic'])
    return out


def practiced(ck: str, now: datetime | None = None, days: int = 30) -> dict[str, int]:
    """{raw topic: answers} of the last `days` (utils/mastery events)."""
    try:
        from utils import mastery
        evs = mastery.events(ck, now=now)
    except Exception:
        return {}
    cut = ((now or datetime.now()) - timedelta(days=days)).isoformat()
    out: dict[str, int] = {}
    for e in evs:
        if e.get('ts', '') >= cut:
            out[e.get('topic', '')] = out.get(e.get('topic', ''), 0) + 1
    return out


def next_topic(ck: str, since: date, now: datetime | None = None, summaries: Path | None = None) -> dict | None:
    from agent import exam_weight as ew
    rows = ew.load(ck).get('stats') or []
    have = taught(ck, since, summaries)
    if not rows or not have:
        return None
    done = practiced(ck, now)

    def tried(topic: str) -> bool:
        return any(ew.find(raw, [{'topic': topic}], ck) for raw in done)
    best = max((r for r in rows if r['topic'] in have),
               key=lambda r: r['exam_pct'] * (0.4 if tried(r['topic']) else 1.0), default=None)
    if not best:
        return None
    return {'course': ck, 'topic': best['topic'], 'exam_pct': best['exam_pct'], 'practiced': tried(best['topic'])}


def today_pick(today: date, since: date, courses: list[str] | None = None, summaries: Path | None = None) -> dict | None:
    """One course a day (rotating among those with something taught + an exam map) → its next topic."""
    from agent import exam_weight as ew
    cands = [n for ck in (courses or ew.courses()) if (n := next_topic(ck, since, summaries=summaries))]
    if not cands:
        return None
    return cands[today.toordinal() % len(cands)]


def line(p: dict) -> str:
    from config.paths import COURSE_NAMES
    short = p['topic'].split(' (')[0]
    return (f"• 📚 30 דק׳ {COURSE_NAMES.get(p['course'], p['course'])} — {short} "
            f"(ב-{p['exam_pct']}% מהמבחנים{'' if p['practiced'] else ', עוד לא תרגלת'}) · \"תביא לי שאלה על {short}\"")


def answer(today: date, since: date) -> str:
    from agent import exam_weight as ew
    from config.paths import COURSE_NAMES
    out = []
    for ck in ew.courses():
        p = next_topic(ck, since)
        if p:
            short = p['topic'].split(' (')[0]
            out.append(f"• *{COURSE_NAMES.get(ck, ck)}*: {short} — ב-{p['exam_pct']}% מהמבחנים"
                       + ('' if p['practiced'] else ', ועוד לא תרגלת') + f'\n  → "תביא לי שאלה על {short}"')
    if not out:
        return '📚 עוד אין לי מספיק — צריך סיכומי הרצאות מהסמסטר ומפת נושאים מהמבחנים של הקורס.'
    return '🎯 *הכי שווה ללמוד עכשיו* (מה שכבר נלמד × כמה זה מופיע במבחנים × מה עוד לא תרגלת):\n' + '\n'.join(out)
