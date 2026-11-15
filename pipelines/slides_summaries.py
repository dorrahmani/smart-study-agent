"""
pipelines/slides_summaries.py — a lecture nobody recorded still gets a summary: from its Moodle slides (20/10).

The owner 20/10: micro and macro have NO recordings on Panopto at all, so two of four courses got nothing. Each lecture
that took place (The owner's studies calendar: "<course> – שיעור", not "❌ בוטל", not a no-class day) and has no
transcript → its slides / lecture notes from Moodle (collectors/course_files.for_lecture, by the lecture's number
in the semester) → the usual summarizer, titled "הרצאה N (מהמצגת)". When a recording exists the regular pipeline
merges transcript + slides + the lecturer's notes ("השילוב של הכל"); this step never runs for that lecture.
A course that has had recordings this semester waits 2 days for one before falling back to the slides.
Runs in the nightly pipeline after the backfill, before the WhatsApp push (step 1c).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GRACE_DAYS = 2


def _course_label(ck: str) -> str:
    from config.paths import COURSE_NAMES
    return (COURSE_NAMES.get(ck) or ck).split(' (')[0].strip()


def is_lecture_of(summary: str, ck: str) -> bool:
    s = (summary or '').strip()
    if s.startswith('❌') or not re.search(r'שיעור|הרצאה', s) or re.search(r'תרגיל|תרגול', s):
        return False
    label = _course_label(ck)
    words = [w for w in re.findall(r'[א-ת]{3,}', label)][:2]
    return bool(words) and all(w in s for w in words)


def held_lectures(ck: str, today: date, start: date, events_on, no_class: set[str] | None = None,
                  now: datetime | None = None) -> list[tuple[int, date]]:
    """[(n, day)] — the n-th lecture of the semester that already took place (ended before `now`)."""
    now = now or datetime.now()
    out, n = [], 0
    d = start
    while d <= today:
        if d.isoformat() not in (no_class or set()):
            for ev in sorted(events_on(d) or [], key=lambda e: (e.get('start') or {}).get('dateTime', '')):
                if not is_lecture_of(ev.get('summary', ''), ck):
                    continue
                end = (ev.get('end') or {}).get('dateTime', '')
                if end and datetime.fromisoformat(end).replace(tzinfo=None) > now:
                    continue
                n += 1
                out.append((n, d))
                break
        d += timedelta(days=1)
    return out


def _has_srt(ck: str, day: date, panopto: Path) -> bool:
    """A recording of this lecture: that day, or a lecture recording of the same week (another group's — 23/10)."""
    if any(panopto.glob(f'{ck}_{day.isoformat()}*.srt')):
        return True
    w = _week0(day)
    for srt in panopto.glob(f'{ck}_*.srt'):
        rest = srt.stem[len(ck) + 1:]
        if re.match(r'\d{4}-\d{2}-\d{2}_', rest) and not re.search(r'תרגול|תרגיל|recitation', rest, re.I) \
                and _week0(date.fromisoformat(rest[:10])) == w:
            return True
    return False


def _recorded_course(ck: str, start: date, panopto: Path) -> bool:
    return any(p.name[len(ck) + 1:len(ck) + 11] >= start.isoformat() for p in panopto.glob(f'{ck}_*.srt'))


def _has_summary(ck: str, day: date, summaries: Path) -> bool:
    d = summaries / ck / 'lectures'
    return d.exists() and any(d.glob(f'{day.isoformat()}*.md'))


def todo(ck: str, today: date, events_on, start: date, panopto: Path, summaries: Path,
         no_class: set[str] | None = None, now: datetime | None = None) -> list[tuple[int, date]]:
    recorded = _recorded_course(ck, start, panopto)
    out = []
    for n, d in held_lectures(ck, today, start, events_on, no_class, now):
        if _has_srt(ck, d, panopto) or _has_summary(ck, d, summaries):
            continue
        if recorded and (today - d).days < GRACE_DAYS:          # a recording may still arrive
            continue
        out.append((n, d))
    return out


STANDIN = '(מהמצגת)'


def _week0(d: date) -> date:
    return d - timedelta(days=(d.weekday() + 1) % 7)


def standins_to_retire(ck: str, panopto: Path, summaries: Path) -> list[Path]:
    """23/10: a slides-only stand-in whose lecture recording arrived later (micro's 19/10 and macro's 21/10 lectures
    were on Panopto under another group) — the recording's summary already merges the same slides."""
    lec_weeks = set()
    for srt in panopto.glob(f'{ck}_*.srt'):
        rest = srt.stem[len(ck) + 1:]
        if not re.match(r'\d{4}-\d{2}-\d{2}_', rest) or re.search(r'תרגול|תרגיל|recitation', rest, re.I):
            continue
        lec_weeks.add(_week0(date.fromisoformat(rest[:10])))
    d = summaries / ck / 'lectures'
    # 14/11: only once the recording's own summary is there — software 1's Lecture 3 stand-in was moved aside while the
    # recording had no summary yet, and the week was left with none
    summed = set()
    for md in d.glob('*.md') if d.exists() else []:
        if STANDIN not in md.name and re.match(r'\d{4}-\d{2}-\d{2} ', md.name):
            summed.add(_week0(date.fromisoformat(md.name[:10])))
    out = []
    for md in sorted(d.glob(f'*{STANDIN}*.md')) if d.exists() else []:
        try:
            w = _week0(date.fromisoformat(md.name[:10]))
            if w in lec_weeks and w in summed:
                out.append(md)
        except ValueError:
            continue
    return out


def misfiled(ck: str, panopto: Path, summaries: Path) -> list[tuple[Path, list[Path]]]:
    """26/10: micro B's lecture was summarised and sent as Software 1's (searched by name, no folder yet). A summary
    or recording under one course whose title names ANOTHER configured course (its number or name) and not this
    one — (summary md, its recordings) to move aside, so the real lecture's slides summary isn't blocked."""
    import yaml
    try:
        courses = yaml.safe_load((ROOT / 'config' / 'courses.yaml').read_text(encoding='utf-8'))['courses']
    except (OSError, ValueError, KeyError, TypeError):
        return []

    def norm(t: str) -> str:
        return re.sub(r'\s+', ' ', (t or '').replace('׳', "'").replace('"', '').replace('״', '')).strip()

    def marks(k: str) -> list[str]:
        c = courses.get(k) or {}
        code = str(c.get('course_code') or '').lstrip('0')
        return [m for m in ([code] if len(code) >= 6 else []) + [norm(c.get('name', ''))] if m]
    mine = marks(ck)
    # names as they show in file titles: "מיקרו כלכלה ב - …" (the geresh is dropped from filenames)
    others = {m for k in courses if k != ck for m in marks(k) + [norm(re.sub(r"[׳']", '', courses[k].get('name', '')))]
              if m and m not in mine and not any(m in x for x in mine)}

    def foreign(stem: str) -> bool:
        t = norm(stem)
        return not any(m in t for m in mine) and any(re.search(rf'(?:^|[\s\-_]){re.escape(m)}(?:$|[\s\-_])', t)
                                                    for m in others)
    out = []
    for sub in ('lectures', 'tutorials'):
        d = summaries / ck / sub
        for md in sorted(d.glob('*.md')) if d.exists() else []:
            if foreign(md.stem[13:] if re.match(r'\d{4}-\d{2}-\d{2} - ', md.stem) else md.stem):
                srts = [p for p in panopto.glob(f'{ck}_{md.stem[:10]}_*.srt') if foreign(p.stem[len(ck) + 12:])]
                out.append((md, srts))
    # 02/11: the recording alone (its summary already moved) came back from Drive, counted as "this week's lecture
    # recording", and the real Software 1 lecture 2 (from the slides) was retired as a stand-in → (None, [srt])
    listed = {p for _, srts in out for p in srts}
    out += [(None, [p]) for p in sorted(panopto.glob(f'{ck}_*.srt'))
            if p not in listed and re.match(r'\d{4}-\d{2}-\d{2}_', p.stem[len(ck) + 1:]) and foreign(p.stem[len(ck) + 12:])]
    return out


def move_srt_aside(srt: Path, remote: str = 'gdrive:smart-study-agent/data') -> None:
    """data/panopto/<x>.srt → data/panopto/replaced/ — here and on Drive (else the nightly Drive → local copy)."""
    import subprocess
    (srt.parent / 'replaced').mkdir(exist_ok=True)
    srt.replace(srt.parent / 'replaced' / srt.name)
    subprocess.run(['rclone', 'moveto', f'{remote}/panopto/{srt.name}', f'{remote}/panopto/replaced/{srt.name}', '--quiet'],
                   check=False, timeout=120)


def retire(md: Path, log=print, remote: str = 'gdrive:smart-study-agent/data') -> None:
    """Move the stand-in (md + pdf) to <course>/replaced/ — here and on Drive (else the nightly Drive → local copy
    brings it back) — and drop its vault note. Moved, never deleted."""
    import subprocess
    ck = md.parts[-3]
    dest = md.parent.parent / 'replaced'
    dest.mkdir(parents=True, exist_ok=True)
    for f in (md, md.with_suffix('.pdf')):
        if not f.exists():
            continue
        f.replace(dest / f.name)
        rel = f'summaries/{ck}/{md.parent.name}/{f.name}'
        subprocess.run(['rclone', 'moveto', f'{remote}/{rel}', f'{remote}/summaries/{ck}/replaced/{f.name}', '--quiet'],
                       check=False, timeout=120)
    try:
        retire_vault_note(ck, md.stem, remote=remote.rsplit('/', 1)[0] + '/vault')
    except Exception as e:
        log(f'  ⚠️ vault cleanup: {e}')
    log(f'  ♻️ {ck}: "{md.stem}" replaced by the recording\'s summary')


def retire_vault_note(ck: str, stem: str, vault: Path | None = None, remote: str = 'gdrive:smart-study-agent/vault',
                      run=None) -> list[str]:
    """02/11: retired summaries stayed in Obsidian — the note sits in a subfolder (the old cleanup looked only at the
    course folder's top), and the PC pull is copy-only, so a removal never reached the owner's Obsidian. The note moves to
    <vault>/.replaced/ (Obsidian hides dot-folders) here and on Drive, and is listed in vault_removed.txt, which
    scripts/vault_pull.ps1 reads to move it aside on the PC too. → the moved relative paths."""
    import subprocess
    from config.paths import VAULT_DIR, COURSE_NAMES
    vault = vault or VAULT_DIR
    run = run or (lambda args: subprocess.run(args, check=False, timeout=120))
    base = vault / COURSE_NAMES.get(ck, ck)
    moved = []
    for f in sorted(base.rglob('*')) if base.exists() else []:
        if not f.is_file() or f.stem != stem or '.replaced' in f.parts:
            continue
        rel = f.relative_to(vault).as_posix()
        dest = vault / '.replaced' / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        f.replace(dest)
        run(['rclone', 'moveto', f'{remote}/{rel}', f'{remote}/.replaced/{rel}', '--quiet'])
        moved.append(rel)
    if moved:
        lst = vault / 'vault_removed.txt'
        have = lst.read_text(encoding='utf-8').splitlines() if lst.exists() else []
        lst.write_text('\n'.join(dict.fromkeys(have + moved)) + '\n', encoding='utf-8')
    return moved


def run(courses: list[str] | None = None, budget_usd: float = 1.5, log=print, today: date | None = None) -> dict:
    from config.paths import ACTIVE_COURSES, COURSE_NAMES
    from collectors.course_files import SEMESTER_START, for_lecture
    from integrations import gcal
    from generators.summarizer import AISummarizer
    import json
    today = today or date.today()
    if not gcal.configured():
        return {'generated': 0, 'note': 'no calendar'}
    try:
        no_class = set(json.loads((ROOT / 'data' / 'no_class_days.json').read_text(encoding='utf-8')).get('dates', []))
    except (OSError, ValueError):
        no_class = set()
    cache: dict = {}

    def events_on(d):
        if d not in cache:
            cache[d] = gcal.events_on(d)
        return cache[d]

    panopto, summaries = ROOT / 'data' / 'panopto', ROOT / 'data' / 'summaries'
    res = {'generated': 0, 'cost_usd': 0.0, 'created': [], 'no_slides': []}
    summarizer = None
    for ck in courses or list(ACTIVE_COURSES):
        for md, srts in misfiled(ck, panopto, summaries):      # first — a foreign recording must not retire a stand-in
            try:
                for srt in srts:
                    move_srt_aside(srt)
                if md:
                    retire(md, log)
                name = md.name if md else srts[0].name
                res.setdefault('misfiled', []).append(name)
                log(f'  🚫 {ck}: "{name}" belongs to another course — moved aside')
            except Exception as e:
                log(f'  ⚠️ misfiled {ck}: {e}')
        for md in standins_to_retire(ck, panopto, summaries):
            try:
                retire(md, log)
                res.setdefault('retired', []).append(md.name)
            except Exception as e:
                log(f'  ⚠️ retire {md.name}: {e}')
        for n, d in todo(ck, today, events_on, SEMESTER_START, panopto, summaries, no_class):
            if res['cost_usd'] >= budget_usd:
                log(f'  💸 slides budget used (${res["cost_usd"]:.2f}) — the rest next run')
                return res
            title = f'הרצאה {n}'
            slides = for_lecture(ck, d, title)
            if not slides:
                res['no_slides'].append(f'{ck} {d} {title}')
                continue
            summarizer = summarizer or AISummarizer()
            full = f'{title} (מהמצגת)'
            out = Path(AISummarizer.build_summary_path(ck, 'lecture', full, d.isoformat()))
            try:
                summary, sources = summarizer.generate_summary(
                    course_name=COURSE_NAMES.get(ck, ck), lecture_title=full, caption_path=None,
                    pdf_path=slides, pdf_source_type='moodle', course_key=ck)
            except Exception as e:
                log(f'  ⚠️ slides summary {ck} {d}: {e}')
                continue
            if not summary or len(summary) < 1500 or summary.startswith('# שגיאה'):
                log(f'  ⚠️ slides summary {ck} {d} too short — skipped')
                continue
            summarizer.save_summary(summary=summary, output_path=str(out), course_name=COURSE_NAMES.get(ck, ck),
                                    lecture_title=full, sources=sources)
            res['generated'] += 1
            res['cost_usd'] += 0.15
            res['created'].append({'course_key': ck, 'title': full, 'md_path': str(out)})
            log(f'  🖥️ {ck}: {full} ({d}) from {len(slides)} Moodle files')
    return res
