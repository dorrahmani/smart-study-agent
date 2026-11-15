"""
NotebookLM Agent — מנהל אינטגרציה עם NotebookLM של גוגל.

הספרייה (notebooklm-py) היא async-only, ולכן כל מתודה ציבורית כאן עוטפת
async function ב-asyncio.run() כדי שניתן יהיה לקרוא מ-Streamlit (סינכרוני).

לפני השימוש הראשון:
    pip install "notebooklm-py[browser]"
    notebooklm login   # פותח דפדפן ומבצע OAuth של גוגל פעם אחת
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

# ----- Paths --------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
NOTEBOOKS_JSON = ROOT / "config" / "notebooklm_notebooks.json"
DATA_ROOT = ROOT / "data" / "notebooklm"
SUMMARIES_ROOT = ROOT / "data" / "summaries"
MOODLE_ROOT = ROOT / "data" / "moodle"
COURSES_YAML = ROOT / "config" / "courses.yaml"


# ----- Utility ------------------------------------------------------------

def _load_notebooks_index() -> dict:
    if not NOTEBOOKS_JSON.exists():
        return {}
    try:
        return json.loads(NOTEBOOKS_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_notebooks_index(idx: dict) -> None:
    NOTEBOOKS_JSON.parent.mkdir(parents=True, exist_ok=True)
    tmp = NOTEBOOKS_JSON.with_suffix(".tmp")           # a half-written index reads as empty → a new notebook
    tmp.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(NOTEBOOKS_JSON)


def _course_data_dir(course_key: str, sub: str) -> Path:
    p = DATA_ROOT / course_key / sub
    p.mkdir(parents=True, exist_ok=True)
    return p


def _safe_filename(name: str) -> str:
    return re.sub(r"[^\w\-. ]+", "_", name).strip()[:120]


def _course_display_name(course_key: str) -> str:
    """Read display name from courses.yaml, fallback to key."""
    try:
        import yaml
        with open(COURSES_YAML, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data.get("courses", {}).get(course_key, {}).get("name", course_key)
    except Exception:
        return course_key


# ----- Async helper -------------------------------------------------------

def _run(coro):
    """Run an async coroutine from sync code, even if a loop is already running."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # In Streamlit there usually isn't a running loop in the main thread,
            # but be defensive: run in a fresh loop in a thread.
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                return ex.submit(asyncio.run, coro).result()
    except RuntimeError:
        pass
    return asyncio.run(coro)


# ----- Agent --------------------------------------------------------------

class NotebookLMAgent:
    """
    Agent ניהול NotebookLM. כל מתודה ציבורית סינכרונית ובולעת/יוצרת event loop.
    דורש שהמשתמש הריץ `notebooklm login` פעם אחת מראש (storage_state.json נשמר ב-~/.notebooklm/).
    """

    # --- low-level: open client per call (תמיד "טרי") ---

    @staticmethod
    async def _open_client():
        from notebooklm import NotebookLMClient
        return await NotebookLMClient.from_storage()

    @staticmethod
    def is_authenticated() -> bool:
        """Tests session validity via the new (0.3+) API: list notebooks under
        an `async with` context manager. Returns True when at least one call
        succeeds; False if the storage file is missing or cookies expired."""
        try:
            from notebooklm import NotebookLMClient
            try:
                from notebooklm.paths import get_storage_path
                storage_path = get_storage_path()
            except ImportError:
                from notebooklm import DEFAULT_STORAGE_PATH as storage_path
        except ImportError:
            return False
        if not os.path.exists(str(storage_path)):
            return False

        async def _test():
            try:
                async with await NotebookLMClient.from_storage() as client:
                    nbs = await client.notebooks.list()
                    return len(nbs) >= 0  # zero is still "authed"
            except Exception:
                return False

        try:
            ok = _run(_test())
            if not ok:
                print("⚠️  NotebookLM session expired. Run: notebooklm login")
            return ok
        except Exception:
            return False

    @staticmethod
    def auth_age_hours() -> float:
        """מחזיר כמה שעות עברו מאז חידוש ה-auth האחרון. inf אם לא נמצא."""
        try:
            import time
            try:                                   # 11/11 notebooklm-py 0.8.6 dropped DEFAULT_STORAGE_PATH
                from notebooklm.paths import get_storage_path
                path = get_storage_path()
            except ImportError:
                from notebooklm import DEFAULT_STORAGE_PATH as path
            return (time.time() - os.path.getmtime(str(path))) / 3600
        except Exception:
            return float("inf")

    # --- public: notebook lifecycle ---

    def get_or_create_notebook(self, course_key: str, scope: str = "all") -> str:
        """
        מחזיר notebook_id קיים לקורס+סקופ, או יוצר חדש ושומר באינדקס.
        scope: "all" / "lectures_only" / "YYYY-MM-DD_to_YYYY-MM-DD"
        """
        idx = _load_notebooks_index()
        existing = idx.get(course_key, {}).get(scope, {}).get("notebook_id")
        if existing:
            return existing

        course_name = _course_display_name(course_key)
        title = f"{course_name} — {scope}"

        nb = _run(self._create_notebook(title))
        nb_id = nb.notebook_id if hasattr(nb, "notebook_id") else (
            getattr(nb, "id", None) or nb.project_id
        )

        idx.setdefault(course_key, {})[scope] = {
            "notebook_id": nb_id,
            "title": title,
            "created": datetime.now().isoformat(timespec="seconds"),
            "sources_synced": [],
            "last_sync": None,
        }
        _save_notebooks_index(idx)
        return nb_id

    async def _create_notebook(self, title: str):
        async with await self._open_client() as client:
            return await client.notebooks.create(title)

    # --- public: sources ---

    # --- SRT helpers ---

    @staticmethod
    def _srt_to_plain_text(srt_path: str) -> str:
        """
        ממיר קובץ SRT לטקסט נקי (ללא מספרי שורה וחותמות זמן).
        פורמט SRT:
            1
            00:00:01,000 --> 00:00:04,000
            Some dialogue here.

            2
            ...
        """
        import re as _re
        text = Path(srt_path).read_text(encoding="utf-8", errors="replace")
        # הסר שורות מספר (שורה שמכילה רק ספרה/ות)
        # הסר שורות חותמת זמן (HH:MM:SS,mmm --> HH:MM:SS,mmm)
        lines = []
        ts_pattern = _re.compile(
            r"^\d{2}:\d{2}:\d{2}[,\.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,\.]\d{3}"
        )
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.isdigit():
                continue
            if ts_pattern.match(stripped):
                continue
            lines.append(stripped)
        return " ".join(lines)

    def sync_sources(
        self,
        course_key: str,
        scope: str,
        summary_pdfs: list[str] | None = None,
        moodle_pdfs: list[str] | None = None,
        srt_files: list[str] | None = None,
        text_files: list[str] | None = None,
    ) -> dict:
        """
        מוסיף PDFs ל-notebook אם לא הוספו עדיין.
        SRT files מומרים לטקסט נקי ומועלים דרך add_text (NotebookLM לא תומך .srt).
        text_files: קובצי .md (מצגות ממודל שכבר נקראו) מועלים כמו שהם דרך add_text.
        מחזיר {added: [...], skipped: [...]}.
        """
        nb_id = self.get_or_create_notebook(course_key, scope)
        idx = _load_notebooks_index()
        entry = idx[course_key][scope]
        already = set(entry.get("sources_synced", []))

        pdf_paths: list[str] = []
        if summary_pdfs:
            pdf_paths.extend(summary_pdfs)
        if moodle_pdfs:
            pdf_paths.extend(moodle_pdfs)

        added, skipped = [], []

        # --- PDF / binary files ---
        for p in pdf_paths:
            name = os.path.basename(p)
            if name in already or not os.path.exists(p):
                skipped.append(name)
                continue
            try:
                _run(self._add_file(nb_id, p))
                added.append(name)
                already.add(name)
            except Exception as e:
                skipped.append(f"{name} (ERROR: {e})")

        # --- SRT → plain text ---
        for srt_path in (srt_files or []):
            name = os.path.basename(srt_path)
            # track key: same basename (e.g. "intro_to_cs_Lecture 1b.srt")
            if name in already or not os.path.exists(srt_path):
                skipped.append(name)
                continue
            try:
                plain = self._srt_to_plain_text(srt_path)
                if not plain.strip():
                    skipped.append(f"{name} (EMPTY)")
                    continue
                # title: שם קובץ ללא סיומת
                title = os.path.splitext(name)[0]
                _run(self._add_text(nb_id, title, plain))
                added.append(name)
                already.add(name)
            except Exception as e:
                skipped.append(f"{name} (ERROR: {e})")

        # --- Moodle .md → text ---
        for path in (text_files or []):
            name = os.path.basename(path)
            if name in already or not os.path.exists(path):
                skipped.append(name)
                continue
            try:
                plain = Path(path).read_text(encoding="utf-8", errors="replace")
                if not plain.strip():
                    skipped.append(f"{name} (EMPTY)")
                    continue
                _run(self._add_text(nb_id, re.sub(r"^[0-9:_]+", "", Path(path).stem) or name, plain[:900000]))
                added.append(name)
                already.add(name)
            except Exception as e:
                skipped.append(f"{name} (ERROR: {e})")

        # the primer syncs during the day now: another writer may have saved meanwhile
        idx = _load_notebooks_index()
        entry = idx.setdefault(course_key, {}).setdefault(scope, entry)
        entry["sources_synced"] = sorted(already | set(entry.get("sources_synced", [])))
        entry["last_sync"] = datetime.now().isoformat(timespec="seconds")
        _save_notebooks_index(idx)

        return {"notebook_id": nb_id, "added": added, "skipped": skipped,
                "stale": moved_aside_sources(course_key, already)}

    def remove_sources(self, course_key: str, scope: str, names: list[str], apply: bool = False) -> dict:
        """Takes these sources out of the notebook, matched by exact title (the file name, with or without its
        extension: that is how sync_sources titles them), and off sources_synced. The files stay where they are.
        apply=False only reports. → {matched, missing, removed}."""
        nb_id = _load_notebooks_index()[course_key][scope]["notebook_id"]
        by_title = {t: n for n in names for t in (n, os.path.splitext(n)[0])}
        hits = [(s, by_title[s.title]) for s in _run(self._list_sources(nb_id)) if s.title in by_title]
        res = {"matched": sorted({n for _, n in hits}), "removed": [],
               "missing": sorted(set(names) - {n for _, n in hits})}
        if not apply:
            return res
        for s, name in hits:
            _run(self._delete_source(nb_id, s.id))
            res["removed"].append(name)
        idx = _load_notebooks_index()
        entry = idx[course_key][scope]
        entry["sources_synced"] = sorted(set(entry.get("sources_synced", [])) - set(res["removed"]))
        _save_notebooks_index(idx)
        return res

    async def _list_sources(self, notebook_id: str):
        async with await self._open_client() as client:
            return await client.sources.list(notebook_id)

    async def _delete_source(self, notebook_id: str, source_id: str):
        async with await self._open_client() as client:
            return await client.sources.delete(notebook_id, source_id)

    async def _add_file(self, notebook_id: str, file_path: str, title: str | None = None):
        async with await self._open_client() as client:
            if title:                               # 15/11 coursework: 'תרגיל 3 — פתרון רשמי', not '185195_….pdf'
                return await client.sources.add_file(
                    notebook_id, file_path, wait=True, wait_timeout=180.0, title=title
                )
            return await client.sources.add_file(
                notebook_id, file_path, wait=True, wait_timeout=180.0
            )

    async def _add_text(self, notebook_id: str, title: str, content: str):
        async with await self._open_client() as client:
            return await client.sources.add_text(
                notebook_id, title, content, wait=True, wait_timeout=120.0
            )

    # --- public: artifacts ---

    def generate_quiz(
        self,
        course_key: str,
        scope: str,
        topic_hint: str = "",
        quantity: str = "ten_questions",
        difficulty: str = "medium",
    ) -> dict:
        """מייצר חידון. מחזיר dict עם questions[]."""
        nb_id = self.get_or_create_notebook(course_key, scope)
        out_dir = _course_data_dir(course_key, "quizzes")
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        out_path = out_dir / f"quiz_{scope}_{ts}.json"

        _run(self._gen_and_download(
            nb_id, "quiz",
            instructions=topic_hint or None,
            quantity=quantity, difficulty=difficulty,
            out_path=str(out_path),
        ))

        try:
            data = json.loads(Path(out_path).read_text(encoding="utf-8"))
        except Exception:
            data = {"raw_path": str(out_path)}
        data["_path"] = str(out_path)
        return data

    def generate_podcast(
        self,
        course_key: str,
        scope: str,
        language: str = "he",
        instructions: str = "",
        audio_format: str | None = None,
        audio_length: str | None = None,
    ) -> str:
        """מייצר פודקאסט MP3. מחזיר path."""
        nb_id = self.get_or_create_notebook(course_key, scope)
        out_dir = _course_data_dir(course_key, "podcasts")
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        out_path = out_dir / f"podcast_{scope}_{ts}.mp3"

        _run(self._gen_and_download(
            nb_id, "audio",
            language=language, instructions=instructions or None,
            audio_format=audio_format, audio_length=audio_length,
            out_path=str(out_path),
        ))
        return str(out_path)

    def generate_video(
        self,
        course_key: str,
        scope: str,
        language: str = "he",
        instructions: str = "",
        video_format: str | None = None,
        video_style: str | None = None,
    ) -> str:
        """מייצר Video Overview. מחזיר path ל-MP4."""
        nb_id = self.get_or_create_notebook(course_key, scope)
        out_dir = _course_data_dir(course_key, "videos")
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        out_path = out_dir / f"video_{scope}_{ts}.mp4"

        _run(self._gen_and_download(
            nb_id, "video",
            language=language, instructions=instructions or None,
            video_format=video_format, video_style=video_style,
            out_path=str(out_path),
        ))
        return str(out_path)

    def generate_flashcards(self, course_key: str, scope: str) -> dict:
        """מייצר כרטיסיות. מחזיר dict עם cards[]."""
        nb_id = self.get_or_create_notebook(course_key, scope)
        out_dir = _course_data_dir(course_key, "flashcards")
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        out_path = out_dir / f"flashcards_{scope}_{ts}.json"

        _run(self._gen_and_download(
            nb_id, "flashcards", out_path=str(out_path),
        ))

        try:
            data = json.loads(Path(out_path).read_text(encoding="utf-8"))
        except Exception:
            data = {"raw_path": str(out_path)}
        data["_path"] = str(out_path)
        return data

    def generate_study_guide(
        self, course_key: str, scope: str, language: str = "he",
        extra_instructions: str = "",
    ) -> str:
        """מייצר מדריך לימוד. מחזיר path ל-MD/JSON."""
        nb_id = self.get_or_create_notebook(course_key, scope)
        out_dir = _course_data_dir(course_key, "study_guides")
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        out_path = out_dir / f"study_guide_{scope}_{ts}.md"

        _run(self._gen_and_download_report(
            nb_id, "study_guide", language=language,
            extra_instructions=extra_instructions or None,
            out_path=str(out_path),
        ))
        return str(out_path)

    def generate_faq(self, course_key: str, scope: str,
                     language: str = "he") -> str:
        """מייצר FAQ של הקורס. מחזיר path ל-MD."""
        nb_id = self.get_or_create_notebook(course_key, scope)
        out_dir = _course_data_dir(course_key, "faqs")
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        out_path = out_dir / f"faq_{scope}_{ts}.md"

        _run(self._gen_and_download_report(
            nb_id, "report", language=language,
            extra_instructions=(
                "Produce a comprehensive FAQ for this course. "
                "Format: clear question + answer pairs in Hebrew. "
                "Cover the most likely exam-style questions and the key conceptual "
                "questions a student would ask. 15-25 Q&A items, organized by topic."
            ),
            out_path=str(out_path),
        ))
        return str(out_path)

    def generate_timeline(self, course_key: str, scope: str,
                          language: str = "he") -> str:
        """מייצר ציר זמן של נושאי הקורס. מחזיר path."""
        nb_id = self.get_or_create_notebook(course_key, scope)
        out_dir = _course_data_dir(course_key, "timelines")
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        out_path = out_dir / f"timeline_{scope}_{ts}.md"

        _run(self._gen_and_download_report(
            nb_id, "report", language=language,
            extra_instructions=(
                "Produce a chronological timeline of topics covered in this course. "
                "For each lecture in order: date, main topics introduced, "
                "key theorems/definitions, what builds on previous lectures. "
                "Format as a Hebrew Markdown timeline."
            ),
            out_path=str(out_path),
        ))
        return str(out_path)

    def generate_mindmap(self, course_key: str, scope: str,
                         language: str = "he") -> str:
        """מייצר mindmap (Mermaid) של מושגי הקורס. מחזיר path."""
        nb_id = self.get_or_create_notebook(course_key, scope)
        out_dir = _course_data_dir(course_key, "mindmaps")
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        out_path = out_dir / f"mindmap_{scope}_{ts}.md"

        _run(self._gen_and_download_report(
            nb_id, "report", language=language,
            extra_instructions=(
                "Produce a concept mind map of this course as a Mermaid mindmap diagram. "
                "Group concepts into themes; show dependencies and relationships. "
                "Output should be a Hebrew Markdown file with one Mermaid 'mindmap' "
                "code block, plus a short legend in Hebrew. "
                "Use plain Hebrew labels (no LaTeX) — Mermaid does not render math."
            ),
            out_path=str(out_path),
        ))
        return str(out_path)

    # --- artifact pipeline (generate -> wait -> download) ---

    async def _gen_and_download(
        self, notebook_id: str, kind: str,
        out_path: str,
        instructions=None, language: str | None = None,
        quantity=None, difficulty=None,
        audio_format=None, audio_length=None,
        video_format=None, video_style=None,
    ):
        async with await self._open_client() as client:
            arts = client.artifacts
            if kind == "quiz":
                status = await arts.generate_quiz(
                    notebook_id, instructions=instructions,
                    quantity=quantity, difficulty=difficulty,
                )
                downloader = arts.download_quiz
            elif kind == "audio":
                status = await arts.generate_audio(
                    notebook_id, language=language or "en",
                    instructions=instructions,
                    audio_format=audio_format, audio_length=audio_length,
                )
                downloader = arts.download_audio
            elif kind == "video":
                status = await arts.generate_video(
                    notebook_id, language=language or "en",
                    instructions=instructions,
                    video_format=video_format, video_style=video_style,
                )
                downloader = arts.download_video
            elif kind == "flashcards":
                status = await arts.generate_flashcards(
                    notebook_id, instructions=instructions,
                )
                downloader = arts.download_flashcards
            else:
                raise ValueError(f"unknown kind: {kind}")

            # timeout לפי סוג — כולם יכולים לקחת >10 דקות
            _wait_timeout = {
                "quiz":       600.0,   # 10 דקות
                "audio":     1800.0,   # 30 דקות
                "video":     3600.0,   # 60 דקות
                "flashcards": 600.0,   # 10 דקות
            }.get(kind, 1200.0)

            task_id = getattr(status, "task_id", None) or getattr(status, "id", None)
            if task_id:
                try:
                    await arts.wait_for_completion(
                        notebook_id, task_id, timeout=_wait_timeout
                    )
                except Exception as _wait_err:
                    # NotebookLM עדיין עשוי לסיים — ננסה להוריד בכל זאת
                    try:
                        await downloader(notebook_id, out_path)
                        return  # הורדה הצליחה למרות timeout
                    except Exception:
                        pass  # הורדה נכשלה — נמשיך ונזרוק את שגיאת ה-wait
                    raise _wait_err
            await downloader(notebook_id, out_path)

    async def _gen_and_download_report(
        self, notebook_id: str, report_type: str,
        out_path: str, language: str = "en",
        extra_instructions: str | None = None,
    ):
        async with await self._open_client() as client:
            arts = client.artifacts
            if report_type == "study_guide":
                status = await arts.generate_study_guide(
                    notebook_id, language=language,
                    extra_instructions=extra_instructions,
                )
            else:
                status = await arts.generate_report(
                    notebook_id, language=language,
                    extra_instructions=extra_instructions,
                )
            task_id = getattr(status, "task_id", None) or getattr(status, "id", None)
            if task_id:
                try:
                    await arts.wait_for_completion(
                        notebook_id, task_id, timeout=1800.0  # 30 דקות
                    )
                except Exception as _wait_err:
                    try:
                        await arts.download_report(notebook_id, out_path)
                        return
                    except Exception:
                        pass
                    raise _wait_err
            await arts.download_report(notebook_id, out_path)

    # --- public: list & download existing artifacts ---

    def list_notebook_artifacts(self, course_key: str, scope: str) -> list[dict]:
        """
        מחזיר רשימת כל ה-artifacts הקיימים ב-notebook (מה שכבר נוצר בעבר).
        כל פריט: {id, title, kind, status, created_at}
        status: 1=processing, 2=pending, 3=completed, 4=failed
        """
        nb_id = self.get_or_create_notebook(course_key, scope)
        artifacts = _run(self._list_artifacts(nb_id))
        result = []
        for a in artifacts:
            # str(a.kind) מחזיר "ArtifactType.AUDIO" — נרמל ל-"audio"
            raw_kind = str(a.kind)
            kind_str = raw_kind.split(".")[-1].lower() if "." in raw_kind else raw_kind.lower()
            result.append({
                "id": a.id,
                "title": a.title,
                "kind": kind_str,   # "audio" / "video" / "quiz" / "flashcards" / "report" / ...
                "status": a.status,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            })
        return result

    async def _list_artifacts(self, notebook_id: str):
        async with await self._open_client() as client:
            return await client.artifacts.list(notebook_id)

    def download_existing_artifact(
        self, course_key: str, scope: str, artifact_id: str, kind: str
    ) -> str:
        """
        מוריד artifact קיים מ-NotebookLM לפי ID.
        kind: "audio" / "video" / "quiz" / "flashcards" / "report"
        מחזיר path לקובץ שהורד.
        """
        nb_id = self.get_or_create_notebook(course_key, scope)
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")

        _kind_meta = {
            "audio":      ("podcasts",     "mp3"),
            "video":      ("videos",       "mp4"),
            "quiz":       ("quizzes",      "json"),
            "flashcards": ("flashcards",   "json"),
            "report":     ("study_guides", "md"),
        }
        folder, ext = _kind_meta.get(kind, (kind, "bin"))
        out_dir = _course_data_dir(course_key, folder)
        out_path = out_dir / f"{kind}_{scope}_{ts}.{ext}"

        _run(self._download_by_id(nb_id, artifact_id, kind, str(out_path)))
        return str(out_path)

    async def _download_by_id(
        self, notebook_id: str, artifact_id: str, kind: str, out_path: str
    ):
        """
        מוריד artifact לפי artifact_id ספציפי.
        משתמש ב-API הרשמי של הספרייה עם artifact_id.
        """
        async with await self._open_client() as client:
            arts = client.artifacts
            if kind == "audio":
                await arts.download_audio(notebook_id, out_path, artifact_id=artifact_id)
            elif kind == "video":
                await arts.download_video(notebook_id, out_path, artifact_id=artifact_id)
            elif kind == "quiz":
                await arts.download_quiz(notebook_id, out_path, artifact_id=artifact_id)
            elif kind == "flashcards":
                await arts.download_flashcards(notebook_id, out_path, artifact_id=artifact_id)
            elif kind in ("report", "study_guide"):
                await arts.download_report(notebook_id, out_path, artifact_id=artifact_id)
            else:
                raise ValueError(f"unknown artifact kind: {kind}")

    # --- public: chat (Q&A) ---

    def ask(self, course_key: str, scope: str, question: str,
            conversation_id: str | None = None) -> dict:
        """
        שאלה חופשית. מחזיר {answer, conversation_id, references}.
        """
        nb_id = self.get_or_create_notebook(course_key, scope)
        result = _run(self._ask(nb_id, question, conversation_id))

        # AskResult attrs vary; extract defensively
        answer = getattr(result, "answer", None) or getattr(result, "text", str(result))
        conv = getattr(result, "conversation_id", None)
        refs_raw = getattr(result, "references", []) or []
        refs = []
        for r in refs_raw:
            refs.append({
                "title": getattr(r, "title", "") or getattr(r, "source_title", ""),
                "snippet": getattr(r, "snippet", "") or getattr(r, "text", ""),
            })
        return {"answer": answer, "conversation_id": conv, "references": refs}

    async def _ask(self, notebook_id: str, question: str,
                   conversation_id: str | None):
        async with await self._open_client() as client:
            return await client.chat.ask(
                notebook_id, question, conversation_id=conversation_id
            )


# ----- Helpers for dashboard ----------------------------------------------

def list_summary_pdfs(course_key: str, kind: str = "lectures") -> list[str]:
    """מחזיר את כל ה-PDFs של הרצאות/תרגולים שנוצרו לקורס."""
    base = SUMMARIES_ROOT / course_key / kind
    if not base.exists():
        return []
    return sorted(str(p) for p in base.glob("*.pdf"))


def list_moodle_pdfs(course_key: str) -> list[str]:
    """05/11: the slides live in data/moodle/<course>/files/ since 19/10, and this looked one folder up: no slide or
    syllabus of this semester ever reached a notebook."""
    base = MOODLE_ROOT / course_key
    if not base.exists():
        return []
    return sorted(str(p) for p in base.glob("*.pdf")) + moodle_source_paths(course_key)[0]


def list_moodle_texts(course_key: str) -> list[str]:
    """The Moodle files we already read into .md (Gemini's clean Hebrew, or a pptx's text)."""
    return moodle_source_paths(course_key)[1]


OLD_EXAM_RX = re.compile(r"מבחנ|פתרונ|moed", re.I)        # SKIP_RX's "מבחן" misses the plural


def worthy_moodle_entries(index: dict) -> list[dict]:
    """The Moodle files a notebook should hold: slides, class notes, the syllabus. No exams, solutions or homework
    (dealt with elsewhere), no code or archives, no second copy of a file two group pages list.
    15/11: homework, solutions, feedback and the quiz reviews go in through generators/nlm_coursework (titled, deduped,
    replaced on change) — so neither a quiz review nor an exercise ('Assignment 1' slipped past SKIP_RX) comes here."""
    from collectors.course_files import SKIP_RX
    from agent.obligations import HW_RX, NOT_HW_RX
    out, seen = [], set()
    for e in index.values():
        text = f"{e.get('title', '')} {e.get('section', '')}"
        syllabus = re.search(r"סילבוס|syllab", e.get("title", ""), re.I)
        name = re.sub(r"\W+", "", e.get("title", ""))
        if e.get("module") == "quiz":
            continue
        hw = HW_RX.search(e.get("title", "")) and not NOT_HW_RX.search(e.get("title", ""))
        if not e.get("file") or name in seen or hw or ((SKIP_RX.search(text) or OLD_EXAM_RX.search(text)) and not syllabus):
            continue
        if Path(e["file"]).suffix.lower() not in (".pdf", ".pptx", ".docx", ".ppt"):
            continue
        seen.add(name)
        out.append(e)
    return out


def moodle_source_paths(course_key: str, entries: list[dict] | None = None) -> tuple[list[str], list[str]]:
    """(PDFs to upload, .md files to add as text). The .md wins: PDF slides with reversed Hebrew are read properly there.
    A pptx / ppt goes in as the PDF beside it (collectors.course_files.convert_pending); without one it is left out:
    NotebookLM's upload here takes PDFs."""
    if entries is None:
        from collectors import course_files
        entries = worthy_moodle_entries(course_files.load_index(course_key))
    pdfs, texts = [], []
    for e in entries:
        path = ROOT / e["file"]
        md = path.with_suffix(".md")
        pdf = path.with_suffix(".pdf")
        if md.exists() and md.stat().st_size > 200:
            texts.append(str(md))
        elif pdf.exists() and path.suffix.lower() in (".pdf", ".ppt", ".pptx"):
            pdfs.append(str(pdf))
    return pdfs, texts


def moved_aside_sources(course_key: str, synced: list[str] | set[str], summaries: Path | None = None,
                        panopto: Path | None = None) -> list[str]:
    """06/11: sync_sources only adds. A summary moved aside after it was uploaded (micro B's 26/10 lecture filed under
    Software 1, moved to replaced/ on 31/10) stayed in the notebook and went on answering. → the names a notebook still
    holds whose file now sits in replaced/ and has no live copy under the same name."""
    base = (summaries or SUMMARIES_ROOT) / course_key
    pan = panopto or ROOT / "data" / "panopto"
    out = []
    for name in sorted(synced):
        if name.endswith(".srt"):
            aside, live = pan / "replaced" / name, [pan / name]
        else:
            aside, live = base / "replaced" / name, [base / "lectures" / name, base / "tutorials" / name]
        if aside.exists() and not any(p.exists() for p in live):
            out.append(name)
    return out


_TUTORIAL_KEYWORDS = ("תרגול", "tut", "tirgul", "exercise", "recitation", "drill")
_LECTURE_KEYWORDS  = ("הרצאה", "הרצאות", "lecture", "lec", "shiur")


def _classify_srt(filename: str) -> str:
    """מסווג קובץ SRT לפי שם: 'tutorial' או 'lecture'."""
    name_lower = filename.lower()
    # strip course prefix ({course_key}_...) before checking
    if "_" in name_lower:
        name_lower = name_lower.split("_", 1)[1]
    if any(k in name_lower for k in _TUTORIAL_KEYWORDS):
        return "tutorial"
    return "lecture"   # default


def _srt_course(name: str, course_key: str) -> bool:
    """13/11: "microeconomics_*.srt" also matched every "microeconomics_2_*.srt" — Micro B's recordings went into last
    semester's Micro A notebooks (all, lectures_only, tutorials_only). A file belongs to the LONGEST course key that
    prefixes it."""
    from config.paths import COURSE_NAMES
    longer = [k for k in set(COURSE_NAMES) | set(_load_notebooks_index())
              if k != course_key and k.startswith(course_key + "_")]
    return not any(name.startswith(k + "_") for k in longer)


def list_srt_files(course_key: str, kind: str = "all") -> list[str]:
    """
    מחזיר SRT תמלולים לפי קורס מ-data/panopto/{course_key}_*.srt
    kind: "all" | "lectures" | "tutorials"
    """
    panopto = ROOT / "data" / "panopto"
    if not panopto.exists():
        return []
    all_srts = [p for p in sorted(panopto.glob(f"{course_key}_*.srt")) if _srt_course(p.name, course_key)]
    if kind == "all":
        return [str(p) for p in all_srts]
    want = "tutorial" if kind == "tutorials" else "lecture"
    return [str(p) for p in all_srts if _classify_srt(p.name) == want]


def all_scopes_for_course(course_key: str) -> list[str]:
    idx = _load_notebooks_index()
    return list(idx.get(course_key, {}).keys())
