"""
vault_sync/maps.py — the graph of the second brain: a map per area, a hub per course, dead links in old week notes (17/11).

The 17/11 audit of the live vault (1,904 notes): 720 announcements, 129 assignments and 167 exams had no link at all,
11 notes of "🧠 המוח" were orphans, and the course "_index" notes are dataview tables, which Obsidian's graph doesn't count.
273 links were dead, 258 of them in old week digests (Moodle "המשך..." announcements that were merged away later).

This writes, all with `source: assistant` (so the bot owns them and never touches anything else):
    🗺️ מפת המוח.md                          the root map: every course hub, every area map, the assistant, the brain, his notes
    <course folder>/🧭 <course> — מפה.md      the course's lessons (summary · transcript), weeks, assignments, announcements, exams
    📢 הודעות/📢 הודעות — מפה.md  · 📋 מטלות/📋 מטלות — מפה.md · 🧾 מבחנים/🧾 מבחנים — מפה.md
    📅 שבועות/📅 שבועות — מפה.md  · מושגים/🧠 מושגים — מפה.md · 🧠 המוח/🧠 המוח — מפה.md
and repair_weeks(): in a week digest (type: weekly_digest, generated) a link to a note that doesn't exist becomes its text.

Links are written with the folder path ([[מאקרו כלכלה/2026-10-21 - …|…]]): 220 note names exist twice (a lesson summary and its
transcript share a name), so a bare [[name]] could open the wrong one.
Deterministic, no model, no network: $0. Only notes whose text changed are written (vault_sync/notewriter).
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from vault_sync import privacy
from vault_sync.notewriter import Writer, bot_owned

ROOT_MAP = '🗺️ מפת המוח'
A_ANN, A_ASG, A_EXAM, A_WEEKS, A_CONC, A_BRAIN = '📢 הודעות', '📋 מטלות', '🧾 מבחנים', '📅 שבועות', 'מושגים', '🧠 המוח'
AREA_MAPS = {A_ANN: '📢 הודעות — מפה', A_ASG: '📋 מטלות — מפה', A_EXAM: '🧾 מבחנים — מפה', A_WEEKS: '📅 שבועות — מפה',
             A_CONC: '🧠 מושגים — מפה', A_BRAIN: '🧠 המוח — מפה'}
TRANSCRIPTS = '🎙️ תמלולים'
_FM_RX = re.compile(r'^---\s*\n(.*?)\n---', re.S)
_CK_RX = re.compile(r'^course_key:\s*[\'"]?([\w-]+)', re.M)
_LINK_RX = re.compile(r'\[\[([^\]|#]+)(#[^\]|]*)?(?:\|([^\]]*))?\]\]')


def _plink(rel_no_ext: str, alias: str = '') -> str:
    alias = (alias or Path(rel_no_ext).name).replace('|', ' ').replace(']', ' ')
    return f'[[{rel_no_ext}|{alias}]]'


def _md(d: Path) -> list[Path]:
    return sorted(p for p in d.glob('*.md')) if d.is_dir() else []


def _ck_of_note(p: Path) -> str:
    try:
        head = p.read_text(encoding='utf-8', errors='replace')[:1500]
    except OSError:
        return ''
    m = _FM_RX.match(head)
    c = _CK_RX.search(m.group(1)) if m else None
    return c.group(1) if c else ''


def _front(typ: str, title: str, **kw) -> dict:
    fm = {'type': typ, 'title': title}
    fm.update({k: v for k, v in kw.items() if v not in (None, '', [], {})})
    fm['tags'] = list(dict.fromkeys(['MOC'] + list(kw.get('tags') or [])))
    fm['source'] = 'assistant'
    return fm


class Maps:
    def __init__(self, vault: Path, writer: Writer):
        from config.paths import COURSE_NAMES, VAULT_COURSE_FOLDER_TO_KEY
        self.v = Path(vault)
        self.w = writer
        self.names = dict(COURSE_NAMES)
        self.folder_of: dict[str, str] = {}                 # course key → the course folder that exists
        for f, ck in VAULT_COURSE_FOLDER_TO_KEY.items():
            if (self.v / f).is_dir() and ck not in self.folder_of:
                self.folder_of[ck] = f
        self.hub: dict[str, str] = {}                      # course key → its hub note (path, no extension)
        for ck, f in self.folder_of.items():               # a course name with a quote (מדמ"ח): keep the file name safe
            safe = re.sub(r'[\\/:*?"<>|#^]+', '', self.names.get(ck, f))
            self.hub[ck] = f'{f}/🧭 {safe} — מפה'

    def course(self, ck: str) -> str:
        return _plink(self.hub[ck], self.names.get(ck, ck)) if ck in self.hub else self.names.get(ck, ck or 'ללא קורס')

    # ── collect ──
    def _by_course(self, area: str) -> dict[str, list[Path]]:
        """area (📢 / 📋: one folder per course key) → {course key: notes}."""
        out: dict[str, list[Path]] = defaultdict(list)
        base = self.v / area
        for d in sorted(base.iterdir()) if base.is_dir() else []:
            if d.is_dir() and not d.name.startswith('.'):
                out[d.name] += [p for p in sorted(d.rglob('*.md')) if '.replaced' not in p.parts]
        return out

    def _exams(self) -> dict[str, list[Path]]:
        """🧾 מבחנים/<course display name>/… → by the course key most of the folder's notes carry."""
        out: dict[str, list[Path]] = defaultdict(list)
        base = self.v / A_EXAM
        for d in sorted(base.iterdir()) if base.is_dir() else []:
            if not d.is_dir() or d.name.startswith('.'):
                continue
            notes = [p for p in sorted(d.rglob('*.md')) if '.replaced' not in p.parts]
            keys = [k for k in (_ck_of_note(p) for p in notes) if k]
            ck = max(set(keys), key=keys.count) if keys else ''
            out[ck or d.name] += notes
        return out

    def _lessons(self, ck: str) -> list[tuple[str, Path, bool]]:
        f = self.folder_of.get(ck)
        if not f:
            return []
        d = self.v / f
        trans = {p.stem for p in _md(d / TRANSCRIPTS)}
        return [(p.stem[:10], p, p.stem in trans) for p in _md(d) if re.match(r'\d{4}-\d{2}-\d{2} - ', p.stem)]

    def _rel(self, p: Path) -> str:
        return p.relative_to(self.v).with_suffix('').as_posix()

    # ── write ──
    def write_course_hubs(self, ann: dict, asg: dict, exams: dict) -> None:
        from datetime import date
        for ck, folder in self.folder_of.items():
            name = self.names.get(ck, folder)
            lessons = self._lessons(ck)
            body = [f'# 🧭 {name}', '', f'כל מה שיש בכספת על הקורס, כקישורים. הטבלאות (dataview): {_plink(f"{folder}/_index", "_index")}.',
                    f'↩️ {_plink(ROOT_MAP)}', '']
            weeks = sorted({'%d-W%02d' % date.fromisoformat(d).isocalendar()[:2] for d, _, _ in lessons})
            if weeks:
                body += ['## 📅 שבועות', ' · '.join(_plink(f'{A_WEEKS}/{w}', w) for w in weeks if (self.v / A_WEEKS / f'{w}.md').exists())
                         or '—', '']
            body += ['## 🎓 שיעורים ותרגולים'] + ([f'- {d} · {_plink(self._rel(p), p.stem[13:] or p.stem)}'
                                                   + (f' · {_plink(f"{folder}/{TRANSCRIPTS}/{p.stem}", "🎙️ תמלול")}' if t else '')
                                                   for d, p, t in sorted(lessons, reverse=True)] or ['- —']) + ['']
            orphans_t = [p for p in _md(self.v / folder / TRANSCRIPTS) if p.stem not in {x.stem for _, x, _ in lessons}]
            if orphans_t:
                body += ['## 🎙️ תמלולים נוספים'] + [f'- {_plink(self._rel(p))}' for p in orphans_t] + ['']
            try:                                            # 18/11 the exam statistics note (agent/exam_plan), when built
                from agent import exam_plan
                plan = exam_plan.note_rel(ck)
                if (self.v / plan).exists():
                    body += ['## 📊 על מה לשים את הכסף במבחן', f'- {_plink(plan[:-3], "סטטיסטיקת המבחנים: נושאים, מה חוזר, מבנה")}', '']
            except Exception as e:
                print(f'  ⚠️ maps exam plan: {e!r}'[:200])
            try:                                            # 19/11 the grader's feedback per item (agent/grades), when any
                from agent import grades
                fb = grades.course_note_rel(ck)
                if (self.v / fb).exists():
                    body += ['## 📝 משובים וציונים', f'- {_plink(fb[:-3], "מה הבודקים כתבו ואיפה ירדו נקודות")}', '']
            except Exception as e:
                print(f'  ⚠️ maps feedback: {e!r}'[:200])
            for title, notes in (('📋 מטלות', asg.get(ck, [])), ('📢 הודעות', ann.get(ck, [])), ('🧾 מבחנים', exams.get(ck, []))):
                if notes:
                    body += [f'## {title} ({len(notes)})'] + [f'- {_plink(self._rel(p))}' for p in sorted(notes, key=lambda p: p.stem,
                                                                                                         reverse=True)] + ['']
            self.w.note(f'{self.hub[ck]}.md', _front('moc', f'{name} — מפה', course_key=ck, lessons=len(lessons),
                                                    tags=[ck, 'קורס']), '\n'.join(body))

    def write_area(self, area: str, groups: dict[str, list[Path]], intro: str) -> None:
        body = [f'# {AREA_MAPS[area]}', '', intro, f'↩️ {_plink(ROOT_MAP)}', '']
        for key in sorted(groups, key=lambda k: (k not in self.hub, self.names.get(k, k))):
            notes = groups[key]
            if not notes:
                continue
            if key in self.hub:                             # the course hub lists every note: here only the newest
                body += [f'## {self.course(key)} ({len(notes)})'] + [f'- {_plink(self._rel(p))}' for p in
                                                                      sorted(notes, key=lambda p: p.stem, reverse=True)[:8]] + ['']
            else:                                           # no course hub (an old course, "unknown"): every note here
                body += [f'## {self.names.get(key, key)} ({len(notes)})'] + [f'- {_plink(self._rel(p))}' for p in
                                                                              sorted(notes, key=lambda p: p.stem, reverse=True)] + ['']
        self.w.note(f'{area}/{AREA_MAPS[area]}.md', _front('moc', AREA_MAPS[area], area=area, count=sum(map(len, groups.values())),
                                                           tags=[area.split(' ', 1)[-1]]), '\n'.join(body))

    def write_weeks(self) -> None:
        weeks = [p for p in _md(self.v / A_WEEKS) if re.fullmatch(r'\d{4}-W\d{2}', p.stem)]
        body = [f'# {AREA_MAPS[A_WEEKS]}', '', 'תקציר שבועי לכל שבוע (נכתב בכל לילה ראשון). השבוע הנוכחי של העוזר: '
                f'{_plink(f"{privacy.AREA}/✅ משימות/📅 השבוע", "📅 השבוע")}.', f'↩️ {_plink(ROOT_MAP)}', ''] + [
                f'- {_plink(self._rel(p))}' for p in sorted(weeks, reverse=True)]
        self.w.note(f'{A_WEEKS}/{AREA_MAPS[A_WEEKS]}.md', _front('moc', AREA_MAPS[A_WEEKS], count=len(weeks), tags=['שבוע']),
                    '\n'.join(body))

    def write_concepts(self) -> None:
        notes = [p for p in _md(self.v / A_CONC) if p.stem != AREA_MAPS[A_CONC]]
        by: dict[str, list] = defaultdict(list)
        for p in notes:
            by[(p.stem[:1] or '#').upper()].append(p)
        body = [f'# {AREA_MAPS[A_CONC]}', '', f'{len(notes)} מושגים, לפי האות הראשונה. כל מושג מקושר לשיעורים שבהם הופיע.',
                f'↩️ {_plink(ROOT_MAP)}', '']
        for k in sorted(by):
            body += [f'## {k}', ' · '.join(_plink(self._rel(p)) for p in sorted(by[k], key=lambda p: p.stem)), '']
        self.w.note(f'{A_CONC}/{AREA_MAPS[A_CONC]}.md', _front('moc', AREA_MAPS[A_CONC], count=len(notes), tags=['מושגים']),
                    '\n'.join(body))

    def write_brain(self) -> None:
        d = self.v / A_BRAIN
        notes = [p for p in sorted(d.rglob('*.md')) if '.replaced' not in p.parts and p.stem != AREA_MAPS[A_BRAIN]] if d.is_dir() else []
        by: dict[str, list] = defaultdict(list)
        for p in notes:
            parts = p.relative_to(d).parts
            by[parts[0] if len(parts) > 1 else ''].append(p)
        body = [f'# {AREA_MAPS[A_BRAIN]}', '', 'מה שהבוט יודע עליך, מיילים, מסמכים, והשיחות שלך עם קלוד לפי פרויקט.', f'↩️ {_plink(ROOT_MAP)}', '']
        for k in sorted(by):
            body += ([f'## {k}'] if k else []) + [f'- {_plink(self._rel(p))}' for p in by[k]] + ['']
        self.w.note(f'{A_BRAIN}/{AREA_MAPS[A_BRAIN]}.md', _front('moc', AREA_MAPS[A_BRAIN], count=len(notes), tags=['מוח'],
                                                                 private=None), '\n'.join(body))

    def write_root(self, counts: dict[str, int]) -> None:
        body = ['# 🗺️ מפת המוח', '', 'נקודת הכניסה לכספת. כל ההערות כאן נכתבות על ידי המערכת (source: assistant) ונבנות מחדש בלילה; '
                'ההערות שלך בתיקייה ' + _plink(f'{privacy.MINE}/Inbox', privacy.MINE) + ' — הבוט קורא אותן ולא כותב בהן.', '',
                '## 🎓 הקורסים'] + [f'- {self.course(ck)}' for ck in sorted(self.hub, key=lambda k: self.names.get(k, k))] + [
                '', '## 🗂️ תחומים'] + [f'- {_plink(f"{a}/{m}", m)} ({counts.get(a, 0)})' for a, m in AREA_MAPS.items()
                                       if (self.v / a).is_dir()] + [
                '', '## 👩‍💼 העוזר', f'- {_plink(f"{privacy.AREA}/🏠 העוזר", "🏠 העוזר")} — אנשים, מסמכים, משימות, יומן יומי',
                f'- {_plink(f"{A_BRAIN}/עליי", "עליי")} — מה הבוט יודע עליך']
        for extra in ('🏠 בית', '🏠 Dashboard', '🌉 גשרי מושגים', '📅 מטלות'):
            if (self.v / f'{extra}.md').exists():
                body.append(f'- {_plink(extra)}')
        self.w.note(f'{ROOT_MAP}.md', _front('moc', 'מפת המוח', tags=['בית']), '\n'.join(body))


def _names(vault: Path) -> tuple[set[str], set[str]]:
    stems, rels = set(), set()
    for p in vault.rglob('*'):
        if '.obsidian' in p.parts or '.replaced' in p.parts or not p.is_file():
            continue
        rel = p.relative_to(vault).as_posix()
        stems.add(p.stem if p.suffix == '.md' else p.name)
        rels.add(rel[:-3] if rel.endswith('.md') else rel)
    return stems, rels


def repair_weeks(vault: Path, dry: bool = False) -> int:
    """A week digest's link to a note that no longer exists → its plain text (the alias, or the name). → notes fixed.
    Only notes of `type: weekly_digest` (generated by pipelines/weekly_digest); never anything else."""
    d = Path(vault) / A_WEEKS
    if not d.is_dir():
        return 0
    stems, rels = _names(Path(vault))

    def alive(target: str) -> bool:
        t = target.strip().replace("''", "'")
        t = t[:-3] if t.endswith('.md') else t
        return t in rels or t.split('/')[-1] in stems

    fixed = 0
    for p in _md(d):
        txt = p.read_text(encoding='utf-8', errors='replace')
        m = _FM_RX.match(txt)
        if not m or not re.search(r'^type:\s*weekly_digest\s*$', m.group(1), re.M) or bot_owned(txt):
            continue
        new = _LINK_RX.sub(lambda x: x.group(0) if alive(x.group(1)) else (x.group(3) or x.group(1)).strip(), txt)
        if new != txt:
            fixed += 1
            if not dry:
                p.write_text(new, encoding='utf-8')
    return fixed


def build(vault: Path | None = None, out_root: Path | None = None) -> dict:
    """All maps. → writer stats + 'weeks_fixed'."""
    if vault is None:
        from config.paths import VAULT_DIR
        vault = VAULT_DIR
    vault = Path(vault)
    w = Writer(vault, out_root=out_root)
    m = Maps(vault, w)
    ann, asg, exams = m._by_course(A_ANN), m._by_course(A_ASG), m._exams()
    counts = {A_ANN: sum(map(len, ann.values())), A_ASG: sum(map(len, asg.values())), A_EXAM: sum(map(len, exams.values()))}
    steps = (lambda: m.write_course_hubs(ann, asg, exams),
             lambda: m.write_area(A_ANN, ann, 'הודעות המודל והמרצים, לפי קורס. כל ההודעות של קורס: במפת הקורס.'),
             lambda: m.write_area(A_ASG, asg, 'מטלות ותרגילי בית, לפי קורס.'),
             lambda: m.write_area(A_EXAM, exams, 'מבחני עבר (תמלול), לפי קורס. על מה לשים את הכסף בכל קורס: '
                                  + _plink('👩‍💼 העוזר/🎓 לימודים/📊 מבחנים/📊 מבחנים', '📊 מבחנים') + '.'),
             m.write_weeks, m.write_concepts, m.write_brain)
    for step in steps:
        try:
            step()
        except Exception as e:                              # one broken area never stops the rest
            print(f'  ⚠️ maps: {e!r}'[:300])
    for a in (A_WEEKS, A_CONC, A_BRAIN):
        counts[a] = len(list((vault / a).rglob('*.md'))) if (vault / a).is_dir() else 0
    m.write_root(counts)
    fixed = 0
    try:
        fixed = repair_weeks(vault, dry=out_root is not None)
    except Exception as e:
        print(f'  ⚠️ weeks repair: {e!r}'[:300])
    return {**dict(w.stats), 'written': w.written, 'weeks_fixed': fixed}
