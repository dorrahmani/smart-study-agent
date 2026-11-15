"""
generators/nlm_coursework.py — every course's homework in its NotebookLM 'all' notebook (15/11, the owner: all homework in
every course must be in NotebookLM and known to the bot).

worthy_moodle_entries (generators/notebooklm_agent.py) leaves homework and solutions out on purpose, so the only
homework any notebook held was Software 1's Assignment 1 (as a bare file name). Here, per course, from
agent/obligations.coursework() + the saved quiz reviews:

    sources   each exercise sheet, its official solution, his graded feedback, each finished quiz's review
    titles    'תרגיל 3 — דף' · 'תרגיל 3 — פתרון רשמי' · 'תרגיל 3 — משוב הבודק' · 'בוחן הרצאה 4 — שאלות ותשובות'
              (a lecturer-site sheet of a past year: 'תרגיל 2 משנה קודמת — דף')
    dedupe    by content hash (Micro B's two group pages list all 24 files twice, byte for byte)
    .md wins  over the PDF of the same name (Gemini's clean Hebrew of probability's sheets; RTL PDFs read reversed)
    replace   the notebook index keeps {title: sha256} per notebook ('coursework'); a changed file (a new quiz attempt,
              a corrected solution) is uploaded again under the same title and the old source is removed after the new
              one is in (scripts/nlm_resync.py's logic, add-first so a failed upload never leaves a hole)
    legacy    a coursework file the old sync uploaded under its file name ('255582_Assignment 1.pdf',
              'quiz_Lecture 4 Quiz') is removed once its titled copy is in
    limit     NotebookLM Pro holds 300 sources a notebook. If the notebook would pass SOURCE_LIMIT - HEADROOM, each
              role is merged into one source per course (all sheets / all solutions / all feedback / all quiz reviews:
              one markdown for the texts, one merged PDF for the PDFs) and the single ones are retired
Never removes a source it didn't put there. No LLM (the NotebookLM web session only).
"""
from __future__ import annotations

import hashlib
import os
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE_LIMIT = 300                 # NotebookLM Pro (the account's linear algebra notebook holds 230)
HEADROOM = 25
MIN_TEXT = 300                     # an assignment page's "Edit submission" stub is not a source
ROLE_HE = {'sheet': 'דף', 'solution': 'פתרון רשמי', 'feedback': 'משוב הבודק'}
BUNDLE_HE = {'sheet': 'כל תרגילי הבית — דפים', 'solution': 'כל תרגילי הבית — פתרונות רשמיים',
             'feedback': 'כל תרגילי הבית — משובי הבודק', 'quiz': 'כל הבחנים — שאלות ותשובות'}


def _sha(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def quiz_title(name: str) -> str:
    """'Lecture 4 Quiz' → 'בוחן הרצאה 4 — שאלות ותשובות'."""
    m = re.search(r'(?:lecture|הרצאה|שיעור)\s*(\d+)', name or '', re.I)
    return f"בוחן הרצאה {m.group(1)} — שאלות ותשובות" if m else f"בוחן {(name or '').strip()} — שאלות ותשובות"


def _older_year(p: Path) -> bool:
    """collectors/lecturer_site saves this year's sheets as '<yy>_ex_NN' ("27/ex_01.pdf"); a bare 'ex_NN' is a past
    year's (probability's ex_02 is due 26.10.21)."""
    return p.parent.name == 'homework' and not re.match(r'\d{2}_', p.name)


def legacy_titles(path: str) -> set[str]:
    """The titles the old sync gave a file: add_file → its name; add_text → its stem without the leading id."""
    p = Path(path)
    return {p.name, p.stem, re.sub(r'^[0-9:_]+', '', p.stem)} - {''}


def plan(course: str, root: Path | None = None) -> list[dict]:
    """[{title, path, kind 'file'|'text', role, num, sha}] — what the course's 'all' notebook should hold. Pure on files."""
    from agent import obligations as ob
    base = root or ROOT
    cand = []
    cw = ob.coursework(course, base)
    for num in sorted(cw, key=lambda n: int(n)):
        e = cw[num]
        for role in ('sheet', 'solution', 'feedback'):
            paths = sorted(e[role])
            mds = {Path(p).with_suffix('') for p in paths if p.lower().endswith('.md')
                   and Path(p).stat().st_size >= MIN_TEXT}
            for p in paths:
                P = Path(p)
                ext = P.suffix.lower()
                if ext == '.pdf':
                    if P.with_suffix('') in mds:
                        continue
                    kind = 'file'
                elif ext in ('.md', '.txt'):
                    if P.stat().st_size < MIN_TEXT:
                        continue
                    kind = 'text'
                else:
                    continue                                     # code / archives: NotebookLM can't read them here
                label = f'תרגיל {num}' + (' משנה קודמת' if _older_year(P) else '')
                cand.append({'title': f'{label} — {ROLE_HE[role]}', 'path': str(P), 'kind': kind, 'role': role,
                             'num': num})
    files = base / 'data' / 'moodle' / course / 'files'
    for p in sorted(files.glob('*_quiz_*.md')) if files.exists() else []:
        name = p.stem.partition('_quiz_')[2]
        n = re.search(r'\d+', name)
        cand.append({'title': quiz_title(name), 'path': str(p), 'kind': 'text', 'role': 'quiz',
                     'num': n.group(0) if n else ''})
    out, seen, used = [], set(), Counter()
    for c in cand:
        c['sha'] = _sha(c['path'])
        if c['sha'] in seen:
            continue
        seen.add(c['sha'])
        used[c['title']] += 1
        if used[c['title']] > 1:
            c['title'] = f"{c['title']} ({used[c['title']]})"
        out.append(c)
    return out


def bundle(srcs: list[dict], outdir: Path) -> list[dict]:
    """Near the source limit: one source per role and type — the texts in one markdown (a heading per item), the PDFs
    merged into one PDF (pypdf). Written under outdir; → the new source list."""
    outdir.mkdir(parents=True, exist_ok=True)
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for s in srcs:
        groups[(s['role'], s['kind'])].append(s)
    out = []
    for (role, kind), items in sorted(groups.items()):
        title = BUNDLE_HE[role] + (' (PDF)' if kind == 'file' and (role, 'text') in groups else '')
        if kind == 'text':
            p = outdir / f'coursework_{role}.md'
            body = '\n\n'.join(f"# {s['title']}\n\n" + Path(s['path']).read_text(encoding='utf-8', errors='replace')
                               for s in items)
            p.write_text(body, encoding='utf-8')
        else:
            from pypdf import PdfWriter
            p = outdir / f'coursework_{role}.pdf'
            w = PdfWriter()
            for s in items:
                w.append(s['path'])
            with p.open('wb') as f:
                w.write(f)
        out.append({'title': title, 'path': str(p), 'kind': kind, 'role': role, 'num': '', 'sha': _sha(p),
                    'parts': [s['title'] for s in items]})
    return out


def diff(srcs: list[dict], state: dict, live: list[tuple[str, str]], synced=(), retire: bool = False) -> dict:
    """Pure. live = [(source id, title)] now in the notebook; state = {title: sha} of what this sync uploaded.
    → {'upload': [(src, [old ids to drop after])], 'legacy': [(id, old title, the title replacing it)],
       'retire': [(id, title)], 'kept': n}."""
    by_title = defaultdict(list)
    for sid, t in live:
        by_title[t].append(sid)
    want = {s['title'] for s in srcs}
    upload, kept = [], 0
    for s in srcs:
        ids = by_title.get(s['title'], [])
        if ids and state.get(s['title']) == s['sha']:
            kept += 1
            continue
        upload.append((s, list(ids)))
    synced = set(synced or ())
    legacy, seen = [], set()
    for s in srcs:
        for p in [s['path']] + [x for x in s.get('legacy_paths', [])]:
            if os.path.basename(p) not in synced:
                continue
            for t in legacy_titles(p):
                for sid in by_title.get(t, []) if t not in want else []:
                    if sid not in seen:
                        seen.add(sid)
                        legacy.append((sid, t, s['title']))
    gone = []
    if retire:                                          # bundling: the single sources this sync put there go
        for t in sorted(set(state) - want):
            gone += [(sid, t) for sid in by_title.get(t, []) if sid not in seen]
    return {'upload': upload, 'legacy': legacy, 'retire': gone, 'kept': kept}


class Ops:
    """The NotebookLM calls, through generators/notebooklm_agent (one fresh client per call, as there)."""

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
            text = Path(src['path']).read_text(encoding='utf-8', errors='replace')
            self.nlm._run(self.agent._add_text(nb_id, src['title'], text[:900000]))

    def delete(self, nb_id: str, sid: str) -> None:
        self.nlm._run(self.agent._delete_source(nb_id, sid))


def sync(course: str, scope: str = 'all', ops=None, apply: bool = True, root: Path | None = None,
         index_load=None, index_save=None, limit: int = SOURCE_LIMIT) -> dict:
    """The course's homework into its notebook (an existing one: none is created here). apply=False only reports.
    → {before, after, uploaded, replaced, legacy_removed, retired, kept, errors, bundled, planned}."""
    from generators import notebooklm_agent as nlm
    load = index_load or nlm._load_notebooks_index
    save = index_save or nlm._save_notebooks_index
    entry = (load().get(course) or {}).get(scope)
    res = {'course': course, 'scope': scope, 'before': 0, 'after': 0, 'uploaded': [], 'replaced': [],
           'legacy_removed': [], 'retired': [], 'kept': 0, 'errors': [], 'bundled': False, 'planned': 0}
    if not entry or not entry.get('notebook_id'):
        res['errors'].append('no notebook')
        return res
    srcs = plan(course, root)
    res['planned'] = len(srcs)
    if not srcs:
        return res
    nb = entry['notebook_id']
    ops = ops or Ops()
    live = ops.list(nb)
    res['before'] = len(live)
    state = dict(entry.get('coursework') or {})
    synced = entry.get('sources_synced') or []
    d = diff(srcs, state, live, synced)
    new = sum(1 for _s, ids in d['upload'] if not ids)
    if len(live) + new - len(d['legacy']) > limit - HEADROOM or entry.get('coursework_bundled'):
        singles = srcs
        srcs = bundle(srcs, (root or ROOT) / 'data' / 'notebooklm' / course / 'coursework')
        for b in srcs:
            b['legacy_paths'] = [s['path'] for s in singles]
        d = diff(srcs, state, live, synced, retire=True)
        res['bundled'] = True
    res['kept'] = d['kept']
    if not apply:
        res.update(uploaded=[s['title'] for s, ids in d['upload'] if not ids],
                   replaced=[s['title'] for s, ids in d['upload'] if ids],
                   legacy_removed=[t for _i, t, _n in d['legacy']], retired=[t for _i, t in d['retire']])
        res['after'] = res['before'] + len(res['uploaded']) - len(res['legacy_removed']) - len(res['retired'])
        return res
    for s, old in d['upload']:
        try:
            ops.add(nb, s)
        except Exception as e:
            res['errors'].append(f"{s['title']}: {e!r}"[:200])
            continue
        state[s['title']] = s['sha']
        (res['replaced'] if old else res['uploaded']).append(s['title'])
        for sid in old:
            try:
                ops.delete(nb, sid)
            except Exception as e:
                res['errors'].append(f"drop old {s['title']}: {e!r}"[:200])
    for sid, t, by in d['legacy']:
        if by not in state:
            continue                                     # its titled copy isn't in yet: keep the old one for now
        try:
            ops.delete(nb, sid)
            res['legacy_removed'].append(t)
        except Exception as e:
            res['errors'].append(f'remove {t}: {e!r}'[:200])
    if res['bundled'] and not res['errors']:             # the merged sources are all in → the single ones go
        for sid, t in d['retire']:
            try:
                ops.delete(nb, sid)
                res['retired'].append(t)
                state.pop(t, None)
            except Exception as e:
                res['errors'].append(f'remove {t}: {e!r}'[:200])
    if res['bundled'] and not res['errors']:
        for t in [t for t in state if t not in {x['title'] for x in srcs}]:
            state.pop(t, None)
    idx = load()                                         # another writer may have saved meanwhile
    e = idx.setdefault(course, {}).setdefault(scope, entry)
    e['coursework'] = state
    e['coursework_bundled'] = res['bundled']
    e['coursework_sync'] = datetime.now().isoformat(timespec='seconds')
    save(idx)
    try:
        res['after'] = len(ops.list(nb))
    except Exception:
        res['after'] = res['before'] + len(res['uploaded']) - len(res['legacy_removed']) - len(res['retired'])
    return res


def covered(course: str, root: Path | None = None) -> set[str]:
    """Paths this sync owns — the general Moodle-file sync leaves them to it."""
    try:
        return {s['path'] for s in plan(course, root)}
    except Exception:
        return set()


if __name__ == '__main__':
    import argparse
    import json
    import sys
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(description="homework → each course's NotebookLM 'all' notebook; reports only "
                                             "without --apply")
    ap.add_argument('courses', nargs='*')
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()
    from config.paths import ACTIVE_COURSES
    for ck in a.courses or ACTIVE_COURSES:
        r = sync(ck, apply=a.apply)
        print(json.dumps(r, ensure_ascii=False))
