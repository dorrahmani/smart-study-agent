"""
notifier/toast.py — Windows toast notifications. Falls back silently if
the platform doesn't support it.

Usage: notify("New", "5 things changed in Moodle")
"""

from __future__ import annotations
import sys


def notify(title: str, message: str, duration: int = 5) -> bool:
    """Best-effort Windows toast. Returns True on success."""
    if not sys.platform.startswith('win'):
        return False
    # Try win11toast first (Windows 11), then win10toast as fallback
    try:
        from win11toast import toast as _toast
        _toast(title, message, duration='short')
        return True
    except ImportError:
        pass
    try:
        from win10toast import ToastNotifier
        ToastNotifier().show_toast(title, message, duration=duration, threaded=True)
        return True
    except ImportError:
        pass
    print(f'[notify] {title}: {message}')
    return False
