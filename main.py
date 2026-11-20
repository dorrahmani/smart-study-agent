#!/usr/bin/env python3
"""
Smart Study Agent - Main Script
Runs the whole system: collection, processing, summarizing and sending
"""

import os
import sys
import io
import re
from pathlib import Path
from datetime import datetime

# Fix Windows console encoding for Hebrew + emoji
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except AttributeError:
    pass
import yaml
import argparse
from typing import List, Dict, Optional

# add the main folder to the path
sys.path.insert(0, str(Path(__file__).parent))

from collectors.panopto_collector import PanoptoCollector
from collectors.moodle_collector import MoodleCollector
from generators.summarizer import AISummarizer
from generators.gmail_sender import GmailSender


# The semester gate lives in utils/semester.py (shared with scripts/run_daily.sh).
from utils.semester import semester_active  # noqa: E402,F401


class SmartStudyAgent:
    def __init__(self, config_path: str = "config/courses.yaml"):
        print("🚀 Smart Study Agent מתחיל...")
        
        self.config_path = config_path
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)
        
        # initialize modules
        self.panopto = PanoptoCollector(config_path)
        self.moodle = MoodleCollector(config_path)
        self.summarizer = AISummarizer()
        self.mailer = GmailSender()

        # log in to Panopto once
        self.panopto.login()
        
        self.output_dir = Path("data/summaries")
        self.output_dir.mkdir(parents=True, exist_ok=True)
    
    def collect_materials(self, course_key: str) -> Dict:
        """Collects materials for a course"""
        print(f"\n{'='*60}")
        print(f"📚 שלב 1: איסוף חומרים עבור {course_key}")
        print(f"{'='*60}\n")

        materials = {
            'course_key': course_key,
            'panopto': [],
            'moodle': []
        }

        # collect from Panopto
        try:
            days_back = self.config['settings']['schedule'].get('days_back', 2)
            panopto_results = self.panopto.collect_course(course_key, days_back=days_back)
            materials['panopto'] = panopto_results
        except Exception as e:
            print(f"⚠️  שגיאה באיסוף מ-Panopto: {e}")

        # collect from Moodle + Wikidot + Static PDF
        try:
            moodle_results = self.moodle.collect_course(course_key)
            materials['moodle'] = moodle_results
        except Exception as e:
            print(f"⚠️  שגיאה באיסוף מ-Moodle: {e} — מנסה להשתמש בקבצים שכבר הורדו")
            materials['moodle'] = self._load_cached_moodle(course_key)

        return materials

    def _load_cached_moodle(self, course_key: str) -> List[Dict]:
        """Fallback: loads files already downloaded from Moodle from disk (when there is a timeout)."""
        import json as _json
        cached = []

        # try to load from the metadata JSON saved in previous runs
        meta_path = Path(f"data/moodle/{course_key}_metadata.json")
        if meta_path.exists():
            try:
                with open(meta_path, 'r', encoding='utf-8') as f:
                    cached = _json.load(f)
                # make sure the files still exist on disk
                cached = [r for r in cached if r.get('local_path') and Path(r['local_path']).exists()]
                if cached:
                    print(f"  📂 נטענו {len(cached)} קבצים מ-cache מקומי")
                    return cached
            except Exception:
                pass

        # if there is no metadata — scan the folder directly
        moodle_dir = Path(f"data/moodle/{course_key}")
        if moodle_dir.exists():
            for f in moodle_dir.glob("*.pdf"):
                cached.append({
                    'course': course_key,
                    'type': 'moodle',
                    'file_info': {'title': f.stem.replace('_', ' ')},
                    'local_path': str(f),
                })
            if cached:
                print(f"  📂 נסרקו {len(cached)} קבצים מ-{moodle_dir}")

        # static_pdf always
        static_pdf = self.config['courses'][course_key]['sources'].get('static_pdf')
        if static_pdf and Path(static_pdf).exists():
            cached.append({
                'course': course_key,
                'type': 'static_pdf',
                'file_info': {'title': Path(static_pdf).stem},
                'local_path': static_pdf,
            })

        return cached

    def match_pdf_to_video(self, video_title: str, moodle_items: List[Dict],
                           session_index: int = 0, item_type: str = 'lecture') -> tuple:
        """
        Smartly matches a PDF to a video.
        Returns (local_path, source_type) or (None, None).
        source_type: 'moodle' | 'wikidot' | 'static_pdf'
        session_index: position in the list (0-based) — fallback for the number when the title has none
        item_type: 'lecture' | 'tutorial' — defines which PDFs are relevant
"""
        import re

        TUTORIAL_WORDS = {'תרגול', 'תרגיל', 'tutorial', 'recitation', 'exercise'}
        LECTURE_WORDS  = {'הרצאה', 'lecture', 'שיעור', 'shiur'}
        SKIP_WORDS     = {'נהלי', 'סילבוס', 'syllabus', 'אותיות', 'סימונים'}

        def _is_tutorial_pdf(title: str) -> bool:
            t = title.lower()
            return any(w in t for w in TUTORIAL_WORDS)

        def _is_lecture_pdf(title: str) -> bool:
            t = title.lower()
            return any(w in t for w in LECTURE_WORDS)

        def _is_skip_pdf(title: str) -> bool:
            t = title.lower()
            return any(w in t for w in SKIP_WORDS)

        # filter: lectures get PDFs that are not tutorials; tutorials get tutorial PDFs only
        def _is_relevant(item: dict) -> bool:
            if item.get('type') == 'static_pdf':
                return item_type == 'lecture'  # static_pdf for lectures only
            title = item.get('file_info', {}).get('title', '')
            if _is_skip_pdf(title):
                return False
            if item_type == 'tutorial':
                return _is_tutorial_pdf(title)
            else:  # lecture
                return not _is_tutorial_pdf(title)

        relevant_items = [it for it in moodle_items if _is_relevant(it)]

        if not relevant_items:
            print(f"  ⚠️  לא נמצאו PDFs רלוונטיים עבור {item_type}: {video_title}")
            return None, None

        patterns = [
            r'(?:הרצאה|תרגול|תרגיל|lecture|tirgul|recitation)\s*(\d+)',
            r'שיעור\s*(\d+)',
            r'(?:lec|rec)\s*(\d+)',   # lec1, rec2 from the Computer Science wikidot
            r'\((\d+)\)',
            r'\b(\d{1,2})\b',
        ]

        # extract the number from the video title
        video_number = None
        for pattern in patterns:
            match = re.search(pattern, video_title, re.IGNORECASE)
            if match:
                video_number = match.group(1).lstrip('0') or '1'
                break

        if video_number is None:
            video_number = str(session_index + 1)
            print(f"  ℹ️  אין מספר בכותרת '{video_title}' — positional: {video_number}")

        # 1. look for all PDFs with the same number (there can be several — e.g. Discrete Math lecture 3)
        if video_number:
            matched_items = []
            for item in relevant_items:
                if item.get('type') == 'static_pdf':
                    continue
                pdf_title = item.get('file_info', {}).get('title', '')
                for pattern in patterns:
                    m = re.search(pattern, pdf_title, re.IGNORECASE)
                    if m:
                        num = m.group(1).lstrip('0') or '1'
                        if num == video_number:
                            matched_items.append(item)
                            break
            if matched_items:
                paths = [it['local_path'] for it in matched_items]
                source_type = matched_items[0].get('type', 'moodle')
                if len(paths) == 1:
                    print(f"  ✅ התאמה: #{video_number} ↔ {matched_items[0]['file_info']['title']}")
                    return paths[0], source_type
                else:
                    titles = [it['file_info']['title'] for it in matched_items]
                    print(f"  ✅ {len(paths)} PDFs עבור #{video_number}: {titles}")
                    return paths, source_type  # returns a list

        # 2. if there is only one relevant item that is not static_pdf — use it
        non_static = [it for it in relevant_items if it.get('type') != 'static_pdf']
        if len(non_static) == 1:
            item = non_static[0]
            pdf_title = item.get('file_info', {}).get('title', '')
            print(f"  ✅ פריט יחיד: {pdf_title}")
            return item['local_path'], item.get('type', 'moodle')

        # 3. fallback: static_pdf (lectures only)
        for item in relevant_items:
            if item.get('type') == 'static_pdf':
                print(f"  ✅ static PDF: {item['local_path']}")
                return item['local_path'], 'static_pdf'

        # 4. fallback: the closest by number (e.g. lec2 when lec3 hasn't been uploaded yet)
        if video_number:
            numbered = []
            for item in non_static:
                pdf_title = item.get('file_info', {}).get('title', '')
                for pattern in patterns:
                    m = re.search(pattern, pdf_title, re.IGNORECASE)
                    if m:
                        n = int(m.group(1))
                        numbered.append((n, item))
                        break
            if numbered:
                # pick the closest one that does not exceed the video number
                target = int(video_number)
                below = [(n, it) for n, it in numbered if n <= target]
                if below:
                    best_n, best_item = max(below, key=lambda x: x[0])
                    pdf_title = best_item.get('file_info', {}).get('title', '')
                    print(f"  ✅ fallback קרוב: #{target} → #{best_n} ({pdf_title})")
                    return best_item['local_path'], best_item.get('type', 'moodle')

        print(f"  ⚠️  לא נמצא PDF מתאים ל: {video_title}")
        return None, None
    
    def process_and_summarize(self, materials: Dict) -> List[str]:
        """Processes and creates summaries"""
        print(f"\n{'='*60}")
        print(f"🤖 שלב 2: עיבוד וסיכום")
        print(f"{'='*60}\n")
        
        course_key = materials['course_key']
        course_config = self.config['courses'][course_key]
        course_name = course_config['name']
        
        summaries = []
        
        panopto_items = materials.get('panopto', [])
        moodle_items = materials.get('moodle', [])

        # count lectures and tutorials separately (by positional PDF matching)
        # starting from the number of existing summaries, to get the right lecture number even across separate runs
        _lec_dir = Path(f"data/summaries/{course_key}/lectures")
        _tut_dir = Path(f"data/summaries/{course_key}/tutorials")
        lecture_index = len(list(_lec_dir.glob("*.md"))) if _lec_dir.exists() else 0
        tutorial_index = len(list(_tut_dir.glob("*.md"))) if _tut_dir.exists() else 0

        # load feedback notes from the dashboard
        import json as _json
        ratings_path = Path("data/ratings.json")
        ratings_data = {}
        if ratings_path.exists():
            try:
                ratings_data = _json.loads(ratings_path.read_text(encoding='utf-8'))
            except Exception:
                pass

        # summaries from Panopto (transcript + PDF)
        for panopto_item in panopto_items:
            video_title = panopto_item['title']
            item_type = panopto_item.get('type', 'lecture')
            caption_path = panopto_item['srt_path']
            session_date = panopto_item.get('date', '')[:10]  # YYYY-MM-DD

            # path by structure: data/summaries/{course}/{lectures|tutorials}/{date} - {title}.md
            summary_path = AISummarizer.build_summary_path(course_key, item_type, video_title, date=session_date)

            # update the index by type
            current_index = lecture_index if item_type == 'lecture' else tutorial_index
            if item_type == 'lecture':
                lecture_index += 1
            else:
                tutorial_index += 1

            # skip if the summary already exists
            if Path(summary_path).exists():
                print(f"⏭️  סיכום קיים: {summary_path}")
                summaries.append(summary_path)
                continue

            matching_pdf = None
            pdf_source_type = None
            # look for a PDF for both lectures and tutorials
            matching_pdf, pdf_source_type = self.match_pdf_to_video(
                video_title, moodle_items, session_index=current_index, item_type=item_type
            )

            video_path = panopto_item.get('video_path')

            try:
                notebook_offset = course_config.get('notebook_offset', 0)
                # check whether there are dashboard feedback notes for this summary
                type_folder = 'lectures' if item_type == 'lecture' else 'tutorials'
                rating_key = f"{course_key}/{type_folder}/{Path(summary_path).name}"
                feedback_data = ratings_data.get(rating_key, {})
                user_feedback = feedback_data.get('notes', '') if feedback_data.get('feedback_pending') else ''
                if user_feedback:
                    print(f"  📝 נמצאו הערות משוב — מיישם בסיכום")
                    # mark as applied
                    ratings_data[rating_key]['feedback_pending'] = False
                    ratings_path.write_text(_json.dumps(ratings_data, ensure_ascii=False, indent=2), encoding='utf-8')

                # if there is a feedback_pending from a previous summary of the same course — take it
                if not user_feedback:
                    type_folder = 'lectures' if item_type == 'lecture' else 'tutorials'
                    prefix = f"{course_key}/{type_folder}/"
                    for rk, rdata in ratings_data.items():
                        if (rk.startswith(prefix)
                                and rdata.get('feedback_pending')
                                and rdata.get('notes')
                                and rk != rating_key):
                            user_feedback = rdata['notes']
                            print(f"  📝 נמצא משוב מסיכום קודם ({rk.split('/')[-1]}) — מיישם")
                            # mark as applied
                            ratings_data[rk]['feedback_pending'] = False
                            ratings_path.write_text(
                                _json.dumps(ratings_data, ensure_ascii=False, indent=2),
                                encoding='utf-8'
                            )
                            break

                summary, sources = self.summarizer.generate_summary(
                    course_name=course_name,
                    lecture_title=video_title,
                    caption_path=caption_path,
                    pdf_path=matching_pdf,
                    pdf_source_type=pdf_source_type,
                    video_path=video_path,
                    course_key=course_key,
                    notebook_offset=notebook_offset,
                    user_feedback=user_feedback
                )

                # ── do not save error summaries ──────────────────────────────────
                if summary.startswith('# שגיאה'):
                    print(f"❌ הסיכום נכשל (API error) — לא נשמר: {video_title}")
                    continue

                self.summarizer.save_summary(summary, summary_path,
                                             course_name=course_name,
                                             lecture_title=video_title,
                                             sources=sources)

                # ── add professor tips automatically ───────────────────────────
                try:
                    from generators.professor_agent import ProfessorAgent
                    print(f"\n👨‍🏫 פרופסור מוסיף טיפים...")
                    prof = ProfessorAgent()
                    prof_result = prof.add_tips_to_summary(summary_path, course_key)
                    print(f"  💰 פרופסור: {prof_result.get('tokens_in',0):,} in + {prof_result.get('tokens_out',0):,} out = ${prof_result.get('cost_usd',0):.4f}")
                except Exception as e:
                    print(f"  ⚠️  פרופסור נכשל (לא קריטי): {e}")

                # ── sync to NotebookLM automatically ───────────────────────────
                try:
                    from generators.notebooklm_agent import NotebookLMAgent
                    _nlm = NotebookLMAgent()
                    if _nlm.is_authenticated():
                        _nlm_scope = "lectures_only" if item_type == "lecture" else "tutorials_only"
                        _pdf_path  = summary_path.replace(".md", ".pdf")
                        _pdfs_to_sync = [_pdf_path] if Path(_pdf_path).exists() else []
                        # look for SRT — located in data/panopto/{course_key}_{title}.srt
                        _safe_title = re.sub(r'[<>:"/\\|?*]', '', video_title).strip()
                        _srt_path = Path("data/panopto") / f"{course_key}_{_safe_title}.srt"
                        _srt_files = [str(_srt_path)] if _srt_path.exists() else []
                        if _pdfs_to_sync or _srt_files:
                            print(f"\n📓 NotebookLM: מסנכרן PDF={len(_pdfs_to_sync)} SRT={len(_srt_files)}...")
                            _nlm_res = _nlm.sync_sources(
                                course_key, _nlm_scope,
                                summary_pdfs=_pdfs_to_sync,
                                srt_files=_srt_files,
                            )
                            # also sync to the "all" notebook (all the material)
                            _nlm.sync_sources(
                                course_key, "all",
                                summary_pdfs=_pdfs_to_sync,
                                srt_files=_srt_files,
                            )
                            print(f"  ✅ NotebookLM: נוספו {len(_nlm_res['added'])}, נדלגו {len(_nlm_res['skipped'])}")
                        else:
                            print(f"\n📓 NotebookLM: PDF לא נמצא — דילוג")
                    else:
                        print(f"\n📓 NotebookLM: לא מחובר — דילוג (הרץ fix_notebooklm_auth.py)")
                except Exception as e:
                    print(f"  ⚠️  NotebookLM סנכרון נכשל (לא קריטי): {e}")

                summaries.append(summary_path)

            except Exception as e:
                print(f"❌ שגיאה ביצירת סיכום עבור {video_title}: {e}")
            finally:
                # delete the temporary video after summarizing (only if it is a local file, not a URL)
                if video_path and not video_path.startswith('http') and Path(video_path).exists():
                    try:
                        Path(video_path).unlink()
                        print(f"🗑️  וידאו זמני נמחק: {Path(video_path).name}")
                    except Exception:
                        pass

        # if there are no new lectures from Panopto - nothing to send
        if not panopto_items:
            print("ℹ️  אין הרצאות/תרגולים חדשים מ-Panopto - אין מה לשלוח")

        return summaries
    
    def _load_sent_log(self) -> dict:
        """Loads the reviewer send log (config/reviewer_sent_log.json)"""
        import json
        log_path = Path("config/reviewer_sent_log.json")
        if log_path.exists():
            try:
                with open(log_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                pass
        return {}

    def _save_sent_log(self, sent: dict):
        """Saves the reviewer send log"""
        import json
        log_path = Path("config/reviewer_sent_log.json")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, 'w', encoding='utf-8') as f:
            json.dump(sent, f, ensure_ascii=False, indent=2)

    def _load_approval_log(self) -> dict:
        """Loads the approval log (config/approval_log.json)"""
        import json
        log_path = Path("config/approval_log.json")
        if log_path.exists():
            try:
                with open(log_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                pass
        return {}

    def _save_approval_log(self, log: dict):
        import json
        log_path = Path("config/approval_log.json")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, 'w', encoding='utf-8') as f:
            json.dump(log, f, ensure_ascii=False, indent=2)

    def _get_reviewer_email(self) -> str:
        """Returns the reviewer's address from recipients_full.json or from the config"""
        import json as _j
        for rpath in [Path("config/recipients_full.json"), Path("config/recipients.json")]:
            if rpath.exists():
                try:
                    rdata = _j.loads(rpath.read_text(encoding='utf-8'))
                    reviewer = rdata.get('reviewer')
                    if reviewer:
                        return reviewer
                except Exception:
                    pass
        from config.paths import owner_email
        return owner_email()

    @staticmethod
    def _summary_lecture_date(summary_path: str):
        """Extracts the lecture date from the file name prefix (YYYY-MM-DD). None if there is none."""
        from datetime import datetime as _dt
        m = re.search(r'(\d{4}-\d{2}-\d{2})', Path(summary_path).name)
        if not m:
            return None
        try:
            return _dt.strptime(m.group(1), '%Y-%m-%d').date()
        except ValueError:
            return None

    def send_summaries(self, summaries: List[str], course_key: str):
        """
        Step 3: sends new summaries to the reviewer only.
        Marks as 'pending' in approval_log — the dashboard will show an approval button for sending to everyone.
"""
        print(f"\n{'='*60}")
        print(f"📧 שלב 3: שליחת סיכומים")
        print(f"{'='*60}\n")

        course_config = self.config['courses'][course_key]
        course_name = course_config['name']

        reviewer = self._get_reviewer_email()
        sent_log = self._load_sent_log()
        approval_log = self._load_approval_log()
        newly_sent = {}

        max_age = int((self.config.get('settings') or {}).get('schedule', {}).get('max_send_age_days', 14))

        for summary_path in summaries:
            summary_key = str(Path(summary_path)).replace('\\', '/')

            # freshness gate — hard protection: do not send a summary for an old lecture, even if sent_log
            # was reset (as in the cloud cron run with a fresh checkout). This prevents resending
            # all the old summaries on every run — the bug that flooded the lecturer.
            lec_date = self._summary_lecture_date(summary_path)
            if lec_date is not None:
                from datetime import date as _date
                age_days = (_date.today() - lec_date).days
                if age_days > max_age:
                    print(f"⏭️  ישן מדי ({age_days} ימים > {max_age}) — לא נשלח: {summary_key}")
                    continue

            # skip if already sent to the reviewer
            if summary_key in sent_log:
                print(f"⏭️  כבר נשלח: {summary_key}")
                continue

            try:
                stem = Path(summary_path).stem.replace(f"{course_key}_", "", 1)
                for prefix in ('lecture_', 'tutorial_', 'pdf_'):
                    if stem.startswith(prefix):
                        stem = stem[len(prefix):]
                        break
                lecture_title = stem

                pdf_path = summary_path.replace('.md', '.pdf')
                if not Path(pdf_path).exists():
                    pdf_path = None

                print(f"📧 שולח מייל אל: {reviewer}")
                success = self.mailer.send_summary(
                    course_name=course_name,
                    lecture_title=lecture_title,
                    summary_md_path=summary_path,
                    summary_pdf_path=pdf_path,
                    recipient=reviewer
                )

                if success:
                    from datetime import datetime as _dt
                    now_iso = _dt.now().isoformat()
                    newly_sent[summary_key] = now_iso
                    # record in approval_log as pending if not yet approved
                    if summary_key not in approval_log:
                        approval_log[summary_key] = {
                            'status': 'pending',
                            'course_key': course_key,
                            'course_name': course_name,
                            'title': lecture_title,
                            'md_path': summary_path,
                            'pdf_path': pdf_path or '',
                            'sent_to_reviewer': now_iso,
                            'sent_to_all': None,
                        }

            except Exception as e:
                print(f"❌ שגיאה בשליחת {summary_path}: {e}")

        if newly_sent:
            sent_log.update(newly_sent)
            self._save_sent_log(sent_log)
            self._save_approval_log(approval_log)
            print(f"✅ נשלחו {len(newly_sent)} סיכומים חדשים")
            print(f"   ⏳ ממתין לאישורך בדשבורד לפני שליחה לכולם")

            # ── step 3b: a quiz for every new summary ──────────────────────────────
            self.generate_and_send_quizzes(newly_sent, course_key, course_name, reviewer)

    def generate_and_send_quizzes(
        self,
        newly_sent: dict,       # {summary_key: iso_ts}
        course_key: str,
        course_name: str,
        recipient: str,
    ):
        """Generates a separate PDF quiz for every new summary and sends it to the reviewer's email."""
        try:
            from generators.quiz_generator import QuizGenerator
        except ImportError as e:
            print(f"  ⚠️ quiz_generator לא זמין: {e}")
            return

        gen = QuizGenerator()
        for summary_key in newly_sent:
            md_path = Path(summary_key)
            if not md_path.exists():
                # try a relative path
                md_path = Path(summary_key.replace('data/', 'data/'))
            if not md_path.exists():
                print(f"  ⚠️ לא נמצא קובץ לחידון: {summary_key}")
                continue
            try:
                print(f"\n✏️  מייצר חידון עבור: {md_path.stem}")
                result = gen.generate(
                    summary_md_path=str(md_path),
                    course_name=course_name,
                    course_key=course_key,
                    n_questions=6,
                )
                ok = gen.send(result, course_name, recipients=[recipient])
                if ok:
                    print(f"  ✅ חידון נשלח ל-{recipient}")
                else:
                    print(f"  ❌ שליחת חידון נכשלה")
            except Exception as e:
                print(f"  ❌ שגיאה ביצירת חידון: {e}")

    def generate_podcast_for_course(self, course_key: str, scope: str = 'latest') -> str | None:
        """Generates a NotebookLM podcast. Returns the path to the MP3 or None."""
        try:
            from generators.notebooklm_agent import NotebookLMAgent
            agent = NotebookLMAgent()
            course_name = self.config['courses'][course_key]['name']
            print(f"\n🎙️  מייצר פודקאסט: {course_name} ({scope})")

            # first sync the latest summaries
            agent.sync_sources(course_key, scope)

            mp3_path = agent.generate_podcast(
                course_key=course_key,
                scope=scope,
                language='he',
                instructions='סיכום קצר ובהיר של עיקרי החומר — 10-15 דקות',
            )
            print(f"  ✅ פודקאסט: {mp3_path}")
            return mp3_path
        except Exception as e:
            print(f"  ❌ שגיאה בפודקאסט: {e}")
            return None

    def _sync_obsidian(self):
        """Syncs summaries and quizzes to the Obsidian vault (silent — not critical)."""
        try:
            import sys as _sys
            _sys.path.insert(0, str(Path(__file__).parent))
            from exporters.obsidian_exporter import sync_all
            result = sync_all(force=False)
            print(f"📓 Obsidian: {result['summaries']} סיכומים, {result['quizzes']} חידונים סונכרנו")
        except Exception as e:
            print(f"  ⚠️ Obsidian sync: {e}")

    def run_course(self, course_key: str):
        """Runs the whole process for a single course"""
        print(f"\n{'#'*60}")
        print(f"# מעבד קורס: {self.config['courses'][course_key]['name']}")
        print(f"{'#'*60}\n")

        # step 1: collection
        materials = self.collect_materials(course_key)

        # step 2: summarizing
        summaries = self.process_and_summarize(materials)

        # step 3: sending
        if summaries:
            self.send_summaries(summaries, course_key)
        else:
            print("⚠️  לא נוצרו סיכומים")

        # step 4: Obsidian sync
        self._sync_obsidian()
    
    def run_all(self):
        """Runs all the courses"""
        print("\n" + "="*60)
        print("🎓 Smart Study Agent - מצב מלא")
        print("="*60 + "\n")
        
        # only the current semester's courses. The old courses stay in the config (history,
        # vault and RAG) but there is no point scanning them every day — see ACTIVE_COURSES.
        all_keys = list(self.config['courses'].keys())
        try:
            from config.paths import ACTIVE_COURSES
            course_keys = [k for k in all_keys if k in ACTIVE_COURSES] or all_keys
        except ImportError:
            course_keys = all_keys
        if len(course_keys) < len(all_keys):
            print(f"📌 קורסים פעילים: {len(course_keys)}/{len(all_keys)} "
                  f"({', '.join(course_keys)})\n")

        for course_key in course_keys:
            try:
                self.run_course(course_key)
            except Exception as e:
                print(f"❌ שגיאה קריטית בקורס {course_key}: {e}")
                continue
        
        print("\n" + "="*60)
        print("✅ הושלם!")
        print("="*60 + "\n")


def main():
    parser = argparse.ArgumentParser(description='Smart Study Agent')
    
    parser.add_argument(
        '--course',
        type=str,
        help='הרץ קורס ספציפי (intro_to_cs, discrete_math_2, וכו\')'
    )
    
    parser.add_argument(
        '--all',
        action='store_true',
        help='הרץ את כל הקורסים'
    )
    
    parser.add_argument(
        '--config',
        type=str,
        default='config/courses.yaml',
        help='נתיב לקובץ תצורה'
    )
    
    args = parser.parse_args()
    
    # load environment variables
    from dotenv import load_dotenv
    load_dotenv(override=True)

    # semester gate: do not run the pipeline outside the semester window (before logging in to Panopto)
    try:
        with open(args.config, 'r', encoding='utf-8') as _f:
            _cfg = yaml.safe_load(_f)
    except Exception:
        _cfg = {}
    active, reason = semester_active(_cfg)
    if not active:
        print(f"⏸️  הצינור מושבת: {reason}")
        print("   אין איסוף / סיכום / שליחה. להרצה ידנית בכל זאת: SMART_STUDY_FORCE=1")
        return

    # initialize
    agent = SmartStudyAgent(args.config)
    
    # run
    if args.course:
        agent.run_course(args.course)
    elif args.all:
        agent.run_all()
    else:
        print("❌ יש לציין --course או --all")
        parser.print_help()


if __name__ == "__main__":
    main()
