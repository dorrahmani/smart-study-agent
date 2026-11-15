"""
collectors/moodle_watcher.py — Detect new things in Moodle and pipe them
into the Vault.

This is a thin orchestrator. It does NOT replace the existing
fetch_assignments_now.py / assignment_tracker.py pipeline — it CALLS them
and then runs the appropriate exporters.

Triggers:
  - Manual via Dashboard "🔄 סנכרן עכשיו" button (now)
  - Scheduled task on Windows (future)
  - Cron on Oracle Cloud + WhatsApp webhook (future)

Returns a dict of changes since last_seen for the Today tab.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from datetime import datetime
from typing import Any

from config.paths import DATA_DIR, ASSIGNMENTS_CACHE
from exporters.assignment_obsidian import export_assignments
from exporters.announcement_obsidian import export_announcements

LAST_SEEN_PATH = DATA_DIR / 'moodle_last_seen.json'


def _load_last_seen() -> dict:
    if LAST_SEEN_PATH.exists():
        try:
            return json.loads(LAST_SEEN_PATH.read_text(encoding='utf-8'))
        except Exception:
            return {}
    return {}


def _save_last_seen(d: dict) -> None:
    LAST_SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAST_SEEN_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2),
                              encoding='utf-8')


def sync(run_fetcher: bool = False, scrape_announcements: bool = True,
         headless: bool = True) -> dict[str, Any]:
    """
    Run a Moodle sync cycle.

    run_fetcher=True actually triggers fetch_assignments_now (slow, opens browser).
    run_fetcher=False just re-exports from existing _cache.json (fast).

    Returns:
      {
        'new_assignments': int,
        'updated_assignments': int,
        'new_announcements': int,
        'last_run': iso_str,
        'changes': [str],     # human-readable list for "🆕 חדש" UI
      }
    """
    changes: list[str] = []
    last_seen = _load_last_seen()

    if run_fetcher:
        try:
            # Lazy import to avoid loading playwright unless explicitly asked
            from collectors import fetch_assignments_now  # noqa: F401
            # The existing module runs as __main__ — we trigger it by import only
            # if it has a callable. Otherwise the user runs it manually.
            if hasattr(fetch_assignments_now, 'main'):
                fetch_assignments_now.main()
        except Exception as e:
            changes.append(f'⚠️ fetch_assignments failed: {e}')

    # Re-export assignments from cache (always cheap)
    new_count = export_assignments(force=False)

    # Track cache mtime to detect "new since last sync"
    if ASSIGNMENTS_CACHE.exists():
        cache_mtime = ASSIGNMENTS_CACHE.stat().st_mtime
        prev = last_seen.get('assignments_cache_mtime', 0)
        if cache_mtime > prev:
            try:
                data = json.loads(ASSIGNMENTS_CACHE.read_text(encoding='utf-8'))
                count = len(data.get('assignments', []))
                changes.append(f'📋 _cache.json עודכן ({count} מטלות)')
            except Exception:
                pass
        last_seen['assignments_cache_mtime'] = cache_mtime

    # ── Announcements (Moodle forums/news) ───────────────────────────
    new_announce = 0
    if scrape_announcements:
        try:
            from collectors.announcement_scraper import fetch_new_announcements
            new_items = fetch_new_announcements(headless=headless)
            if new_items:
                export_announcements(new_items)
                new_announce = len(new_items)
                for it in new_items[:5]:
                    changes.append(
                        f"📢 {it.get('course_key','?')}: {it.get('title','')[:60]}"
                    )
                if len(new_items) > 5:
                    changes.append(f"📢 ועוד {len(new_items)-5} הודעות חדשות")
        except Exception as e:
            changes.append(f'⚠️ announcement scrape failed: {e}')

    last_seen['last_run'] = datetime.now().isoformat()
    _save_last_seen(last_seen)

    return {
        'new_assignments':    new_count,
        'updated_assignments': new_count,
        'new_announcements':  new_announce,
        'last_run':           last_seen['last_run'],
        'changes':            changes,
    }


if __name__ == '__main__':
    import sys
    result = sync(
        run_fetcher='--fetch' in sys.argv,
        scrape_announcements='--no-announce' not in sys.argv,
        headless='--head' not in sys.argv,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
