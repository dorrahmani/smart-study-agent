#!/usr/bin/env python3
"""
scripts/moodle_announce_watch.py — Moodle course announcements → WhatsApp (cron 3×/day, Asia/Jerusalem).

Until 10/10 announcements were only written to the Obsidian vault (and not even scheduled since 27/09).
Now: scrape (discovering the new semester's Moodle ids by course number) → one triage call per batch (the same
as the university mail) → data/mail_queue.jsonl → the WhatsApp worker delivers: urgent now (a cancelled lecture,
a quiz date), the rest in the 13:00/20:00 digest, dates → one ✅ calendar question. Still exported to the vault.
A course seen for the first time is a silent baseline. Nothing new → no LLM call.

crontab:  15 5,10,16 * * *  (UTC) → 08:15 / 13:15 / 19:15 Israel (summer time)
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / '.env', override=True)


def to_queue(items: list[dict], triager=None) -> list[dict]:
    """Moodle posts → the mail queue's shape, triaged."""
    from config.paths import COURSE_NAMES
    from integrations import tau_mail
    if not items:
        return []
    mails = [{'id': f"moodle:{it.get('course_key')}:{it.get('date')}:{it.get('title', '')[:40]}", 'thread': '',
              'from': f"מודל · {COURSE_NAMES.get(it.get('course_key'), it.get('course_key', ''))}",
              'subject': it.get('title', ''), 'attachments': [], 'labels': [],
              'course_key': it.get('course_key'), 'date': it.get('date'),      # 15/11 the relevance rule: whose group
              'text': f"(כתב/ה: {it.get('sender', '')})\n{it.get('body', '')}", 'snippet': ''} for it in items]
    rows = tau_mail.gate(mails, triager or tau_mail.triage, 'moodle')        # another group's post: not queued
    tau_mail.QUEUE.parent.mkdir(parents=True, exist_ok=True)
    with open(tau_mail.QUEUE, 'a', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps({**r, 'queued': datetime.now().isoformat(timespec='seconds'), 'source': 'moodle'},
                               ensure_ascii=False) + '\n')
    return rows


def sync_course_files() -> int:
    """05/11: new slides were collected only by the nightly pipeline, so a deck posted in the morning missed that day's
    class message. Here they arrive 3×/day. → how many new files."""
    from collectors.course_files import sync
    from collectors.moodle_collector import MoodleCollector
    from config.paths import ACTIVE_COURSES
    session = MoodleCollector()._login_and_get_session()
    return sum(len(sync(ck, session=session)) for ck in ACTIVE_COURSES)


def main() -> int:
    from utils.budget import install_guard
    install_guard()                                # the watchers read others' words on a clock: the lock holds here too
    import fcntl                                          # Linux-only: imported here so tests can import this module
    from collectors.announcement_scraper import fetch_new_announcements
    with open(ROOT / 'data' / 'moodle_announce.lock', 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0
        try:
            items = fetch_new_announcements(headless=True)
        except Exception as e:
            print(f'{datetime.now():%F %T} moodle FAILED: {e}', flush=True)
            return 0
        try:
            from exporters.announcement_obsidian import export_announcements
            if items:
                export_announcements(items)               # the vault / RAG keep getting them, as before
        except Exception as e:
            print(f'  ⚠️ vault export failed: {e}', flush=True)
        rows = to_queue(items)
        try:                      # 19/11 a post that names an assignment / quiz with a date → tracked at once
            from agent import obligation_ingest
            obligation_ingest.from_announcements([it for it in items if it.get('course_key')])
        except Exception as e:
            print(f'  ⚠️ obligation ingest: {e!r}'[:200], flush=True)
        print(f'{datetime.now():%F %T} moodle ok: {len(items)} new → queued {len(rows)}', flush=True)
        try:
            print(f'{datetime.now():%F %T} moodle files: {sync_course_files()} new', flush=True)
        except Exception as e:
            print(f'  ⚠️ moodle files failed: {e!r}'[:300], flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
