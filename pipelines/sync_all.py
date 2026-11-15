"""
pipelines/sync_all.py — One-shot full refresh of every data source into the
Obsidian Vault.

Order: cheap-and-fast first, costly steps last (with budget gate).
Every step MUST end with data in the vault — that's non-negotiable.

This orchestrator delegates to existing modules — it does NOT re-implement
anything. Adapter calls are aligned with the real API surfaces:
  - collectors.moodle_watcher.sync()                 -> writes assignments + announcements to vault
  - collectors.panopto_collector.PanoptoCollector().collect_all(days_back)
                                                     -> populates data/panopto + data/summaries
  - exporters.obsidian_exporter.sync_all()           -> sweeps data/summaries -> vault,
                                                        also runs relinker + writes course/home/dashboard
  - rag.vault_indexer.build_index()                  -> embeddings or TF-IDF over the vault
  - vault_sync.graph_config.write()                  -> Obsidian graph colors

Returns a structured result so the dashboard can show progress + cost.
"""

from __future__ import annotations
import json
import time
import traceback
from datetime import date as _date, datetime
from pathlib import Path
from typing import Any, Callable

from config.paths import DATA_DIR

COSTS_PATH = DATA_DIR / 'costs.json'
SYNC_LOG_PATH = DATA_DIR / 'sync_log.json'


def _log_cost(step: str, cost_usd: float, items: int) -> None:
    rec = {
        'date':     datetime.now().isoformat(),
        'step':     step,
        'cost_usd': round(cost_usd, 6),
        'items':    items,
    }
    history: list = []
    if COSTS_PATH.exists():
        try:
            history = json.loads(COSTS_PATH.read_text(encoding='utf-8'))
        except Exception:
            history = []
    history.append(rec)
    COSTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    COSTS_PATH.write_text(
        json.dumps(history, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )


def _step(name: str, fn: Callable, results: list, budget_left: list) -> Any:
    """Run a step, capture timing/items/cost, append to results list."""
    t0 = time.time()
    try:
        out = fn()
        items = (out.get('items') if isinstance(out, dict) else None) or 0
        cost = (out.get('cost_usd') if isinstance(out, dict) else None) or 0.0
        results.append({
            'name':       name,
            'items':      items,
            'cost_usd':   cost,
            'duration_s': round(time.time() - t0, 1),
            'result':     out,
        })
        budget_left[0] -= cost
        if cost > 0:
            _log_cost(name, cost, items)
        return out
    except Exception as e:
        results.append({
            'name':       name,
            'error':      str(e),
            'traceback':  traceback.format_exc()[:500],
            'duration_s': round(time.time() - t0, 1),
        })
        return None


def sync_all(
    budget_usd:         float = 2.0,
    include_panopto:    bool = True,
    include_whisper:    bool = True,
    include_summaries:  bool = True,
    include_notebooklm: bool = True,
    headless:           bool = True,
    preview:            bool = False,
    force:              bool = False,
    progress_callback:  Callable | None = None,
) -> dict:
    """
    Full refresh in cost-ascending order. All output ends in the Obsidian Vault.

    Parameters
    ----------
    budget_usd
        Soft budget; once consumed, costly steps are skipped.
    include_panopto
        If True, run Panopto collector (downloads new SRTs / transcribes).
    include_whisper
        If True, allows Panopto collector to fall back to Whisper transcription
        when no SRT is available. The flag is honored by reading the env var
        SMART_STUDY_DISABLE_WHISPER which the existing collector already
        respects in its default config; we set it for this run only.
    include_summaries
        If True, attempts to drive AISummarizer over any new SRTs that don't
        yet have a markdown summary. Off by default in scheduled runs (cost!).
    headless
        Forwarded to moodle_watcher.

    Returns
    -------
    dict
        steps[], total_cost_usd, total_duration_s, n_new, started_at,
        finished_at, summary_text (Hebrew, ready for toast).
    """
    started = datetime.now()
    results: list = []
    budget_left = [budget_usd]

    # Preview mode: short-circuit all costly steps. Cheap collectors and
    # vault-side work still run so the dashboard sees an accurate diff.
    if preview:
        include_summaries  = False
        include_notebooklm = False
        # whisper transcription is opt-in; preview always disables it
        include_whisper    = False

    # Captured by step 3 (summaries) → consumed by NotebookLM auto-gen below.
    # Each entry: {'course_key', 'item_type', 'title', 'md_path', 'srt_path'}
    new_summaries: list[dict] = []

    def _progress(msg: str, frac: float) -> None:
        if progress_callback:
            try:
                progress_callback(msg, frac)
            except Exception:
                pass

    # ─── Step 1: Moodle (assignments + grades + announcements) ──────────
    # End-state: assignment + announcement notes in the vault.
    _progress('סורק את Moodle...', 0.05)

    def _moodle():
        from collectors.moodle_watcher import sync
        # 04/11: run_fetcher=True ran fetch_assignments_now's hardcoded list (last semester) over _cache.json for ~15 min
        # every evening, until step 3b's scraper restored it — the bot saw submitted work as open. The scraper owns it.
        r = sync(run_fetcher=False, scrape_announcements=True, headless=headless) or {}
        items = (
            (r.get('new_assignments', 0) or 0)
            + (r.get('updated_assignments', 0) or 0)
            + (r.get('new_announce', 0) or 0)
        )
        return {'items': items, 'cost_usd': 0.0, 'detail': r}

    _step('moodle_assignments_announcements', _moodle, results, budget_left)
    _progress('Moodle נסרק', 0.20)

    # ─── Step 2: Panopto (new lectures + SRT/transcripts) ───────────────
    # End-state: SRTs and pre-summary markdown in data/panopto + data/summaries.
    # The vault note is created later in Step 5 (export sweep).
    if include_panopto:
        _progress('סורק את Panopto להרצאות חדשות...', 0.25)

        # Honor include_whisper via env var (collector reads its own config).
        import os
        prev_whisper = os.environ.get('SMART_STUDY_DISABLE_WHISPER')
        if not include_whisper:
            os.environ['SMART_STUDY_DISABLE_WHISPER'] = '1'

        def _panopto():
            from collectors.panopto_collector import PanoptoCollector
            pc = PanoptoCollector()
            # collect_all returns Dict[course_key, List[lecture]]
            per_course = pc.collect_all(days_back=14)
            n = 0
            whisper_cost = 0.0
            if isinstance(per_course, dict):
                for _ck, lectures in per_course.items():
                    if not lectures:
                        continue
                    n += len(lectures)
                    if include_whisper:
                        for lec in lectures:
                            if isinstance(lec, dict) and lec.get('transcribed_via') == 'whisper':
                                dur_min = lec.get('duration_min', 90)
                                # rough estimate, ~$0.006/min for whisper-1
                                whisper_cost += dur_min * 0.006
            return {'items': n, 'cost_usd': whisper_cost, 'detail': per_course}

        try:
            _step('panopto_lectures', _panopto, results, budget_left)
        finally:
            # Restore env
            if not include_whisper:
                if prev_whisper is None:
                    os.environ.pop('SMART_STUDY_DISABLE_WHISPER', None)
                else:
                    os.environ['SMART_STUDY_DISABLE_WHISPER'] = prev_whisper

        _progress('Panopto נסרק', 0.45)

    # ─── Step 3: Generate summaries for new transcripts ─────────────────
    # End-state: markdown summaries in data/summaries/{course}/...
    # Off by default in scheduled runs (Sonnet, ~$0.10-0.30/lecture).
    if include_summaries and budget_left[0] > 0.10:
        _progress('יוצר סיכומים מהרצאות חדשות...', 0.50)

        def _summaries():
            # Use the rich AISummarizer (with timestamped lesson map, professor tips,
            # exam-priority tags, etc.) and generate PDFs too.
            from config.paths import PANOPTO_DIR, SUMMARIES_DIR, COURSE_NAMES
            from generators.summarizer import AISummarizer
            from pathlib import Path as _Path
            import yaml as _yaml

            course_keys = list(COURSE_NAMES.keys())

            def _classify_item_type(title: str) -> str:
                t = title.lower()
                if any(kw in t for kw in ('תרגול', 'tut', 'tirgul', 'recitation', 'תרגיל')):
                    return 'tutorial'
                return 'lecture'

            def _has_summary(course_key: str, date_prefix: str, title: str) -> bool:
                """Check both lectures/ and tutorials/ folders for an existing summary."""
                item_type = _classify_item_type(title)
                # primary: AISummarizer canonical path
                canonical = _Path(AISummarizer.build_summary_path(
                    course_key, item_type, title, date_prefix))
                if canonical.exists() and canonical.stat().st_size > 5000:
                    return True
                # legacy: any date-prefixed file in either subfolder
                for sub in ('lectures', 'tutorials'):
                    d = SUMMARIES_DIR / course_key / sub
                    if not d.exists(): continue
                    # 14/11: a slides stand-in "(מהמצגת)" isn't the recording's summary — it counted as one, so
                    # software 1's Lecture 3 recording was never summarised and the stand-in was then moved aside
                    real = [p for p in d.glob(f'{date_prefix}*.md') if '(מהמצגת)' not in p.name]
                    if any(p for p in d.glob(f'{date_prefix} *{title}*.md') if '(מהמצגת)' not in p.name) or \
                       real and any(p.stat().st_size > 5000 for p in real):
                        return True
                return False

            # Build list of SRTs needing summarization
            todo: list[tuple] = []
            if PANOPTO_DIR.exists():
                for srt in sorted(PANOPTO_DIR.glob('*.srt')):
                    if srt.name.endswith('.bad') or srt.name.endswith('.bak'):
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
                    if not _has_summary(course_key, date_prefix, title):
                        todo.append((date_prefix, course_key, srt, title))

            if not todo:
                return {'items': 0, 'cost_usd': 0.0, 'note': 'no transcripts need summarizing'}

            todo.sort(key=lambda x: x[0], reverse=True)  # newest first

            # Load Moodle PDFs config (per course) — passed to AISummarizer for richer context
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

            summarizer = AISummarizer()
            n_done = 0
            total_cost = 0.0
            for date_prefix, course_key, srt_path, title in todo[:5]:  # cap at 5/run
                if budget_left[0] - total_cost < 0.30:
                    break
                try:
                    course_name = COURSE_NAMES.get(course_key, course_key)
                    item_type = _classify_item_type(title)
                    out_path = AISummarizer.build_summary_path(
                        course_key, item_type, title, date_prefix)
                    md_path = _Path(out_path)
                    pdf_path = md_path.with_suffix('.pdf')

                    # 18/10: the lecturer's own summaries for this lecture's week (probability: lecturer-site.example.com) go first —
                    # one unified summary of the class + his notes
                    try:
                        from collectors.lecturer_site import week_pdfs
                        lecturer = week_pdfs(course_key, _date.fromisoformat(str(date_prefix)[:10]))
                    except Exception as _le:
                        print(f'  ⚠️ lecturer site: {_le}')
                        lecturer = []
                    try:                                 # 19/10: this lecture's Moodle slides (every course)
                        from collectors.course_files import for_lecture
                        lecturer = lecturer + for_lecture(course_key, _date.fromisoformat(str(date_prefix)[:10]), title)
                    except Exception as _se:
                        print(f'  ⚠️ course files: {_se}')
                    pdfs = lecturer + (_moodle_pdfs(course_key) or [])

                    # Generate rich summary (timestamped lesson map + professor tips + PDFs)
                    summary, sources = summarizer.generate_summary(
                        course_name=course_name,
                        lecture_title=title,
                        caption_path=str(srt_path),
                        pdf_path=pdfs or None,
                        course_key=course_key,  # enables previous-lecture recap + covered-topics (was never passed)
                        pdf_source_type='moodle',
                        video_path=None,
                    )
                    if not summary or len(summary) < 2000 or summary.startswith('# שגיאה'):
                        print(f'  ⚠️ סיכום ריק/קצר עבור {title}')
                        continue
                    from utils.other_group import with_label     # 14/11: "from another group" at the top
                    summary = with_label(summary, course_key, str(date_prefix), title,
                                         'תרגול' if item_type == 'tutorial' else 'הרצאה')

                    # Save MD + PDF
                    pdf_result = summarizer.save_summary(
                        summary=summary,
                        output_path=str(md_path),
                        course_name=course_name,
                        lecture_title=title,
                        sources=sources,
                    )
                    print(f'  ✅ סיכום נשמר: {md_path.name} (PDF: {bool(pdf_result)})')
                    try:                                 # remember which lecturer summaries went in (rebuild later)
                        from collectors.lecturer_site import record
                        record(course_key, str(md_path), str(srt_path), title, str(date_prefix), lecturer)
                    except Exception as _re:
                        print(f'  ⚠️ lecturer record: {_re}')
                    n_done += 1
                    new_summaries.append({
                        'course_key': course_key,
                        'item_type':  item_type,
                        'title':      title,
                        'md_path':    str(md_path),
                        'srt_path':   str(srt_path),
                    })
                    total_cost += 0.20
                except Exception as ex:
                    print(f'  ⚠️ summary failed for {srt_path.name}: {ex}')
                    continue
            return {'items': n_done, 'cost_usd': total_cost,
                    'new_summaries': len(new_summaries)}

        _step('summaries', _summaries, results, budget_left)
        _progress('סיכומים נוצרו', 0.65)

    # ─── Step 4: Concept extraction ─────────────────────────────────────
    # extract_concepts runs per-note and is cached; the relinker pass below
    # invokes it for every note, so this step is a no-op when relinker is
    # active. We keep an explicit hook for future use.
    _progress('הכנה לחילוץ מושגים (מבוצע ב-relinker)...', 0.70)

    def _concepts():
        # The actual extraction happens lazily inside obsidian_exporter +
        # relinker (both call extractors.concept_extractor.extract_concepts
        # per note, with a per-note mtime cache). Nothing to do here.
        return {'items': 0, 'cost_usd': 0.0,
                'note': 'extraction deferred to relinker pass'}

    _step('concept_extraction', _concepts, results, budget_left)
    _progress('מושגים יחולצו תוך כדי relink', 0.75)

    # ─── Step 5: Sweep summaries + indexes + relinker into the vault ────
    # End-state: every md in data/summaries/ is mirrored to vault, and
    # every note has up-to-date `concepts:` + `related:` frontmatter.
    _progress('מסנכרן סיכומים, מטלות ואינדקסים ל-Vault...', 0.78)

    def _vault_sweep():
        from exporters.obsidian_exporter import sync_all as _vault_sync
        r = _vault_sync(force=False) or {}
        items = (
            (r.get('summaries', 0) or 0)
            + (r.get('quizzes', 0) or 0)
            + (r.get('relinked', 0) or 0)
        )
        return {'items': items, 'cost_usd': 0.0, 'detail': r}

    _step('vault_sync_and_relink', _vault_sweep, results, budget_left)
    _progress('Vault סונכרן', 0.88)

    # ─── Step 5b: NotebookLM auto-gen (Quiz + Flashcards) — FREE ────────
    # End-state: 🎓 NotebookLM/{course}/{date} - quiz - …md and
    # …- flashcard - …md exist for every newly-summarized lecture/tutorial.
    # Cost: $0.00 (Gemini quota). Time: ~3 min/summary, capped at 3/run.
    if include_notebooklm and new_summaries:
        _progress('NotebookLM: יוצר חידונים וכרטיסיות לסיכומים חדשים...', 0.89)

        def _notebooklm_autogen():
            from generators.notebooklm_agent import NotebookLMAgent
            from exporters.notebooklm_obsidian import export_artifact
            from config.paths import COURSE_NAMES

            if not NotebookLMAgent.is_authenticated():
                return {'items': 0, 'cost_usd': 0.0,
                        'note': 'NotebookLM not authenticated — run `notebooklm login` once.'}

            agent = NotebookLMAgent()
            n_artifacts = 0
            errors: list[str] = []

            # Cap at 3/run so a busy day doesn't stall the pipeline.
            for entry in new_summaries[:3]:
                course_key = entry['course_key']
                title      = entry['title']
                srt_path   = entry['srt_path']
                scope = ('lectures_only' if entry['item_type'] == 'lecture'
                         else 'tutorials_only')
                course_name = COURSE_NAMES.get(course_key, course_key)
                today = datetime.now().strftime('%Y-%m-%d')

                # 1. Push the new SRT as a source on the per-course/scope notebook.
                try:
                    agent.sync_sources(
                        course_key, scope,
                        srt_files=[srt_path],
                    )
                except Exception as e:
                    errors.append(f'sync_sources({course_key}/{scope}): {e}')
                    continue

                # 2. Quiz (10 medium questions, hinted at this lecture's topic).
                try:
                    quiz = agent.generate_quiz(
                        course_key, scope,
                        topic_hint=title,
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
                    n_artifacts += 1
                except Exception as e:
                    errors.append(f'quiz({course_key}/{title}): {e}')

                # 3. Flashcards (deck of cards for the same scope).
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
                    n_artifacts += 1
                except Exception as e:
                    errors.append(f'flashcards({course_key}/{title}): {e}')

            return {
                'items':    n_artifacts,
                'cost_usd': 0.0,
                'errors':   errors,
                'capped_at': 3,
                'pending':   max(0, len(new_summaries) - 3),
            }

        _step('notebooklm_autogen', _notebooklm_autogen, results, budget_left)
        _progress('NotebookLM סונכרן', 0.895)

    # ─── Step 5c: Weekly homework Study Guide — FREE, idempotent ───────
    # Generates one per course where assignments arrived this week. Skips
    # silently if a guide for this ISO week already exists in the vault.
    if include_notebooklm:
        _progress('NotebookLM: בונה מדריכי שיעורי בית שבועיים...', 0.897)

        def _weekly_study_guide():
            from pipelines.weekly_study_guide import generate_weekly_study_guides
            r = generate_weekly_study_guides(window_days=7) or {}
            return {
                'items':    len(r.get('generated', [])),
                'cost_usd': 0.0,
                'detail':   r,
            }

        _step('weekly_study_guide', _weekly_study_guide, results, budget_left)
        _progress('מדריכי שיעורי בית עודכנו', 0.899)

    # ─── Step 6: Build RAG index (OpenAI embeddings, ~$0.005) ──────────
    _progress('בונה אינדקס RAG (embeddings)...', 0.90)

    def _embed():
        from rag.vault_indexer import build_index
        r = build_index(force=False) or {}
        return {
            'items':    r.get('chunks', r.get('n_chunks', 0)) or 0,
            'cost_usd': r.get('cost_usd', 0.0) or 0.0,
            'detail':   r,
        }

    _step('rag_embed_index', _embed, results, budget_left)
    _progress('אינדקס נבנה', 0.95)

    # ─── Step 6b: Weekly digest (free, idempotent) ─────────────────────
    _progress('כותב דיגסט שבועי...', 0.955)

    def _weekly_digest():
        from pipelines.weekly_digest import write_weekly_digest
        path = write_weekly_digest()
        return {'items': 1, 'cost_usd': 0.0, 'path': str(path)}

    _step('weekly_digest', _weekly_digest, results, budget_left)

    # ─── Step 7: Refresh Obsidian graph config (free) ──────────────────
    _progress('מעדכן גרף Obsidian...', 0.96)

    def _graph():
        from vault_sync.graph_config import write as _write_graph
        out = _write_graph()
        return {'items': 1, 'cost_usd': 0.0, 'path': str(out)}

    _step('obsidian_graph_config', _graph, results, budget_left)

    # ─── Wrap up ─────────────────────────────────────────────────────────
    finished = datetime.now()
    total_cost = sum(((s.get('cost_usd') or 0.0) for s in results))
    total_dur = (finished - started).total_seconds()
    n_new = sum(((s.get('items') or 0) for s in results))

    if preview:
        summary_text = (
            f"👁 תצוגה מקדימה: {n_new} פריטים יתווספו, "
            f"~${total_cost:.3f}, {total_dur:.0f} שניות (לא נכתב כלום יקר)"
        )
    else:
        summary_text = (
            f"✅ סנכרון הושלם: {n_new} פריטים חדשים, "
            f"${total_cost:.3f} נצברו, {total_dur:.0f} שניות"
        )

    log: list = []
    if SYNC_LOG_PATH.exists():
        try:
            log = json.loads(SYNC_LOG_PATH.read_text(encoding='utf-8'))
        except Exception:
            log = []
    log.append({
        'started_at':       started.isoformat(),
        'finished_at':      finished.isoformat(),
        'total_cost_usd':   round(total_cost, 6),
        'total_duration_s': round(total_dur, 1),
        'n_new':            n_new,
        'budget_usd':       budget_usd,
        'preview':          preview,
        'force':            force,
        'steps':            results,
    })
    log = log[-50:]
    SYNC_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    SYNC_LOG_PATH.write_text(
        json.dumps(log, ensure_ascii=False, indent=2, default=str),
        encoding='utf-8',
    )

    _progress(summary_text, 1.0)
    return {
        'steps':            results,
        'total_cost_usd':   round(total_cost, 6),
        'total_duration_s': round(total_dur, 1),
        'n_new':            n_new,
        'started_at':       started.isoformat(),
        'finished_at':      finished.isoformat(),
        'summary_text':     summary_text,
        'preview':          preview,
    }


if __name__ == '__main__':
    import sys
    headless = '--head' not in sys.argv
    print(json.dumps(
        sync_all(headless=headless),
        ensure_ascii=False,
        indent=2,
        default=str,
    ))
