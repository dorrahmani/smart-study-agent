"""
scheduler/sync_runner.py — Scheduled-task entry: runs sync_all
conservatively, then surfaces a Windows toast if anything is new.

Conservative defaults (cost-aware):
  - budget_usd=0.50          : hard cap per scheduled run
  - include_panopto=True     : detect new lectures (no API cost — just SRT)
  - include_whisper=False    : never auto-Whisper (paid; on-demand only)
  - include_summaries=False  : never auto-summarize (paid; on-demand only)
  - headless=True            : no browser window
"""

from __future__ import annotations
import os
import sys
from pathlib import Path

# Ensure repo root on sys.path so `pipelines.*` and `notifier.*` resolve
# regardless of how Task Scheduler invokes us.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Force UTF-8 console (Hebrew text + emojis in toasts)
os.environ.setdefault('PYTHONIOENCODING', 'utf-8')


def _drive_sync_up() -> None:
    """Push any new/changed files to Google Drive (best-effort)."""
    import subprocess
    sync_script = ROOT / 'scripts' / 'drive_sync.ps1'
    if not sync_script.exists():
        return
    subprocess.run(
        ['powershell.exe', '-ExecutionPolicy', 'Bypass', '-File', str(sync_script), 'up'],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        cwd=str(ROOT)
    )


def main() -> int:
    from pipelines.sync_all import sync_all

    result = sync_all(
        budget_usd=0.50,
        include_panopto=True,
        include_whisper=False,
        include_summaries=False,
        headless=True,
    )

    if result.get('n_new', 0) > 0:
        try:
            from notifier.toast import notify
            notify('Smart Study Agent', result.get('summary_text', 'סונכרן'))
        except Exception:
            # Toasting is best-effort; never let it break a scheduled run.
            pass

    # Always push any newly acquired files (SRTs, PDFs) to Drive
    try:
        _drive_sync_up()
    except Exception:
        pass

    return 0


if __name__ == '__main__':
    sys.exit(main())
