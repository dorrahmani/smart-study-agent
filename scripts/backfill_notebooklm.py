"""
scripts/backfill_notebooklm.py — One-off NotebookLM artifact backfill.

For every existing summary md in data/summaries/{course}/(lectures|tutorials)/
that does NOT yet have matching Quiz + Flashcards in
🎓 NotebookLM/{course}/, push the SRT into the per-course/scope notebook
and generate both artifacts. Idempotent — re-running skips finished items.

Usage:
    python -m scripts.backfill_notebooklm                  # all courses, both scopes
    python -m scripts.backfill_notebooklm --course calculus_2
    python -m scripts.backfill_notebooklm --scope lectures # only lectures
    python -m scripts.backfill_notebooklm --limit 5        # cap at 5/run
    python -m scripts.backfill_notebooklm --dry-run        # preview only

Cost: $0 (NotebookLM/Gemini quota). Time: ~2-3 min per summary.
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

# Re-encode stdout so Hebrew + emoji print cleanly on Windows consoles.
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

from config.paths import (
    SUMMARIES_DIR, PANOPTO_DIR, VAULT_NOTEBOOKLM_DIR,
    COURSE_NAMES, COURSE_ORDER,
)


# ─── Filename matching ─────────────────────────────────────────────────────

def _summary_key(md_path: Path) -> tuple[str, str]:
    """Return (date, title-fragment) from `2026-04-14 - X - Y.md` → ('2026-04-14','X - Y')."""
    stem = md_path.stem
    if len(stem) >= 12 and stem[4] == '-' and stem[7] == '-' and stem[10] == ' ':
        date = stem[:10]
        rest = stem[10:].lstrip(' -').strip()
        return (date, rest)
    return ('', stem)


def _find_srt(course_key: str, date: str, title_frag: str) -> Path | None:
    """
    Search PANOPTO_DIR for an .srt file that matches a given summary.
    Tries (in order):
      1. {course_key}_{date}_{title_frag}.srt        — canonical dated form
      2. {course_key}_{title_frag}.srt               — legacy un-dated
      3. any .srt that startswith({course_key}_) AND contains title_frag
    """
    if not PANOPTO_DIR.exists():
        return None

    # 1. canonical
    canonical = PANOPTO_DIR / f'{course_key}_{date}_{title_frag}.srt'
    if canonical.exists():
        return canonical

    # 2. legacy un-dated
    legacy = PANOPTO_DIR / f'{course_key}_{title_frag}.srt'
    if legacy.exists():
        return legacy

    # 3. fuzzy: course_key prefix + title fragment substring
    title_norm = title_frag.replace('--', '-').strip()
    for srt in PANOPTO_DIR.glob(f'{course_key}_*.srt'):
        name = srt.name
        # heuristic: shared last 12 chars of the title (cheap fuzzy)
        if title_norm in name or any(
            tok and tok in name
            for tok in title_norm.split(' - ')
        ):
            return srt

    return None


# ─── Vault state inspection ────────────────────────────────────────────────

def _has_artifact(course_key: str, summary_title: str, art_type: str) -> bool:
    """True if a quiz/flashcard md already exists for this title."""
    course_dir = VAULT_NOTEBOOKLM_DIR / course_key
    if not course_dir.exists():
        return False
    # exporter writes: {date} - {art_type} - {title}.md (with title sanitized).
    # We match on art_type + the trailing safe(title) suffix.
    safe_suffix = summary_title.replace('"', '_').replace('/', '_').strip()[:100]
    pat = f'* - {art_type} - {safe_suffix}.md'
    return any(course_dir.glob(pat))


# ─── Plan: which summaries need work ───────────────────────────────────────

def discover_pending(
    course_filter: str | None = None,
    scope_filter:  str | None = None,
) -> list[dict]:
    """
    Walk every {course}/(lectures|tutorials)/*.md and return entries that
    are missing AT LEAST one of: quiz, flashcard. Each entry:
        {
          course_key, item_type ('lecture'|'tutorial'), scope, title,
          md_path, srt_path | None, missing: ['quiz', 'flashcard']
        }
    """
    pending: list[dict] = []
    if not SUMMARIES_DIR.exists():
        return pending

    for course_key in COURSE_ORDER:
        if course_filter and course_key != course_filter:
            continue
        course_dir = SUMMARIES_DIR / course_key
        if not course_dir.is_dir():
            continue

        for sub, item_type, scope in (
            ('lectures',  'lecture',  'lectures_only'),
            ('tutorials', 'tutorial', 'tutorials_only'),
        ):
            if scope_filter and scope_filter not in (sub, item_type, scope):
                continue
            sub_dir = course_dir / sub
            if not sub_dir.is_dir():
                continue
            for md in sorted(sub_dir.glob('*.md')):
                _, title_frag = _summary_key(md)
                title = md.stem  # full stem, used as artifact title
                missing = []
                if not _has_artifact(course_key, title, 'quiz'):
                    missing.append('quiz')
                if not _has_artifact(course_key, title, 'flashcard'):
                    missing.append('flashcard')
                if not missing:
                    continue
                date, _ = _summary_key(md)
                srt = _find_srt(course_key, date, title_frag)
                pending.append({
                    'course_key': course_key,
                    'item_type':  item_type,
                    'scope':      scope,
                    'title':      title,
                    'md_path':    md,
                    'srt_path':   srt,
                    'missing':    missing,
                })
    return pending


# ─── Execution ─────────────────────────────────────────────────────────────

def run_backfill(
    pending: list[dict],
    limit: int = 0,
    dry_run: bool = False,
) -> dict:
    """Generate missing artifacts for each pending entry. Returns summary."""
    from generators.notebooklm_agent import NotebookLMAgent
    from exporters.notebooklm_obsidian import export_artifact

    if not NotebookLMAgent.is_authenticated():
        return {
            'ok':    False,
            'note':  ('NotebookLM not authenticated. '
                      'Run: notebooklm login   (one-time)'),
            'pending_count': len(pending),
        }

    agent = NotebookLMAgent()
    todo = pending if not limit else pending[:limit]
    n_quiz = 0
    n_cards = 0
    errors: list[dict] = []
    skipped_no_srt: list[str] = []
    started = time.time()

    for i, entry in enumerate(todo, 1):
        course_key = entry['course_key']
        scope      = entry['scope']
        title      = entry['title']
        srt        = entry['srt_path']
        missing    = entry['missing']
        course_name = COURSE_NAMES.get(course_key, course_key)

        print(f'[{i}/{len(todo)}] {course_key}/{scope} → {title}'
              f'   (missing: {", ".join(missing)})')

        if dry_run:
            continue

        # 1. Push SRT (if found and not already attached).
        if srt and srt.exists():
            try:
                r = agent.sync_sources(course_key, scope, srt_files=[str(srt)])
                if r.get('added'):
                    print(f'    ✓ added source: {", ".join(r["added"])}')
            except Exception as e:
                errors.append({'phase': 'sync_sources', 'item': title, 'error': str(e)})
                print(f'    ⚠ sync_sources failed: {e}')
                # don't `continue` — generation can still succeed if the
                # notebook already has earlier sources from a prior run.
        else:
            skipped_no_srt.append(f'{course_key}/{title}')
            print('    ⚠ no SRT found — generating from existing notebook sources only')

        today = datetime.now().strftime('%Y-%m-%d')

        # 2. Quiz.
        if 'quiz' in missing:
            try:
                quiz = agent.generate_quiz(
                    course_key, scope, topic_hint=title,
                )
                questions = quiz.get('questions') or quiz
                export_artifact({
                    'course_key':     course_key,
                    'course_name':    course_name,
                    'artifact_type':  'quiz',
                    'title':          title,
                    'date':           today,
                    'generated_from': [f'[[{title}]]'],
                    'file_path':      quiz.get('_path'),
                    'data':           questions,
                })
                n_quiz += 1
                print('    ✓ quiz generated')
            except Exception as e:
                errors.append({'phase': 'quiz', 'item': title, 'error': str(e)})
                print(f'    ⚠ quiz failed: {e}')

        # 3. Flashcards.
        if 'flashcard' in missing:
            try:
                cards = agent.generate_flashcards(course_key, scope)
                cards_data = cards.get('cards') or cards.get('flashcards') or cards
                export_artifact({
                    'course_key':     course_key,
                    'course_name':    course_name,
                    'artifact_type':  'flashcard',
                    'title':          title,
                    'date':           today,
                    'generated_from': [f'[[{title}]]'],
                    'file_path':      cards.get('_path'),
                    'data':           cards_data,
                })
                n_cards += 1
                print('    ✓ flashcards generated')
            except Exception as e:
                errors.append({'phase': 'flashcard', 'item': title, 'error': str(e)})
                print(f'    ⚠ flashcards failed: {e}')

    return {
        'ok':              True,
        'planned':         len(todo),
        'pending_total':   len(pending),
        'quizzes_made':    n_quiz,
        'flashcards_made': n_cards,
        'errors':          errors,
        'skipped_no_srt':  skipped_no_srt,
        'duration_s':      round(time.time() - started, 1),
        'cost_usd':        0.0,
        'dry_run':         dry_run,
    }


# ─── CLI ───────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(
        description='One-off NotebookLM Quiz+Flashcards backfill.'
    )
    p.add_argument('--course',   type=str, default=None,
                   help='Restrict to one course_key (e.g. calculus_2).')
    p.add_argument('--scope',    type=str, default=None,
                   help='lectures | tutorials  (default: both).')
    p.add_argument('--limit',    type=int, default=0,
                   help='Cap at N items (default: no cap).')
    p.add_argument('--dry-run',  action='store_true',
                   help='Just list what would be generated.')
    args = p.parse_args()

    pending = discover_pending(
        course_filter=args.course,
        scope_filter=args.scope,
    )

    print(f'Pending: {len(pending)} summaries need NotebookLM artifacts.')
    if args.scope:
        print(f'  filter: scope={args.scope}')
    if args.course:
        print(f'  filter: course={args.course}')
    print()

    if not pending:
        print('🎉 All summaries already have Quiz + Flashcards. Nothing to do.')
        return 0

    # Brief preview
    by_course: dict[str, int] = {}
    for e in pending:
        by_course[e['course_key']] = by_course.get(e['course_key'], 0) + 1
    for ck, n in by_course.items():
        print(f'  {ck}: {n} pending')
    print()

    try:
        report = run_backfill(pending, limit=args.limit, dry_run=args.dry_run)
    except Exception as e:
        print(f'FATAL: {e}')
        traceback.print_exc()
        return 1

    print()
    print('────── Summary ──────')
    print(f'  planned:         {report.get("planned")}')
    print(f'  pending total:   {report.get("pending_total")}')
    print(f'  quizzes made:    {report.get("quizzes_made")}')
    print(f'  flashcards made: {report.get("flashcards_made")}')
    if report.get('skipped_no_srt'):
        print(f'  no SRT:          {len(report["skipped_no_srt"])} '
              f'(notebook generation may still have worked from existing sources)')
    if report.get('errors'):
        print(f'  errors:          {len(report["errors"])}')
        for err in report['errors'][:5]:
            print(f'    - {err["phase"]} | {err["item"]} | {err["error"][:80]}')
    print(f'  duration:        {report.get("duration_s")}s')
    print(f'  cost:            ${report.get("cost_usd"):.4f}')
    if report.get('dry_run'):
        print('  (dry-run — no artifacts were generated)')

    return 0 if report.get('ok') else 2


if __name__ == '__main__':
    sys.exit(main())
