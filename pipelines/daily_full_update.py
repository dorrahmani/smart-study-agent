"""
pipelines/daily_full_update.py
================================
Full daily run — everything in turn:

  1. Panopto-first discovery + SRT (free)
  2. Whisper for everything without a good Hebrew SRT (~$0.18/lecture)
  3. Moodle: PDFs + auto-discovery of assignments
  4. New summaries (Gemini, ~$0.30/lecture)
  5. Email the new summaries (with sent_log)
  6. Export to the Obsidian vault + relinker
  7. NotebookLM: sync new files to the notebooks
  8. Brain enrichment: more concepts → rich content with Gemini Flash

Maximum cost: $5 as a cap. If the quota is exhausted — we stop.
Log: data/daily_full_log.txt

Run:  python -m pipelines.daily_full_update
"""
from __future__ import annotations
import os, sys, json, traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try: sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except: pass

LOG = ROOT / 'data' / 'daily_full_log.txt'
LOG.parent.mkdir(exist_ok=True)

def log(msg: str):
    line = f'[{datetime.now().strftime("%H:%M:%S")}] {msg}'
    print(line, flush=True)
    # run_daily.sh already appends our stdout to LOG (it must: collector prints
    # like PIPELINE-WARN only reach the log that way) — writing too doubled every line.
    if os.environ.get('SSA_STDOUT_IS_LOG') == '1':
        return
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(line + '\n')


FAILED_STEPS: list[str] = []


def step(name: str, fn, *args, **kwargs):
    log(f'\n══════ {name} ══════')
    try:
        result = fn(*args, **kwargs)
        log(f'✅ {name} done')
        return result
    except Exception as e:
        log(f'❌ {name} failed: {e}')
        traceback.print_exc()
        FAILED_STEPS.append(name.split(':')[0])
        return None


def _stamp(**fields):
    # dead-man switch read by the morning briefing (utils/pipeline_health.py)
    try:
        from utils.pipeline_health import record_run
        record_run(ROOT / 'data', **fields)
    except Exception as e:
        log(f'  ⚠️ run stamp failed: {e}')


def main(budget_usd: float = 5.0):
    LOG.write_text(f'═══════════════════════════════════\n🌅 Daily full update started: {datetime.now()}\n═══════════════════════════════════\n', encoding='utf-8')
    log(f'Budget: ${budget_usd:.2f}')
    _stamp(started=datetime.now().isoformat(timespec='seconds'))

    from utils.env import _load_env_dotfile
    _load_env_dotfile()
    if os.environ.get('GEMINI_API_KEY'):
        os.environ.pop('GOOGLE_API_KEY', None)

    # 0: lecturers who record to YouTube instead of Panopto (18/10: probability had only a Panopto placeholder) —
    # the week's class recording → an SRT the summaries below pick up like any other
    def _lecturer_recordings():
        from collectors.lecturer_site import fetch_class_recordings, homework_sync, HOMEWORK
        made = fetch_class_recordings()
        log(f'  → class recordings transcribed: {len(made)}')
        for ck in HOMEWORK:                             # exercises on the lecturer's site → deadlines (19/10)
            log(f'  → {ck} site exercises new: {len(homework_sync(ck))}')
        from collectors.course_files import sync as files_sync    # every course's Moodle slides / notes (19/10)
        from collectors.moodle_collector import MoodleCollector
        from config.paths import ACTIVE_COURSES
        sess = MoodleCollector()._login_and_get_session()
        for ck in ACTIVE_COURSES:
            log(f'  → {ck} new Moodle files: {len(files_sync(ck, session=sess))}')
    step('0: lecturer class recordings (YouTube)', _lecturer_recordings)

    # 1+2+4+6: standard sync_all
    def _sync_all():
        from pipelines.sync_all import sync_all
        return sync_all(
            budget_usd=budget_usd,
            include_panopto=True,
            include_whisper=True,
            include_summaries=True,
            include_notebooklm=False,  # we do it explicitly below
            headless=True,
        )
    res = step('1-6: sync_all (Panopto+Whisper+Moodle+Summaries+Obsidian)', _sync_all)
    if res:
        log(f'  → {res.get("summary_text", "")[:300]}')

    # 1b: Backfill catch-up (anti-starvation) — fill ANY good Hebrew SRT that
    # sync_all's per-run cap (todo[:5], newest-first) left without a summary.
    # Oldest-first so nothing starves. $0 on normal days (only spends on a gap).
    def _backfill():
        from pipelines.backfill_summaries import backfill_missing_summaries
        r = backfill_missing_summaries(budget_usd=4.0, max_items=15, max_age_days=7)
        log(f'  → backfill: scanned={r["scanned"]} missing={r["missing"]} '
            f'generated={r["generated"]} skipped_bad={r["skipped_bad_srt"]} '
            f'cost=${r["cost_usd"]:.2f}')
        for c in r.get('created', []):
            log(f'     ✅ {c["course_key"]}: {c["title"]}')
        # sync_all's vault sweep already ran before this step, so mirror any
        # freshly-created summaries into the vault now.
        if r.get('generated'):
            try:
                from exporters.obsidian_exporter import sync_all as _vs
                vr = _vs(force=False) or {}
                log(f'  → vault re-sync after backfill: {vr.get("summaries", 0)} summaries')
            except Exception as e:
                log(f'  ⚠️ vault re-sync failed: {e}')
        return r
    step('1b: backfill missing summaries (anti-starvation)', _backfill)

    # 1c (20/10): lectures nobody recorded (micro / macro aren't on Panopto) → a summary from the Moodle slides
    def _slides():
        from pipelines.slides_summaries import run as _sr
        r = _sr(log=log)
        log(f'  → slides summaries: generated={r.get("generated", 0)} cost=${r.get("cost_usd", 0):.2f}'
            + (f' · no slides yet: {", ".join(r["no_slides"][:4])}' if r.get('no_slides') else ''))
        if r.get('generated'):
            try:
                from exporters.obsidian_exporter import sync_all as _vs
                _vs(force=False)
            except Exception as e:
                log(f'  ⚠️ vault re-sync failed: {e}')
        return r
    step('1c: summaries from slides (lectures without a recording)', _slides)

    # 3b: my new auto-discover scraper (overrides hardcoded)
    def _scrape_assignments():
        from collectors.moodle_assignment_scraper import scrape_all
        return scrape_all()
    step('3b: auto-discover Moodle assignments', _scrape_assignments)

    # 3b2: download assignment PDFs (so the homework checker has the questions)
    def _download_assignment_pdfs():
        import subprocess as _sub, sys as _sys
        script = ROOT / 'scripts' / 'download_assignments.py'
        if not script.exists():
            log('  → download_assignments.py not found, skipping')
            return
        r = _sub.run(
            [_sys.executable, str(script)],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            cwd=str(ROOT), timeout=300,
        )
        for line in (r.stdout or '').splitlines():
            if line.strip(): log(f'  {line}')
        if r.returncode != 0:
            log(f'  ⚠️ exit={r.returncode}')
    step('3b2: download assignment PDFs from Moodle', _download_assignment_pdfs)

    def _export_assignments():
        from exporters.assignment_obsidian import export_assignments
        n = export_assignments(force=True)
        log(f'  → exported {n} assignments to vault')
    step('3c: export assignments to vault', _export_assignments)

    # 7: NotebookLM sync
    def _nlm_sync():
        from generators import notebooklm_agent as nlm
        agent = nlm.NotebookLMAgent()
        idx = nlm._load_notebooks_index()
        total_added = 0
        failed_uploads = []      # 13/11: an expired login turned every upload into a quiet "skipped" — the log said "+0"
        # 15/10: only courses that already had a notebook were synced — the new semester's never got one.
        # An active course without a notebook gets 'all' once it has material (no empty notebooks).
        from config.paths import ACTIVE_COURSES
        for ck in list(idx.keys()) + [k for k in ACTIVE_COURSES if k not in idx]:
            for scope in nlm.all_scopes_for_course(ck) or ['all']:
                if scope == 'lectures_only':
                    pdfs = nlm.list_summary_pdfs(ck, 'lectures')
                    srts = nlm.list_srt_files(ck, 'lectures')
                    moodle = []
                    texts = []
                elif scope == 'tutorials_only':
                    pdfs = nlm.list_summary_pdfs(ck, 'tutorials')
                    srts = nlm.list_srt_files(ck, 'tutorials')
                    moodle = []
                    texts = []
                else:
                    pdfs = nlm.list_summary_pdfs(ck, 'lectures') + nlm.list_summary_pdfs(ck, 'tutorials')
                    srts = nlm.list_srt_files(ck, 'all')
                    moodle = nlm.list_moodle_pdfs(ck)
                    texts = nlm.list_moodle_texts(ck)
                if ck not in idx and not (pdfs or srts or moodle):
                    continue                                    # nothing yet — don't create an empty notebook
                try:
                    r = agent.sync_sources(course_key=ck, scope=scope, summary_pdfs=pdfs, moodle_pdfs=moodle,
                                           srt_files=srts, text_files=texts)
                    added = len(r.get('added', []))
                    total_added += added
                    if added: log(f'  + {ck}/{scope}: +{added}')
                    errs = [x for x in r.get('skipped', []) if '(ERROR' in str(x)]
                    if errs:
                        failed_uploads += errs
                        log(f'  ⚠️ {ck}/{scope}: {len(errs)} new sources failed to upload — {str(errs[0])[:160]}')
                    if r.get('stale'):
                        log(f"  ⚠️ {ck}/{scope}: {len(r['stale'])} moved-aside sources still in the notebook "
                            f"(scripts/nlm_remove_moved_aside.py)")
                except Exception as e:
                    log(f'  ⚠️ {ck}/{scope}: {e}')
        # 15/11 every course's homework (sheets, official solutions, his feedback, quiz reviews) → its 'all' notebook,
        # titled, deduped by content, replaced when a file changed (generators/nlm_coursework)
        from generators import nlm_coursework
        ops = nlm_coursework.Ops(agent)
        for ck in ACTIVE_COURSES:
            try:
                r = nlm_coursework.sync(ck, 'all', ops=ops)
            except Exception as e:
                log(f'  ⚠️ {ck} coursework: {e!r}'[:200])
                continue
            if r['uploaded'] or r['replaced'] or r['legacy_removed'] or r['retired']:
                total_added += len(r['uploaded'])
                log(f"  + {ck}/all coursework: +{len(r['uploaded'])} new, {len(r['replaced'])} replaced, "
                    f"{len(r['legacy_removed']) + len(r['retired'])} old removed → {r['after']} sources"
                    + (' (merged per kind: near the source limit)' if r['bundled'] else ''))
            errs = [x for x in r['errors'] if x != 'no notebook']
            if errs:
                failed_uploads += errs
                log(f'  ⚠️ {ck} coursework: {len(errs)} failed — {errs[0][:160]}')
        # 17/11 every lecture's / tutorial's full transcript + summary under one title per course (generators/nlm_lectures):
        # the old file-name sources renamed, Cyrillic / duplicate transcripts out, a missing one uploaded — never twice
        from generators import nlm_lectures
        for ck in nlm._load_notebooks_index():
            try:
                r = nlm_lectures.sync(ck, 'all', ops=nlm_lectures.Ops(agent))
            except Exception as e:
                log(f'  ⚠️ {ck} lectures: {e!r}'[:200])
                continue
            total_added += len(r['uploaded'])
            if r['uploaded'] or r['renamed'] or r['removed']:
                log(f"  + {ck}/all lectures: +{len(r['uploaded'])} new, {len(r['renamed'])} renamed, "
                    f"{len(r['removed'])} bad/duplicate removed → {r['after']} sources"
                    + (f" (per week: {len(r['bundled'])})" if r['bundled'] else ''))
            errs = [x for x in r['errors'] if x != 'no notebook']
            if errs:
                failed_uploads += errs
                log(f'  ⚠️ {ck} lectures: {len(errs)} failed — {errs[0][:160]}')
        log(f'  → NotebookLM: total +{total_added} sources'
            + (f', {len(failed_uploads)} failed (tried again next night)' if failed_uploads else ''))
        if failed_uploads:
            from utils.pipeline_health import pipeline_warn
            login = any(w in str(x) for x in failed_uploads for w in ('Auth', 'login', 'CSRF', 'signed-out'))
            if login:       # 16/11 19:21 the same expired login nlm_keepalive had already told him about (and fixes)
                log('  (login expired — notebooklm login: scripts/nlm_keepalive.py owns that alert, not the health report)')
            else:
                pipeline_warn('NotebookLM: סיכומים ותמלולים חדשים לא עלו למחברות')
    step('7: NotebookLM sync', _nlm_sync)

    # 8: Brain enrichment (concept content + lecture chains + co-occurrence)
    def _brain_enrich():
        from scripts.enhance_brain import lecture_chain, concept_cooccurrence, enrich_concept_content
        lecture_chain()
        concept_cooccurrence()
        enrich_concept_content(skip_existing=True)
    step('8: brain enrichment (chains + co-occurrence + LLM concepts)', _brain_enrich)

    # 9: Obsidian enhancements (graph, MOCs, dashboard, bridges)
    def _enhance_vault():
        from scripts.enhance_obsidian import (
            enhance_graph_json, build_course_mocs, build_home_dashboard,
            inject_concept_wikilinks_into_lectures, build_concept_bridges,
            build_concept_index, detect_concept_duplicates, enrich_weekly_digests,
        )
        notebook_ids = json.loads((ROOT / 'config' / 'notebooklm_notebooks.json').read_text(encoding='utf-8')) \
            if (ROOT / 'config' / 'notebooklm_notebooks.json').exists() else {}
        enhance_graph_json()
        build_course_mocs(notebook_ids)
        build_home_dashboard()
        inject_concept_wikilinks_into_lectures()
        build_concept_bridges()
        build_concept_index()
        detect_concept_duplicates()
        enrich_weekly_digests()
    step('9: vault enhancements (graph + MOCs + dashboard)', _enhance_vault)

    # 9b: Email new summaries (uses sent_log to avoid duplicates)
    def _email_new_summaries():
        import json as _json
        from generators.gmail_sender import GmailSender
        from generators.pdf_generator import markdown_to_pdf
        from config.paths import SUMMARIES_DIR, COURSE_NAMES

        from config.paths import owner_email
        recipient = owner_email()
        if not recipient:
            raise RuntimeError('no RECIPIENT_EMAIL / settings.email.recipient — not sending')
        sent_log_path = ROOT / 'config' / 'sent_log.json'
        sent_log_path.parent.mkdir(exist_ok=True)
        sent = {}
        if sent_log_path.exists():
            try: sent = _json.loads(sent_log_path.read_text(encoding='utf-8'))
            except: sent = {}

        # Find MD summaries that haven't been emailed yet
        candidates = []
        for ck in COURSE_NAMES:
            for kind in ('lectures', 'tutorials'):
                d = SUMMARIES_DIR / ck / kind
                if not d.exists(): continue
                for md in d.glob('*.md'):
                    if md.stat().st_size < 5000: continue
                    key = str(md.resolve())
                    if key in sent: continue
                    candidates.append((ck, kind, md))

        if not candidates:
            log('  → no new summaries to email')
            return

        # ── content-based freshness gate (added 20/07/2026) ──
        # Never send a summary for a lecture older than N days. Automatically handles any "dry spell":
        # mid-semester break, a holiday, a war week, and the end of the semester — without a fixed date.
        # Manual override: SMART_STUDY_FORCE=1.
        import re as _re
        from datetime import date as _date_cls, datetime as _dt
        max_age = 14
        try:
            import yaml as _yaml
            _cfg = _yaml.safe_load((ROOT / 'config' / 'courses.yaml').read_text(encoding='utf-8'))
            max_age = int(((_cfg.get('settings') or {}).get('schedule') or {}).get('max_send_age_days', 14))
        except Exception:
            pass

        def _age_days(md):
            m = _re.search(r'(\d{4}-\d{2}-\d{2})', md.stem)
            if not m:
                return None
            try:
                return (_date_cls.today() - _dt.strptime(m.group(1), '%Y-%m-%d').date()).days
            except ValueError:
                return None

        if os.environ.get('SMART_STUDY_FORCE') != '1':
            fresh = []
            for ck, kind, md in candidates:
                a = _age_days(md)
                if a is not None and a > max_age:
                    continue
                fresh.append((ck, kind, md))
            skipped = len(candidates) - len(fresh)
            if skipped:
                log(f'  → שער רעננות: דילג {skipped} סיכומים ישנים מ-{max_age} ימים (יובש/מבחנים)')
            candidates = fresh
            if not candidates:
                log(f'  → אין הרצאה חדשה ב-{max_age} הימים האחרונים — לא נשלח כלום')
                return

        # Sort by date desc, cap to 10 per run (safety)
        def _date(md):
            import re
            m = re.search(r'(\d{4}-\d{2}-\d{2})', md.stem)
            return m.group(1) if m else ''
        candidates.sort(key=lambda x: _date(x[2]), reverse=True)
        candidates = candidates[:10]

        gmail = GmailSender()
        ok_count = 0
        for ck, kind, md in candidates:
            course_name = COURSE_NAMES.get(ck, ck)
            pdf_path = md.with_suffix('.pdf')
            # Generate PDF if missing
            if not pdf_path.exists():
                try:
                    md_text = md.read_text(encoding='utf-8')
                    if '\n---\n\n' in md_text:
                        from utils.other_group import keep
                        md_text = keep(md_text, md_text.split('\n---\n\n', 1)[-1])
                    markdown_to_pdf(md_text, str(pdf_path),
                                    title=md.stem, course_name=course_name)
                    log(f'  📄 PDF created: {md.stem}')
                except Exception as e:
                    log(f'  ⚠️ PDF failed for {md.stem}: {e}')
            try:
                from utils.other_group import md_has, TAG     # 14/11: another group's recording says so
                ok = gmail.send_summary(
                    course_name=course_name,
                    lecture_title=md.stem + (f' {TAG}' if md_has(md) else ''),
                    summary_md_path=str(md),
                    summary_pdf_path=str(pdf_path) if pdf_path.exists() else None,
                    recipient=recipient,
                )
                if ok:
                    sent[str(md.resolve())] = datetime.now().isoformat()
                    ok_count += 1
                    log(f'  📧 sent: {ck}/{md.stem}')
                else:
                    log(f'  ❌ send failed: {md.stem}')
            except Exception as e:
                log(f'  ⚠️ {md.stem}: {e}')
        # Save sent log
        sent_log_path.write_text(_json.dumps(sent, ensure_ascii=False, indent=2), encoding='utf-8')
        log(f'  → emailed {ok_count}/{len(candidates)} new summaries')
    step('9b: email new summaries (with PDF)', _email_new_summaries)

    # 9c (Telegram push of new summaries) removed 10/11/2026 — Telegram bot deleted; 9d sends each new summary
    # on WhatsApp.

    # 9d: WhatsApp — 13/10: summaries went only to email + Telegram, while the owner lives on WhatsApp. Each new summary's
    # PDF, once (data/wa_summaries_sent.json), with a one-line caption. No LLM.
    def _whatsapp_push_new_summaries():
        from whatsapp.summary_push import push_new
        push_new(log)
    step('9d: WhatsApp push for new summaries', _whatsapp_push_new_summaries)

    # 9d: Rebuild RAG index over the vault (embeddings + TF-IDF fallback)
    # Cheap (~$0.005) and ensures the bot answers from the latest content.
    def _rebuild_rag_index():
        from rag.vault_indexer import build_index
        r = build_index(force=False) or {}
        n_chunks = r.get('chunks', r.get('n_chunks', 0)) or 0
        model = r.get('model', '?')
        cached = r.get('cached', False)
        cost = r.get('cost_usd', 0.0)
        if cached:
            log(f'  📚 RAG index up-to-date ({n_chunks} chunks, {model})')
        else:
            log(f'  📚 RAG index rebuilt: {n_chunks} chunks, {model}, ${cost:.4f}')
    step('9d: rebuild RAG index for /ask', _rebuild_rag_index)

    # 9e: Generate iCal feed of deadlines for iPhone/Google Calendar subscription
    def _rebuild_ical():
        from generators.ical_export import build_ics
        p = build_ics()
        size_kb = round(p.stat().st_size / 1024, 1)
        log(f'  📅 iCal feed rebuilt: {p} ({size_kb} KB)')
    step('9e: rebuild iCal deadlines feed', _rebuild_ical)

    # 9f: Build per-course formula cheatsheets (1-page exam-eve reference)
    def _build_cheatsheets():
        from generators.cheatsheet_builder import build_all_cheatsheets
        results = build_all_cheatsheets(force=False)
        n_built = sum(1 for v in results.values() if v)
        log(f'  📋 Cheatsheets: {n_built}/{len(results)} courses (cached unless new summaries)')
    step('9f: rebuild per-course cheatsheets', _build_cheatsheets)

    # 9g: Build per-course comprehensive summaries (3-5 page exam study material)
    def _build_course_summaries():
        from generators.course_summary_builder import build_all
        results = build_all(force=False)
        n_built = sum(1 for v in results.values() if v)
        log(f'  📚 Course summaries: {n_built}/{len(results)} courses (cached unless new summaries)')
    step('9g: rebuild per-course comprehensive summaries', _build_course_summaries)

    # 9h: the private study site — a page per course (lessons, homework done / not, exam topics, past exams)
    def _build_site():
        from scripts.build_course_site import build, _topic_ask
        r = build(topics={'ask': _topic_ask})      # the night writes a few topics; the rest of the day's rebuilds never do
        log(f"  🌐 study site: {r['courses']} courses, {r['lessons']} lessons → {r['url']}")
    step('9h: rebuild the study site', _build_site)

    # 10: Drive sync - upload all new/changed data to Google Drive
    def _drive_sync():
        import subprocess, sys
        if sys.platform == 'win32':
            sync_script = ROOT / 'scripts' / 'drive_sync.ps1'
            cmd = ['powershell.exe', '-ExecutionPolicy', 'Bypass', '-File', str(sync_script), 'up']
        else:
            sync_script = ROOT / 'scripts' / 'sync_after.sh'
            cmd = ['bash', str(sync_script)]
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            cwd=str(ROOT)
        )
        output = (result.stdout or '') + (result.stderr or '')
        for line in output.splitlines():
            if line.strip():
                log(f'  [drive] {line}')
        if result.returncode != 0:
            raise RuntimeError(f'drive_sync exited with code {result.returncode}')
    step('10: Drive sync (local -> gdrive:smart-study-agent/data)', _drive_sync)

    # 11: Final health report — detect Panopto items that didn't make it to a summary
    def _pipeline_health_report():
        """Scan log for failures (Whisper quota, Cyrillic SRT, missing summaries)
        and send a single consolidated Telegram alert if anything's wrong."""
        from pathlib import Path as _P
        import re as _re_h

        log_path = _P('data/daily_full_log.txt')
        if not log_path.exists(): return

        text = log_path.read_text(encoding='utf-8', errors='replace')
        issues: list[str] = []

        # Silent failures (exit 0, no summary, no alert) — first, they matter most.
        # 1) explicit markers from the collector: schedule filter dropped every
        #    recording, folder_id still a placeholder, a course crashed.
        from utils.pipeline_health import extract_pipeline_warnings, stale_courses
        for w in extract_pipeline_warnings(text)[:12]:
            issues.append(f'🚨 {w}')
        # 1b) secrets must stay 600 (tokens, .env, classmates' emails)
        from utils.pipeline_health import insecure_secret_files
        _bad = insecure_secret_files(ROOT)
        if _bad:
            issues.append('🔐 קבצי סודות פתוחים לקריאה: ' + ', '.join(_bad[:4]) + ' — chmod 600')
        # 2) catch-all: an active course with no new summary for 10 days.
        try:
            from datetime import date as _date
            import yaml as _yaml
            from config.paths import SUMMARIES_DIR, ACTIVE_COURSES, COURSE_NAMES
            _cfg = _yaml.safe_load(open(ROOT / 'config' / 'courses.yaml', encoding='utf-8')) or {}
            _sched = (_cfg.get('settings') or {}).get('schedule') or {}
            _s = _sched.get('semester_start'); _e = _sched.get('semester_end')
            if _s:
                _as_date = lambda v: v if isinstance(v, _date) else _date.fromisoformat(str(v))
                for ck, last in stale_courses(SUMMARIES_DIR, ACTIVE_COURSES, _date.today(),
                                              _as_date(_s), _as_date(_e) if _e else None):
                    since = f'מאז {last:%d.%m}' if last else 'מתחילת הסמסטר'
                    issues.append(f'🚨 *{COURSE_NAMES.get(ck, ck)}:* אין סיכום חדש {since} — '
                                  f'לבדוק folder_id / סינון קבוצה / Panopto')
        except Exception as _e2:
            log(f'  ⚠️ stale-course check failed: {_e2}')

        # Detect Whisper quota exhaustion (429)
        if _re_h.search(r'insufficient_quota|429.*Too Many Requests', text):
            issues.append('⛔ *Whisper quota מוצה* — הוסף קרדיט ב-OpenAI billing.')

        # Detect Cyrillic SRT files left without summaries
        bad_srt_files = _re_h.findall(
            r'הועבר ל-(\S+\.srt\.bad)', text)
        if bad_srt_files:
            for fname in set(bad_srt_files):
                # Check if Whisper produced an alternative AND if a summary exists
                stem = _re_h.sub(r'\.srt\.bad$', '', fname).split('/')[-1]
                # Look for a matching summary
                summary_glob = list(_P('data/summaries').rglob(f'{stem.split("_",2)[-1]}*.md'))
                if not summary_glob:
                    issues.append(f'⚠️ SRT קירילי לא הוחלף בתמלול עברי: `{fname}`')

        # Detect Gemini errors during summarization
        if _re_h.search(r'(429|RESOURCE_EXHAUSTED).*generativelanguage', text):
            issues.append('⛔ *Gemini quota* — חרגנו ממכסת קריאות.')

        # Detect pipeline-level errors logged as ❌
        explicit_errors = _re_h.findall(r'❌[^\n]{5,150}', text)
        # Filter out the noisy "HLS error 429" lines (already handled above)
        explicit_errors = [e for e in explicit_errors
                           if 'HLS' not in e and 'Cooldown' not in e][:5]
        for e in explicit_errors:
            issues.append(f'⚠️ {e.strip()[:120]}')

        # Anti-starvation guard: any good Hebrew SRT still without a summary?
        try:
            from pipelines.backfill_summaries import backfill_missing_summaries
            gap = backfill_missing_summaries(dry_run=True, max_age_days=7)
            for g in gap.get('gaps', [])[:5]:
                issues.append(f"⚠️ הרצאה ללא סיכום: `{g['course_key']} {g['date']} {g['title']}`")
        except Exception as _e:
            log(f'  ⚠️ gap-check failed: {_e}')

        # Class-day sanity: new transcripts today but ZERO new summaries —
        # something is silently blocking (budget lock, API failure, etc.)
        try:
            from datetime import datetime as _dth
            _now = _dth.now()
            if _now.weekday() in (6, 0, 1, 2, 3):  # Sun-Thu (Israel class days)
                _new_srt = [p for p in _P('data/panopto').glob('*.srt')
                            if _dth.fromtimestamp(p.stat().st_mtime).date() == _now.date()]
                _new_md = [p for p in _P('data/summaries').rglob('*.md')
                           if _dth.fromtimestamp(p.stat().st_mtime).date() == _now.date()]
                if _new_srt and not _new_md:
                    issues.append(f'⛔ *יום לימודים:* {len(_new_srt)} תמלולים חדשים היום אבל 0 סיכומים נוצרו — משהו חוסם (תקציב? שגיאת API?).')
        except Exception as _e:
            log(f'  ⚠️ zero-summaries check failed: {_e}')

        if not issues:
            log('  ✅ Pipeline health: כל הסיכומים מולאו, אין כשלים')
            return

        for i in issues:
            log(f'    - {i}')
        from utils.pipeline_health import fresh_issues     # 10/11: the same warnings aren't re-sent every night
        issues = fresh_issues(issues, ROOT / 'data' / 'pipeline_health_seen.json')
        if not issues:
            log('  (all of these were already reported this week — not sent again)')
            return
        msg = '⚠️ *דוח בריאות pipeline*\n\n' + '\n'.join(f'• {i}' for i in issues[:15])
        if len(issues) > 15: msg += f'\n_(ועוד {len(issues)-15})_'
        from utils.notify import deliver            # WhatsApp only (10/11: Telegram deleted)
        if deliver(msg, markdown=True):
            log(f'  📲 Pipeline health alert sent ({len(issues)} issues)')
            fresh_issues(issues, ROOT / 'data' / 'pipeline_health_seen.json', mark=True)
        else:
            log('  ⚠️ WhatsApp alert failed (kept in data/undelivered_alerts.jsonl)')
    step('11: pipeline health report', _pipeline_health_report)

    _stamp(finished=datetime.now().isoformat(timespec='seconds'), failed_steps=FAILED_STEPS)
    log(f'\n═══════════════════════════════════\n🌅 Daily full update DONE: {datetime.now()}\n═══════════════════════════════════')


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--budget', type=float, default=5.0)
    args = p.parse_args()
    main(budget_usd=args.budget)
