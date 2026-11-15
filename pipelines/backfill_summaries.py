"""
pipelines/backfill_summaries.py
================================
Permanent catch-up step: guarantee that EVERY good Hebrew SRT eventually gets
a summary — even on busy days where sync_all's per-run cap (todo[:5]) leaves
some transcripts behind.

Why this exists
---------------
sync_all._summaries() processes only the 5 NEWEST un-summarized transcripts
per run (cost guard). On a day with >5 new lectures, the oldest ones get
pushed down the queue and can be starved indefinitely, because every run
re-sorts newest-first and takes the top 5 again.

Real incident (2026-05-27): intro_to_cs "Lecture 10b" had a perfectly good
SRT but never got a summary — it kept losing the newest-first race.

This module closes that gap permanently. It is intentionally ADDITIVE: it does
NOT change the proven sync_all flow. It runs AFTER sync_all and only spends
money when there is a genuine gap. On a normal day it finds nothing and
returns immediately at $0.

Design choices that mirror the proven sync_all._summaries() logic:
  - same SRT parsing (course_key_YYYY-MM-DD_title)
  - same _classify_item_type / _has_summary / _moodle_pdfs helpers
  - same AISummarizer.generate_summary + save_summary calls (Gemini only)
Differences (the actual fix):
  - scans OLDEST-first so starved lectures are filled before fresh ones
  - gates every SRT through utils.srt_quality.srt_is_good_hebrew() (Cyrillic guard)
  - independent budget + higher cap so a real backlog actually drains

Run standalone:  python -m pipelines.backfill_summaries
"""

from __future__ import annotations
from pathlib import Path as _Path


def backfill_missing_summaries(budget_usd: float = 4.0, max_items: int = 15,
                               dry_run: bool = False,
                               max_age_days: int | None = 14) -> dict:
    """
    Scan all good Hebrew Panopto SRTs and summarize any that lack a summary.

    Parameters
    ----------
    budget_usd
        Soft cap for this catch-up pass (~$0.23/lecture). Stops before exceeding.
    max_items
        Hard cap on number of summaries generated in one pass (safety).
    dry_run
        If True, only scan and report gaps — generate nothing, spend nothing.
        Used by the daily health report to alert on any remaining gap.
    max_age_days
        Only consider SRTs whose lecture date is within this many days of today.
        Matches Panopto's 14-day discovery window so the catch-up fixes RECENT
        starvation only and never resurrects an old historical backlog.
        Pass None to scan the entire archive.

    Returns
    -------
    dict with: scanned, missing, generated, skipped_bad_srt, cost_usd, created[],
    and gaps[] (list of {course_key, date, title} still missing a summary).
    """
    from config.paths import PANOPTO_DIR, SUMMARIES_DIR, COURSE_NAMES
    from generators.summarizer import AISummarizer
    from utils.srt_quality import srt_is_good_hebrew
    from datetime import datetime as _dt, timedelta as _td
    import yaml as _yaml

    _cutoff = None
    if max_age_days is not None:
        _cutoff = (_dt.now() - _td(days=max_age_days)).strftime('%Y-%m-%d')

    course_keys = list(COURSE_NAMES.keys())

    def _classify_item_type(title: str) -> str:
        t = title.lower()
        if any(kw in t for kw in ('תרגול', 'tut', 'tirgul', 'recitation', 'תרגיל')):
            return 'tutorial'
        return 'lecture'

    def _has_summary(course_key: str, date_prefix: str, title: str) -> bool:
        """Check both lectures/ and tutorials/ folders for an existing summary."""
        item_type = _classify_item_type(title)
        canonical = _Path(AISummarizer.build_summary_path(
            course_key, item_type, title, date_prefix))
        if canonical.exists() and canonical.stat().st_size > 5000:
            return True
        # legacy: any date-prefixed file in either subfolder with real content
        for sub in ('lectures', 'tutorials'):
            d = SUMMARIES_DIR / course_key / sub
            if not d.exists():
                continue
            for p in d.glob(f'{date_prefix}*{title}*.md'):
                if p.stat().st_size > 5000 and '(מהמצגת)' not in p.name:
                    return True
        # Duplicate-recording guard (LECTURES only): the same session is often
        # re-published under two titles on the same day (e.g. "הרצאה 8" and
        # "הרצאות שלישי", both by the same professor). If ANY lecture summary
        # already exists for this course+date, treat sibling lecture recordings
        # as the same session — never summarize twice. Tutorials are NOT deduped
        # this way because different instructors run parallel tracks same-day.
        if item_type == 'lecture':
            for sub in ('lectures', 'tutorials'):
                d = SUMMARIES_DIR / course_key / sub
                if not d.exists():
                    continue
                for p in d.glob(f'{date_prefix}*.md'):
                    if p.stat().st_size <= 5000 or '(מהמצגת)' in p.name:   # 14/11: a slides stand-in isn't it
                        continue
                    existing_title = p.stem[10:].lstrip(' -') if len(p.stem) >= 10 else p.stem
                    if _classify_item_type(existing_title) == 'lecture':
                        return True
        return False

    try:
        _cfg = _yaml.safe_load(open('config/courses.yaml', encoding='utf-8'))
    except Exception:
        _cfg = {'courses': {}}

    def _moodle_pdfs(ck: str):
        pdfs = []
        sp = (_cfg.get('courses', {}).get(ck, {}) or {}).get('sources', {}).get('static_pdf', '')
        if sp and _Path(sp).exists():
            pdfs.append(sp)
        mdir = _Path(f'data/moodle/{ck}')
        if mdir.exists():
            pdfs += sorted(str(f) for f in mdir.glob('*.pdf'))
        return pdfs or None

    result = {
        'scanned': 0, 'missing': 0, 'generated': 0,
        'skipped_bad_srt': 0, 'cost_usd': 0.0, 'created': [], 'gaps': [],
    }

    if not PANOPTO_DIR.exists():
        return result

    todo: list[tuple] = []
    for srt in sorted(PANOPTO_DIR.glob('*.srt')):
        name = srt.name
        if name.endswith('.bad') or name.endswith('.bak') or name.endswith('.junk'):
            continue
        stem = srt.stem
        course_key = None
        for ck in sorted(course_keys, key=len, reverse=True):   # 23/10: microeconomics ⊂ microeconomics_2
            if stem.startswith(ck + '_'):
                course_key = ck
                break
        if not course_key:
            continue
        rest = stem[len(course_key) + 1:]
        if not (len(rest) >= 10 and rest[4] == '-' and rest[7] == '-'):
            continue
        date_prefix = rest[:10]
        title = rest[11:] if len(rest) > 11 else rest

        # Recency window: ignore anything older than the cutoff (default 14d).
        if _cutoff is not None and date_prefix < _cutoff:
            continue

        result['scanned'] += 1
        if _has_summary(course_key, date_prefix, title):
            continue
        result['missing'] += 1

        # Cyrillic / junk guard — never summarize from a bad SRT
        if not srt_is_good_hebrew(str(srt)):
            print(f'  ⏭️  מדלג (SRT לא עברי תקין): {name}')
            result['skipped_bad_srt'] += 1
            continue

        todo.append((date_prefix, course_key, srt, title))
        result['gaps'].append({'course_key': course_key,
                               'date': date_prefix, 'title': title})

    if not todo:
        print(f'  ✅ אין פערים — כל {result["scanned"]} התמלולים מסוכמים')
        return result

    # OLDEST-first: this is the actual fix — starved lectures get filled first.
    todo.sort(key=lambda x: x[0])

    if dry_run:
        print(f'  🔎 dry-run: {len(todo)} תמלולים תקינים ללא סיכום')
        return result

    print(f'  🔧 נמצאו {len(todo)} תמלולים ללא סיכום — משלים (oldest-first):')
    for d, ck, _s, t in todo:
        print(f'     • {d}  {ck}  {t}')

    summarizer = AISummarizer()
    for date_prefix, course_key, srt_path, title in todo[:max_items]:
        if result['cost_usd'] >= budget_usd - 0.30:
            print(f'  💸 תקציב backfill מוצה (${result["cost_usd"]:.2f}) — עוצר, ימשיך בריצה הבאה')
            break
        try:
            course_name = COURSE_NAMES.get(course_key, course_key)
            item_type = _classify_item_type(title)
            out_path = AISummarizer.build_summary_path(
                course_key, item_type, title, date_prefix)
            md_path = _Path(out_path)

            try:                                         # the lecturer's summaries for that week first (18/10)
                from datetime import date as _d
                from collectors.lecturer_site import week_pdfs
                lecturer = week_pdfs(course_key, _d.fromisoformat(str(date_prefix)[:10]))
                from collectors.course_files import for_lecture      # + this lecture's Moodle slides (19/10)
                lecturer += for_lecture(course_key, _d.fromisoformat(str(date_prefix)[:10]), title)
            except Exception as _le:
                print(f'  ⚠️ lecturer site: {_le}')
                lecturer = []
            summary, sources = summarizer.generate_summary(
                course_name=course_name,
                lecture_title=title,
                caption_path=str(srt_path),
                pdf_path=(lecturer + (_moodle_pdfs(course_key) or [])) or None,
                course_key=course_key,  # enables previous-lecture recap + covered-topics (was never passed)
                pdf_source_type='moodle',
                video_path=None,
            )
            if not summary or len(summary) < 2000 or summary.startswith('# שגיאה'):
                print(f'  ⚠️ סיכום ריק/קצר עבור {title} — מדלג')
                continue
            from utils.other_group import with_label     # 14/11: "from another group" at the top
            summary = with_label(summary, course_key, str(date_prefix), title,
                                 'תרגול' if item_type == 'tutorial' else 'הרצאה')

            pdf_result = summarizer.save_summary(
                summary=summary,
                output_path=str(md_path),
                course_name=course_name,
                lecture_title=title,
                sources=sources,
            )
            print(f'  ✅ הושלם: {md_path.name} (PDF: {bool(pdf_result)})')
            try:                                         # remember which lecturer summaries went in (rebuild later)
                from collectors.lecturer_site import record
                record(course_key, str(md_path), str(srt_path), title, str(date_prefix), lecturer)
            except Exception as _re:
                print(f'  ⚠️ lecturer record: {_re}')
            result['generated'] += 1
            result['cost_usd'] += 0.23
            result['created'].append({
                'course_key': course_key, 'item_type': item_type,
                'title': title, 'md_path': str(md_path),
            })
        except Exception as ex:
            print(f'  ⚠️ backfill נכשל עבור {srt_path.name}: {ex}')
            continue

    return result


if __name__ == '__main__':
    import sys
    from pathlib import Path
    ROOT = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(ROOT))
    try:
        from utils.env import _load_env_dotfile
        _load_env_dotfile()
        import os
        if os.environ.get('GEMINI_API_KEY'):
            os.environ.pop('GOOGLE_API_KEY', None)
    except Exception:
        pass
    r = backfill_missing_summaries()
    print('\nRESULT:', r)
