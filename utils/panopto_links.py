"""
utils/panopto_links.py — make summary timestamps clickable.

Every "[HH:MM:SS]" in a summary becomes a Markdown link that opens the lecture
recording in Panopto at exactly that second, so "didn't get this part" is one
click away from hearing the lecturer explain it. Works in the PDF (Playwright
renders <a> links), in Obsidian and in the dashboard.

The Panopto session id comes from the collector's per-course metadata
(data/panopto/<course>_metadata.json: video_id + srt_path).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

VIEWER = 'https://tau.cloud.panopto.eu/Panopto/Pages/Viewer.aspx?id={vid}&start={sec}'

# [HH:MM:SS] that is not already a link label "[..](" — 3-part only, so a
# "[12:30]" clock time is never mistaken for a position in the recording
_TS = re.compile(r'\[(\d{1,2}):([0-5]\d):([0-5]\d)\](?!\()')
_ID = re.compile(r'^[0-9a-fA-F-]{36}$')


def _basename(p: str) -> str:
    return re.split(r'[\\/]', str(p))[-1]


def video_id_for_srt(srt_path: str, panopto_dir: Path | str = 'data/panopto') -> str | None:
    """Panopto session id for a transcript file, or None if unknown."""
    if not srt_path:
        return None
    name = _basename(srt_path)
    if name.endswith('.bad'):
        name = name[:-4]
    for meta in Path(panopto_dir).glob('*_metadata.json'):
        try:
            rows = json.loads(meta.read_text(encoding='utf-8'))
        except Exception:
            continue
        for r in rows if isinstance(rows, list) else []:
            if _basename(r.get('srt_path', '')) == name:
                vid = str(r.get('video_id') or '')
                return vid if _ID.match(vid) else None
    return None


def linkify_timestamps(md: str, video_id: str | None) -> str:
    """Turn [HH:MM:SS] into [HH:MM:SS](panopto-url&start=seconds)."""
    if not md or not video_id or not _ID.match(str(video_id)):
        return md

    def _sub(m: re.Match) -> str:
        h, mi, s = (int(x) for x in m.groups())
        sec = h * 3600 + mi * 60 + s
        return f'[{m.group(1)}:{m.group(2)}:{m.group(3)}]({VIEWER.format(vid=video_id, sec=sec)})'

    return _TS.sub(_sub, md)
