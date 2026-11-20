"""
generators/nlm_lectures.py — every lecture's and tutorial's FULL transcript and its summary in the course's NotebookLM
'all' notebook, under one naming (17/11, the owner: "כל התמלול של ההרצאה, כל הסיכום של ההרצאה לתוך המסלול של ההרצאה").

The old sync (notebooklm_agent.sync_sources) uploaded each summary PDF under its file name and each SRT as one long line
under '<course>_<date>_<name>' — including Linear Algebra's Cyrillic Panopto captions and the undated copies of the
same recordings. Here, per course, pure on the files:

    sessions   data/summaries/<course>/{lectures,tutorials}/'<date> - <name>.(md|pdf)' and data/panopto/<course>_*.srt,
               matched by date + name (punctuation / '(2)' vs '2' / double spaces ignored)
    titles     'תמלול — הסתברות וסטטיסטיקה שיעור 3 (01/11/2026)' · 'סיכום — … תרגול 2 (28/10/2026)'
               lectures numbered by meeting date, tutorials by week (several groups teach the same tutorial);
               two recordings on one date and kind → ' — <their name>' added
    quality    an SRT that fails utils.srt_quality.srt_is_good_hebrew (Cyrillic / empty) is never uploaded, and the old
               sync's copy of it is taken out of the notebook (the file stays where it is)
    clean      the transcript as text: no numbers / timestamps / tags, Whisper's repeated lines collapsed, paragraphs
    legacy     a source the old sync made (file-name title) is renamed to its title — no second upload
    idempotent present = its title is in the notebook (or a weekly bundle that holds it, per the index's 'lectures')
    limit      NotebookLM Pro: 300 sources a notebook. If the uploads would pass SOURCE_LIMIT - HEADROOM, the missing
               transcripts go in one source per week ('תמלולים — <קורס> שבוע N'); past the limit nothing is forced in
Never removes a source it didn't put there (only the old sync's titles for bad / duplicate SRTs). No LLM.
"""
from __future__ import annotations

import contextlib
import io
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE_LIMIT = 300
HEADROOM = 10
MAX_TEXT = 900_000
WORD = {'lecture': 'שיעור', 'tutorial': 'תרגול'}
DATE_RX = re.compile(r'^(\d{4}-\d{2}-\d{2})')
TS_RX = re.compile(r'^\d{1,2}:\d{2}:\d{2}[,.]\d{3}\s*-->')


def _quiet_good(path) -> bool:
    from utils.srt_quality import srt_is_good_hebrew
    with contextlib.redirect_stdout(io.StringIO()):
        return srt_is_good_hebrew(path)


def norm(name: str) -> str:
    """'הרצאה מרצה_צ (2)' = 'הרצאה מרצה_צ 2'; 'תרגול – מרצה_ת' = 'תרגול  מרצה_ת'; '(מהמצגת)' is how the summary was made."""
    s = (name or '').lower().replace('(מהמצגת)', '').replace('_merged', '')
    s = re.sub(r'[()\[\]\-–—_.,\'"׳״]+', ' ', s)              # "מיקרו כלכלה א'" = "מיקרו כלכלה א"
    return ' '.join(s.split())


def clean_text(srt_path: str | Path) -> str:
    """The transcript as readable text: no cue numbers / timestamps / tags, a line Whisper repeated once, paragraphs of
    ~8 captions (NotebookLM cites by passage)."""
    raw = Path(srt_path).read_text(encoding='utf-8', errors='replace')
    lines, prev = [], None
    for ln in raw.splitlines():
        t = re.sub(r'<[^>]+>|\{\\[^}]*\}', '', ln).strip()
        if not t or t.isdigit() or TS_RX.match(t) or t == prev:
            continue
        lines.append(t)
        prev = t
    return '\n\n'.join(' '.join(lines[i:i + 8]) for i in range(0, len(lines), 8))


def _course_name(course: str) -> str:
    try:
        from config.paths import COURSE_NAMES
        return COURSE_NAMES.get(course, course)
    except Exception:
        return course


def _srts(course: str, panopto: Path) -> list[Path]:
    from generators import notebooklm_agent as nlm
    if not panopto.exists():
        return []
    return [p for p in sorted(panopto.glob(f'{course}_*.srt')) if nlm._srt_course(p.name, course)]


def _week(d: str) -> tuple[int, int]:
    """Israeli week (Sunday first)."""
    y, w, _ = (date.fromisoformat(d) + timedelta(days=1)).isocalendar()
    return y, w


def sessions(course: str, root: Path | None = None, good=None) -> dict:
    """→ {'sessions': [{kind, date, base, srt, summary_pdf, summary_md}], 'bad': [srt], 'dupes': [srt]}. Pure on files."""
    base_dir = root or ROOT
    good = good or _quiet_good
    from generators.notebooklm_agent import _TUTORIAL_KEYWORDS
    sums: dict[tuple, dict] = {}
    for folder_kind, sub in (('lecture', 'lectures'), ('tutorial', 'tutorials')):
        d = base_dir / 'data' / 'summaries' / course / sub
        for p in sorted(d.glob('*')) if d.is_dir() else []:
            if p.suffix.lower() not in ('.pdf', '.md'):
                continue
            m = re.match(r'^(\d{4}-\d{2}-\d{2})\s*-\s*(.+)$', p.stem)
            dt, name = (m.group(1), m.group(2)) if m else ('', p.stem)
            # 'תרגול - רעי' filed under lectures/ is still a tutorial
            kind = 'tutorial' if any(k in name.lower() for k in _TUTORIAL_KEYWORDS) else folder_kind
            s = sums.setdefault((dt, norm(name)), {'kind': kind, 'date': dt, 'base': name.strip(), 'srt': None,
                                                   'summary_pdf': None, 'summary_md': None})
            key = 'summary_pdf' if p.suffix.lower() == '.pdf' else 'summary_md'
            s[key] = s[key] or p                             # the same summary in both folders is one session
    from generators.notebooklm_agent import _classify_srt
    import hashlib
    bad, dupes, srts = [], [], []
    for p in _srts(course, base_dir / 'data' / 'panopto'):
        rest = p.stem[len(course) + 1:]
        m = re.match(r'^(\d{4}-\d{2}-\d{2})_(.*)$', rest)
        srts.append((p, m.group(1) if m else '', (m.group(2) if m else rest).strip()))
    sha = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p, _dt, _b in srts}
    dated = {sha[p] for p, dt, _b in srts if dt}
    loose = []
    for p, dt, b in srts:
        if not dt and sha[p] in dated:
            dupes.append(p)                                 # the undated copy the collector kept of a dated recording
            continue
        if not good(p):
            bad.append(p)
            continue
        hit = next((k for k in sums if k[0] == dt and k[1] == norm(b) and sums[k]['srt'] is None), None)
        if hit:
            sums[hit]['srt'] = p
        else:
            loose.append((p, dt, b))
    for p, dt, b in loose:                                  # 'הרצאה 3' (summary) ↔ 'הרצאה 3 - רועי' (recording)
        hit = next((k for k in sums if dt and k[0] == dt and sums[k]['srt'] is None
                    and (norm(b).startswith(k[1]) or k[1].startswith(norm(b)))), None)
        if hit:
            sums[hit]['srt'] = p
            continue
        kind = _classify_srt(p.name)
        sums[(dt, norm(b), p.name)] = {'kind': kind, 'date': dt, 'base': b, 'srt': p,
                                       'summary_pdf': None, 'summary_md': None}
    return {'sessions': sorted(sums.values(), key=lambda s: (s['date'] or '9999', s['kind'], s['base'])),
            'bad': bad, 'dupes': dupes}


def _label(s: dict, n: dict, crowded: set) -> str:
    if not s['date']:
        return s['base']
    dd = datetime.strptime(s['date'], '%Y-%m-%d').strftime('%d/%m/%Y')
    tag = f"{WORD[s['kind']]} {n[(s['kind'], s['date'])]} ({dd})"
    return tag + (f" — {s['base']}" if (s['kind'], s['date']) in crowded else '')


def plan(course: str, root: Path | None = None, good=None) -> dict:
    """→ {'sources': [{title, role transcript|summary, kind text|file, path, legacy {titles}, date, week, file}],
    'remove': [(legacy title, why)]}. What the course's 'all' notebook should hold. Pure on files."""
    ss = sessions(course, root, good)
    name = _course_name(course)
    lec_dates = sorted({s['date'] for s in ss['sessions'] if s['kind'] == 'lecture' and s['date']})
    tut_weeks = sorted({_week(s['date']) for s in ss['sessions'] if s['kind'] == 'tutorial' and s['date']})
    n = {}
    for s in ss['sessions']:
        if s['date']:
            n[(s['kind'], s['date'])] = (lec_dates.index(s['date']) + 1 if s['kind'] == 'lecture'
                                         else tut_weeks.index(_week(s['date'])) + 1)
    count = defaultdict(int)
    for s in ss['sessions']:
        if s['date']:
            count[(s['kind'], s['date'])] += 1
    crowded = {k for k, v in count.items() if v > 1}
    weeks = sorted({_week(s['date']) for s in ss['sessions'] if s['date']})
    out, used = [], set()
    for s in ss['sessions']:
        label = _label(s, n, crowded)
        week = weeks.index(_week(s['date'])) + 1 if s['date'] else None          # the course's own week count
        if s['srt']:
            t = f'תמלול — {name} {label}'
            if t not in used:
                used.add(t)
                out.append({'title': t, 'role': 'transcript', 'kind': 'text', 'path': str(s['srt']),
                            'file': s['srt'].name, 'legacy': {s['srt'].stem}, 'date': s['date'], 'week': week,
                            'heading': f"תמלול מלא — {name} — {label}"})
        md, pdf = s['summary_md'], s['summary_pdf']
        if md or pdf:
            t = f'סיכום — {name} {label}'
            if t not in used:
                used.add(t)
                legacy = {x for p in (md, pdf) if p for x in (p.name, p.stem)}
                use_md = md is not None and md.stat().st_size > 200
                p = md if use_md else pdf
                out.append({'title': t, 'role': 'summary', 'kind': 'text' if use_md else 'file', 'path': str(p),
                            'file': (pdf or md).name, 'legacy': legacy, 'date': s['date'], 'week': week,
                            'heading': f'סיכום — {name} — {label}'})
    remove = [(p.stem, 'bad') for p in ss['bad']] + [(p.stem, 'dupe') for p in ss['dupes']]
    return {'sources': out, 'remove': remove}


def titles(course: str, root: Path | None = None) -> dict[str, str]:
    """{file basename (the SRT, the summary's PDF and .md): its title} — sync_sources and remove_sources use the same
    names as this sync."""
    try:
        srcs = plan(course, root)['sources']
    except Exception:
        return {}
    out = {}
    for s in srcs:
        for f in {s['file'], Path(s['path']).name} | {x for x in s['legacy'] if '.' in x}:
            out[f] = s['title']
    return out


def week_title(course: str, week) -> str:
    return f'תמלולים — {_course_name(course)} ' + (f'שבוע {week}' if week else 'ללא תאריך')


def diff(srcs: list[dict], remove: list[tuple[str, str]], live: list[tuple[str, str]], state: dict) -> dict:
    """Pure. live = [(id, title)]; state = {title: file} of what this sync put in (a weekly bundle: {title: [titles]}).
    → {'kept': [titles], 'rename': [(id, old, new)], 'upload': [src], 'remove': [(id, title, why)]}."""
    by_title = defaultdict(list)
    for sid, t in live:
        by_title[t].append(sid)
    bundled = {p for t, v in state.items() if isinstance(v, list) and t in by_title for p in v}
    want = {s['title'] for s in srcs}
    kept, rename, upload, drop, seen = [], [], [], [], set()
    for s in srcs:
        olds = [(sid, t) for t in sorted(s['legacy']) if t not in want for sid in by_title.get(t, [])]
        if by_title.get(s['title']) or s['title'] in bundled:
            kept.append(s['title'])
            drop += [(sid, t, 'duplicate') for sid, t in olds if sid not in seen]   # an old copy beside the titled one
        elif olds:
            rename.append((olds[0][0], olds[0][1], s['title']))
            drop += [(sid, t, 'duplicate') for sid, t in olds[1:] if sid not in seen]
        else:
            upload.append(s)
        seen.update(sid for sid, _t in olds)
    for t, why in remove:
        if t not in want:
            drop += [(sid, t, why) for sid in by_title.get(t, []) if sid not in seen]
    return {'kept': kept, 'rename': rename, 'upload': upload, 'remove': drop}


def bundle_by_week(course: str, srcs: list[dict]) -> list[dict]:
    """The missing transcripts, one text source per week; each part under its own heading."""
    groups = defaultdict(list)
    for s in srcs:
        groups[s['week'] if s['week'] is not None else 0].append(s)
    out = []
    for wk, items in sorted(groups.items()):
        out.append({'title': week_title(course, wk), 'role': 'bundle', 'kind': 'text', 'path': '',
                    'parts': [s['title'] for s in items], 'items': items, 'legacy': set()})
    return out


def _text_of(src: dict) -> str:
    if src.get('items'):
        return '\n\n\n'.join(f"# {s['title']}\n\n{_text_of(s)}" for s in src['items'])[:MAX_TEXT]
    if src['role'] == 'transcript':
        body = clean_text(src['path'])
    else:
        body = Path(src['path']).read_text(encoding='utf-8', errors='replace')
    return f"{src['heading']}\n\n{body}"[:MAX_TEXT]


class Ops:
    """The NotebookLM calls, through generators/notebooklm_agent (a fresh client per call, as there)."""

    def __init__(self, agent=None):
        from generators import notebooklm_agent as nlm
        self.nlm = nlm
        self.agent = agent or nlm.NotebookLMAgent()

    def list(self, nb_id: str) -> list[tuple[str, str]]:
        return [(s.id, s.title) for s in self.nlm._run(self.agent._list_sources(nb_id))]

    def add(self, nb_id: str, src: dict) -> None:
        if src['kind'] == 'file':
            self.nlm._run(self.agent._add_file(nb_id, src['path'], title=src['title']))
        else:
            self.nlm._run(self.agent._add_text(nb_id, src['title'], _text_of(src)))

    def rename(self, nb_id: str, sid: str, title: str) -> None:
        self.nlm._run(self.agent._rename_source(nb_id, sid, title))

    def delete(self, nb_id: str, sid: str) -> None:
        self.nlm._run(self.agent._delete_source(nb_id, sid))


def sync(course: str, scope: str = 'all', ops=None, apply: bool = True, root: Path | None = None,
         index_load=None, index_save=None, limit: int = SOURCE_LIMIT, good=None) -> dict:
    """The course's transcripts + summaries into its existing notebook. apply=False only reports.
    → {before, after, kept, renamed, uploaded, removed, bundled, errors, table}."""
    from generators import notebooklm_agent as nlm
    load = index_load or nlm._load_notebooks_index
    save = index_save or nlm._save_notebooks_index
    entry = (load().get(course) or {}).get(scope)
    res = {'course': course, 'scope': scope, 'before': 0, 'after': 0, 'kept': 0, 'renamed': [], 'uploaded': [],
           'removed': [], 'bundled': [], 'errors': [], 'table': []}
    if not entry or not entry.get('notebook_id'):
        res['errors'].append('no notebook')
        return res
    p = plan(course, root, good)
    srcs = p['sources']
    nb = entry['notebook_id']
    ops = ops or Ops()
    live = ops.list(nb)
    res['before'] = len(live)
    state = dict(entry.get('lectures') or {})
    d = diff(srcs, p['remove'], live, state)
    res['kept'] = len(d['kept'])
    present = set(d['kept']) | {n for _i, _o, n in d['rename']}
    res['table'] = table(srcs, present)
    ups = d['upload']
    room = limit - HEADROOM - (len(live) - len(d['remove']))
    if len(ups) > room:
        trans = [s for s in ups if s['role'] == 'transcript']
        ups = [s for s in ups if s['role'] != 'transcript'] + bundle_by_week(course, trans)
        res['bundled'] = [b['title'] for b in ups if b['role'] == 'bundle']
    if len(ups) > room:
        res['errors'].append(f'source limit: room for {max(room, 0)} of {len(ups)} new sources')
        ups = ups[:max(room, 0)]
    if not apply:
        res.update(renamed=[f'{o} → {n}' for _i, o, n in d['rename']], uploaded=[s['title'] for s in ups],
                   removed=[f'{t} ({w})' for _i, t, w in d['remove']])
        res['after'] = res['before'] + len(ups) - len(d['remove'])
        return res
    for sid, old, new in d['rename']:
        try:
            ops.rename(nb, sid, new)
            res['renamed'].append(f'{old} → {new}')
            state[new] = next((s['file'] for s in srcs if s['title'] == new), '')
        except Exception as e:
            res['errors'].append(f'rename {old}: {e!r}'[:200])
    for s in ups:
        try:
            ops.add(nb, s)
        except Exception as e:
            res['errors'].append(f"{s['title']}: {e!r}"[:200])
            continue
        res['uploaded'].append(s['title'])
        state[s['title']] = s['parts'] if s.get('parts') else s['file']
    for sid, t, why in d['remove']:
        try:
            ops.delete(nb, sid)
            res['removed'].append(f'{t} ({why})')
        except Exception as e:
            res['errors'].append(f'remove {t}: {e!r}'[:200])
    for t in d['kept']:
        state.setdefault(t, next((s['file'] for s in srcs if s['title'] == t), ''))
    idx = load()                                         # another writer may have saved meanwhile
    e = idx.setdefault(course, {}).setdefault(scope, entry)
    e['lectures'] = state
    synced = set(e.get('sources_synced') or [])
    done = set(res['uploaded']) | set(d['kept']) | {n for _i, _o, n in d['rename']}
    covered = {x['title'] for b in ups if b.get('parts') and b['title'] in done for x in b['items']}
    e['sources_synced'] = sorted(synced | {s['file'] for s in srcs if s['title'] in done | covered})
    e['lectures_sync'] = datetime.now().isoformat(timespec='seconds')
    save(idx)
    try:
        res['after'] = len(ops.list(nb))
    except Exception:
        res['after'] = res['before'] + len(res['uploaded']) - len(res['removed'])
    return res


def table(srcs: list[dict], present: set) -> list[dict]:
    """Per session: {date, label, summary: True/False/None (none on disk), transcript: …}."""
    rows: dict[tuple, dict] = {}
    for s in srcs:
        label = s['title'].split(' — ', 1)[1]
        r = rows.setdefault((s['date'], label), {'date': s['date'], 'label': label, 'summary': None, 'transcript': None})
        r[s['role']] = s['title'] in present
    return list(rows.values())


def main(argv=None) -> int:
    import argparse
    import json
    import sys
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(description="lecture/tutorial transcripts + summaries → each course's NotebookLM 'all' "
                                             "notebook; reports only without --apply")
    ap.add_argument('courses', nargs='*')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--table', action='store_true', help='print the per-session table')
    a = ap.parse_args(argv)
    from generators import notebooklm_agent as nlm
    for ck in a.courses or list(nlm._load_notebooks_index()):
        r = sync(ck, apply=a.apply)
        rows = r.pop('table')
        if a.table:
            print(f'== {ck}')
            for x in rows:
                mark = {True: 'V', False: '-', None: ' '}
                print(f"   summary {mark[x['summary']]}  transcript {mark[x['transcript']]}  {x['label']}")
        print(json.dumps(r, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
