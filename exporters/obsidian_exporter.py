"""
obsidian_exporter.py — exports summaries, quizzes and assignments to the Obsidian Vault.

Vault structure:
  📁 Smart Study Agent/
    📁 מיקרו כלכלה א'/
      📄 2026-04-26 - פתרון תרגיל 1.md
      📄 2026-04-26 - קבלת החלטות כלכליות.md
      📄 _index.md          ← Dataview index for the course
    📁 מדמ"ח/
      ...
    📁 חידונים/
      📄 2026-04-26 - מיקרו - חידון.md
    📄 🏠 בית.md            ← a nice home page with course cards
    📄 📅 מטלות.md           ← assignments board
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except AttributeError:
    pass

import yaml

ROOT = Path(__file__).parent.parent

# ── Vault path — shared with config/paths.py so everyone agrees on the location ─────────
# Order of priority:
#  1. OBSIDIAN_VAULT_PATH (env, backward compatibility)
#  2. SMART_STUDY_VAULT   (env, same variable as config/paths.py)
#  3. ~/Documents/Obsidian Vault (default cross-platform)
import os as _os
_vault_env = _os.environ.get('OBSIDIAN_VAULT_PATH') or _os.environ.get('SMART_STUDY_VAULT')
if _vault_env:
    VAULT_ROOT = Path(_vault_env)
else:
    VAULT_ROOT = Path.home() / 'Documents' / 'Obsidian Vault'
STUDY_DIR = VAULT_ROOT / 'Smart Study Agent'   # root folder inside the vault

SUMMARIES_DIR = ROOT / 'data' / 'summaries'
QUIZZES_DIR   = ROOT / 'data' / 'quizzes'

# Loading course names
with open(ROOT / 'config' / 'courses.yaml', encoding='utf-8') as _f:
    _cfg = yaml.safe_load(_f)
COURSE_NAMES: dict[str, str] = {
    k: v.get('name', k) for k, v in _cfg.get('courses', {}).items()
}

# Emojis for courses
COURSE_EMOJI = {
    'microeconomics':   '📈',
    'linear_algebra_2': '🔢',
    'calculus_2':       '∫',
    'discrete_math_2':  '🧩',
    'intro_to_cs':      '💻',
}

# Course colors (for the home page)
COURSE_COLOR = {
    'microeconomics':   '#e8a838',
    'linear_algebra_2': '#5b8dee',
    'calculus_2':       '#48c774',
    'discrete_math_2':  '#b06de8',
    'intro_to_cs':      '#f06292',
}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _strip_metadata_block(md: str) -> str:
    """Removes the metadata block (API costs, materials) from the beginning of the summary."""
    parts = re.split(r'\n---\n', md, maxsplit=1)
    return parts[1].lstrip('\n') if len(parts) == 2 else md


def _frontmatter(data: dict) -> str:
    """Builds the YAML frontmatter for an Obsidian file."""
    lines = ['---']
    for k, v in data.items():
        if isinstance(v, list):
            lines.append(f'{k}:')
            for item in v:
                lines.append(f'  - {item}')
        elif isinstance(v, str) and '\n' in v:
            lines.append(f'{k}: |-')
            for line in v.splitlines():
                lines.append(f'  {line}')
        else:
            # quote strings that need it
            val = str(v) if not isinstance(v, str) else v
            if any(c in val for c in [':', '#', '[', ']', '{', '}']):
                val = f'"{val}"'
            lines.append(f'{k}: {val}')
    lines.append('---\n')
    return '\n'.join(lines)


def _safe_dirname(name: str) -> str:
    """Removes characters that are illegal in Windows folder names."""
    return re.sub(r'[\\/:*?"<>|]', '', name).strip()


def _course_dir(course_key: str) -> Path:
    name = _safe_dirname(COURSE_NAMES.get(course_key, course_key))
    d = STUDY_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _parse_date(stem: str) -> datetime | None:
    """Extracts a date from a file's stem."""
    date_str = stem[:10]
    if date_str.count('-') == 2:
        try:
            return datetime.strptime(date_str, '%Y-%m-%d')
        except ValueError:
            pass
    return None


def _extract_topics(body: str) -> list[str]:
    """Extracts ## and ### headings from the summary body as topics."""
    topics = []
    for line in body.splitlines():
        m = re.match(r'^#{2,3}\s+(.+)', line)
        if m:
            topic = m.group(1).strip()
            # Filter out headings that are too generic
            if len(topic) > 3 and topic not in ('סיכום', 'מבוא', 'הקדמה', 'רקע', 'דוגמאות', 'תרגילים'):
                topics.append(topic)
    return topics[:8]  # maximum 8 topics


# ─────────────────────────────────────────────────────────────────────────────
# Wikilink engine
# ─────────────────────────────────────────────────────────────────────────────

_RETIRED_DIRS = {'replaced', '.replaced', 'misfiled', '.trash'}


def _live_summaries(course_sum_dir: Path) -> list[Path]:
    """17/11: the course's summaries, minus the retired ones. pipelines/slides_summaries.retire() moves a replaced summary into
    <course>/replaced/; rglob picked it up again, so 12 retired lessons were written back into the vault on 16/11 and the PC
    pull moved them aside again on every run."""
    return sorted(p for p in course_sum_dir.rglob('*.md')
                  if not any(part in _RETIRED_DIRS for part in p.relative_to(course_sum_dir).parts[:-1]))


def _build_all_stems(course_key: str) -> dict[str, dict]:
    """
    Builds a map of all the summaries in a course: stem → {date, type, topics, obsidian_stem}
"""
    result = {}
    course_sum_dir = SUMMARIES_DIR / course_key
    if not course_sum_dir.exists():
        return result

    for md_file in _live_summaries(course_sum_dir):
        stem = md_file.stem
        date = _parse_date(stem)
        type_folder = md_file.parent.name   # "lectures" | "tutorials"
        kind = 'הרצאה' if type_folder == 'lectures' else 'תרגול'

        # Read the body to extract topics
        try:
            content = md_file.read_text(encoding='utf-8', errors='replace')
            body = _strip_metadata_block(content)
            topics = _extract_topics(body)
        except Exception:
            topics = []

        result[stem] = {
            'date': date,
            'type': kind,
            'topics': topics,
            'path': md_file,
        }

    return result


def _find_related_notes(stem: str, all_stems: dict[str, dict]) -> list[str]:
    """
    Finds related lectures/tutorials:
    1. same week (±7 days), different type (lecture↔tutorial)
    2. neighboring lectures/tutorials (±3 days) of the same type
"""
    current = all_stems.get(stem)
    if not current or not current['date']:
        return []

    current_date = current['date']
    current_type = current['type']
    related = []

    for other_stem, other in all_stems.items():
        if other_stem == stem or not other['date']:
            continue

        delta = abs((other['date'] - current_date).days)

        # Lecture ↔ tutorial in the same week (up to 7 days apart)
        if other['type'] != current_type and delta <= 7:
            related.append(other_stem)
        # Same type, the day before/after (double lecture / follow-up tutorial)
        elif other['type'] == current_type and delta <= 3 and delta > 0:
            related.append(other_stem)

    # Sort: closer first
    related.sort(key=lambda s: abs((all_stems[s]['date'] - current_date).days))
    return related[:6]  # maximum 6 links


def _build_related_section(stem: str, all_stems: dict[str, dict], course_name: str) -> str:
    """Builds a 'ראה גם' (see also) section with wikilinks."""
    related = _find_related_notes(stem, all_stems)
    if not related:
        return ''

    lines = ['\n---\n\n## 🔗 ראה גם\n']
    for r_stem in related:
        r = all_stems[r_stem]
        type_icon = '📖' if r['type'] == 'הרצאה' else '✏️'
        course_dir_name = _safe_dirname(course_name)
        lines.append(f'- {type_icon} [[{r_stem}]] — {r["type"]}')

    return '\n'.join(lines)


def _inject_topic_links(body: str, all_stems: dict[str, dict], current_stem: str, course_name: str) -> str:
    """
    Adds wikilinks to topics that are mentioned in the summary body and appear in other summaries.
    Adds only the first wikilink for each topic (and not from the frontmatter).
"""
    # Build a map: topic → the stem where it is defined (main heading)
    topic_to_note: dict[str, str] = {}
    for other_stem, other in all_stems.items():
        if other_stem == current_stem:
            continue
        for topic in other['topics']:
            if len(topic) >= 5 and topic not in topic_to_note:
                topic_to_note[topic] = other_stem

    if not topic_to_note:
        return body

    # Add wikilinks — only the first occurrence of each topic in the body (not inside headings)
    lines = body.splitlines()
    already_linked: set[str] = set()
    result_lines = []

    for line in lines:
        # Don't touch headings and existing wikilink lines
        if line.startswith('#') or '[[' in line:
            result_lines.append(line)
            continue

        modified = line
        for topic, note_stem in topic_to_note.items():
            if topic in already_linked:
                continue
            # Check that the topic is mentioned in the line (outside existing links)
            if re.search(re.escape(topic), modified):
                modified = modified.replace(topic, f'[[{note_stem}|{topic}]]', 1)
                already_linked.add(topic)

        result_lines.append(modified)

    return '\n'.join(result_lines)


# ─────────────────────────────────────────────────────────────────────────────
# Export: Summaries
# ─────────────────────────────────────────────────────────────────────────────

def _extract_cost(content: str) -> float | None:
    m = re.search(r'סה"כ:\s*\$([0-9.]+)', content)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def _extract_panopto_url(content: str) -> str | None:
    m = re.search(r'(https://[^\s)]*panopto[^\s)]*)', content, re.IGNORECASE)
    return m.group(1) if m else None


def _detect_pdf_path(md_path: Path) -> str | None:
    """If a sibling .pdf exists with the same stem, return its path."""
    candidate = md_path.with_suffix('.pdf')
    return str(candidate) if candidate.exists() else None


def _detect_transcription_method(content: str) -> str | None:
    if 'Whisper' in content or 'whisper' in content:
        return 'whisper'
    if 'Panopto' in content and 'כתוביות' in content:
        return 'panopto-srt'
    return None


def export_summary(md_path: Path, course_key: str, all_stems: dict | None = None,
                   extra_fm: dict | None = None) -> Path:
    """
    Exports one summary to Obsidian.
    Adds YAML frontmatter, wikilinks and a "ראה גם" (see also) section.

    extra_fm: extra frontmatter fields to merge in (e.g. weakness_score, sources_used, tokens).
"""
    content = md_path.read_text(encoding='utf-8', errors='replace')
    from utils.other_group import keep, has as _og
    body    = keep(content, _strip_metadata_block(content))      # 14/11: "from another group" stays on top

    # Extract the date + title from the stem
    stem = md_path.stem
    date_str = stem[:10] if stem[:10].count('-') == 2 else ''
    title    = stem[13:].strip() if date_str else stem

    # Detect the type
    type_folder = md_path.parent.name   # "lectures" | "tutorials"
    kind = 'הרצאה' if type_folder == 'lectures' else 'תרגול'

    course_name = COURSE_NAMES.get(course_key, course_key)
    topics = _extract_topics(body)

    # Wikilinks
    related_section = ''
    if all_stems:
        related_section = _build_related_section(stem, all_stems, course_name)
        # topic injection — only in paragraphs, not in headings
        # body = _inject_topic_links(body, all_stems, stem, course_name)  # optional — currently disabled, can cause noise

    fm_data = {
        'title':       title,
        'course':      course_name,
        'course_key':  course_key,
        'date':        date_str,
        'type':        kind,
        'topics':      topics,
        'topics_from_first_page': topics,  # alias — uses same source (first page H2/H3)
        'tags':        [course_key, kind, 'סיכום', 'smart-study-agent'] + (['קבוצה-אחרת'] if _og(content) else []),
        'created':     datetime.now().strftime('%Y-%m-%d'),
    }
    if _og(content):
        fm_data['other_group'] = True
    # Auto-extract concepts via Haiku (cached — re-runs only on new content)
    try:
        from extractors.concept_extractor import extract_concepts as _extract_c
        out_path_for_cache = STUDY_DIR / _course_dir(course_key).name / f'{stem}.md'
        concepts = _extract_c(out_path_for_cache, topics, body[:800], course_key, use_api=True)
        if concepts:
            fm_data['concepts'] = concepts
    except Exception as _e:
        # Never let concept extraction break the export pipeline
        print(f'  ⚠️ concept extraction skipped: {_e}', file=sys.stderr)

    # Auto-populated rich fields
    cost = _extract_cost(content)
    if cost is not None:
        fm_data['cost_usd'] = cost
    panopto = _extract_panopto_url(content)
    if panopto:
        fm_data['panopto_url'] = panopto
    pdf_path = _detect_pdf_path(md_path)
    if pdf_path:
        fm_data['pdf_path'] = pdf_path
    method = _detect_transcription_method(content)
    if method:
        fm_data['transcription_method'] = method
    # Caller-supplied overrides (concepts, weakness_score, sources_used, tokens, ...)
    if extra_fm:
        fm_data.update(extra_fm)

    fm = _frontmatter(fm_data)

    out_path = _course_dir(course_key) / f'{stem}.md'
    out_path.write_text(fm + body + related_section, encoding='utf-8')

    # Also copy the matching PDF (if exists) so dashboard + Obsidian users see it.
    src_pdf = md_path.with_suffix('.pdf')
    if src_pdf.exists():
        import shutil as _sh
        dst_pdf = out_path.with_suffix('.pdf')
        if not dst_pdf.exists() or dst_pdf.stat().st_size != src_pdf.stat().st_size:
            try:
                _sh.copy2(src_pdf, dst_pdf)
            except Exception as _e:
                print(f'⚠️ failed to copy PDF for {stem}: {_e}')

    return out_path


def export_all_summaries(force: bool = False) -> int:
    """Exports all summaries. Skips existing ones (unless force=True)."""
    count = 0
    for course_key in COURSE_NAMES:
        course_sum_dir = SUMMARIES_DIR / course_key
        if not course_sum_dir.exists():
            continue

        # Build the stems map for this course (for wikilinks)
        all_stems = _build_all_stems(course_key)

        for md_file in _live_summaries(course_sum_dir):
            out = _course_dir(course_key) / f'{md_file.stem}.md'
            if not force and out.exists() and out.stat().st_mtime >= md_file.stat().st_mtime:
                continue
            try:
                export_summary(md_file, course_key, all_stems=all_stems)
                count += 1
                print(f'  ✅ {md_file.stem}')
            except Exception as e:
                print(f'  ⚠️ {md_file.name}: {e}')
    return count


# ─────────────────────────────────────────────────────────────────────────────
# Export: Quizzes → Obsidian note
# ─────────────────────────────────────────────────────────────────────────────

def export_quiz(json_path: Path, course_key: str) -> Path:
    """Converts a JSON quiz into an Obsidian page with toggle-style questions."""
    data    = json.loads(json_path.read_text(encoding='utf-8'))
    title   = data.get('title', 'חידון')
    qs      = data.get('questions', [])
    stem    = json_path.stem.replace('_quiz', '')   # "2026-04-26 - ..."
    date_str = stem[:10] if stem[:10].count('-') == 2 else ''

    fm = _frontmatter({
        'title':      title,
        'course':     COURSE_NAMES.get(course_key, course_key),
        'course_key': course_key,
        'date':       date_str,
        'type':       'חידון',
        'tags':       [course_key, 'חידון', 'smart-study-agent'],
    })

    lines = [fm, f'# {title}\n']
    for i, q in enumerate(qs, 1):
        options = q.get('options', {})
        correct = q.get('answer', '')
        expl    = q.get('explanation', '')

        lines.append(f'## שאלה {i}')
        lines.append(f'\n{q.get("q", "")}\n')
        for letter, text in options.items():
            marker = '✅' if letter == correct else '◻️'
            lines.append(f'{marker} **{letter}.** {text}')
        if expl:
            lines.append(f'\n> [!tip] הסבר\n> {expl}\n')
        lines.append('')

    # Spaced-repetition cards (obsidian-spaced-repetition plugin format)
    # Format: "question :: answer" with blank lines between cards.
    sr_lines = ['', '---', '', '#flashcards', '', '## 🃏 חזרה מרווחת', '']
    for q in qs:
        question_text = q.get('q', '').replace('\n', ' ').strip()
        correct = q.get('answer', '')
        options = q.get('options', {})
        ans_text = options.get(correct, correct) if isinstance(options, dict) else correct
        expl = q.get('explanation', '').replace('\n', ' ').strip()
        if not question_text or not ans_text:
            continue
        full_answer = ans_text
        if expl:
            full_answer = f'{ans_text} — {expl}'
        sr_lines.append(f'{question_text}::{full_answer}')
        sr_lines.append('')
    lines.extend(sr_lines)

    quiz_dir = STUDY_DIR / 'חידונים'
    quiz_dir.mkdir(parents=True, exist_ok=True)
    out = quiz_dir / f'{stem}_חידון.md'
    out.write_text('\n'.join(lines), encoding='utf-8')
    return out


def export_all_quizzes(force: bool = False) -> int:
    count = 0
    if not QUIZZES_DIR.exists():
        return 0
    for json_file in sorted(QUIZZES_DIR.rglob('*_quiz.json')):
        course_key = json_file.parent.name
        stem       = json_file.stem.replace('_quiz', '')
        quiz_dir   = STUDY_DIR / 'חידונים'
        quiz_dir.mkdir(parents=True, exist_ok=True)
        out = quiz_dir / f'{stem}_חידון.md'
        if not force and out.exists():
            continue
        try:
            export_quiz(json_file, course_key)
            count += 1
            print(f'  ✅ חידון: {json_file.stem}')
        except Exception as e:
            print(f'  ⚠️ {json_file.name}: {e}')
    return count


# ─────────────────────────────────────────────────────────────────────────────
# Export: Assignments calendar
# ─────────────────────────────────────────────────────────────────────────────

def export_assignments() -> Path:
    """Exports assignments + deadlines to an Obsidian page with a calendar view."""
    from collectors.assignment_tracker import _load_cache
    cache = _load_cache()
    assignments = cache.get('assignments', [])

    today     = datetime.now()
    out_lines = []

    fm = _frontmatter({
        'title':   'לוח מטלות',
        'tags':    ['מטלות', 'דדליינים', 'smart-study-agent'],
        'updated': today.strftime('%Y-%m-%d %H:%M'),
    })
    out_lines.append(fm)
    out_lines.append('# 📅 לוח מטלות\n')
    out_lines.append(f'> עודכן: {today.strftime("%d/%m/%Y %H:%M")}\n')

    # Group by category
    overdue, urgent, upcoming, no_date = [], [], [], []
    submitted = []

    for a in assignments:
        # Submitted assignments — a separate category
        if a.get('submitted'):
            submitted.append(a)
            continue
        dl = a.get('days_left')
        if dl is None:
            no_date.append(a)
        elif dl < 0:
            overdue.append(a)
        elif dl <= 3:
            urgent.append(a)
        else:
            upcoming.append(a)

    def _row(a: dict) -> str:
        dl  = a.get('days_left')
        due = a.get('due_date', '')[:10] if a.get('due_date') else '—'
        grade = a.get('grade')
        graded = a.get('graded', False)
        feedback = a.get('feedback')

        if a.get('submitted') and grade:   icon = f'⭐'
        elif a.get('submitted') and graded: icon = '🔍'
        elif a.get('submitted'):           icon = '✅'
        elif dl is None:                   icon = '❓'
        elif dl < 0:                       icon = '🔴'
        elif dl <= 3:                      icon = '🟠'
        elif dl <= 7:                      icon = '🟡'
        else:                              icon = '🟢'

        local = f' · [[{Path(a["local_path"]).stem}]]' if a.get('local_path') and Path(a['local_path']).exists() else ''
        sub_local = f' · [[{Path(a["submission_local_path"]).stem}|📄 הגשה שלי]]' if a.get('submission_local_path') and Path(a['submission_local_path']).exists() else ''
        grade_tag = f' · `ציון: {grade}/100`' if grade else (' · `נבדק`' if graded else '')
        sub_tag = ' `הוגש`' if a.get('submitted') else ''
        notes_tag = f' · `⚠️ {a["notes"]}`' if a.get('notes') and not a.get('submitted') else ''

        row = f'- {icon} **{a["title"]}** · {a.get("course_name","")}{local}{sub_local} · הגשה: `{due}`{sub_tag}{grade_tag}{notes_tag}'
        if feedback:
            row += f'\n  > 💬 **הערת מרצה:** {feedback[:200]}'
        return row

    if overdue:
        out_lines.append('\n## 🔴 מאוחר\n')
        out_lines.extend(_row(a) for a in overdue)

    if urgent:
        out_lines.append('\n## 🟠 דחוף — עד 3 ימים\n')
        out_lines.extend(_row(a) for a in urgent)

    if upcoming:
        out_lines.append('\n## 🟡 קרוב\n')
        out_lines.extend(_row(a) for a in upcoming)

    if no_date:
        out_lines.append('\n## ❓ ללא תאריך\n')
        out_lines.extend(_row(a) for a in no_date)

    if submitted:
        out_lines.append('\n## ✅ הוגש\n')
        out_lines.extend(_row(a) for a in submitted)

    out_path = STUDY_DIR / '📅 מטלות.md'
    STUDY_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text('\n'.join(out_lines), encoding='utf-8')
    return out_path


# ─────────────────────────────────────────────────────────────────────────────
# Course index pages (Dataview)
# ─────────────────────────────────────────────────────────────────────────────

def write_course_index(course_key: str) -> Path:
    """Writes a course's _index.md page with Dataview queries."""
    course_name = COURSE_NAMES.get(course_key, course_key)
    d = _course_dir(course_key)
    safe_name = _safe_dirname(course_name)
    emoji = COURSE_EMOJI.get(course_key, '📚')

    content = f"""---
title: {course_name}
course_key: {course_key}
tags: [{course_key}, index]
---

# {emoji} {course_name}

## 📚 הרצאות

```dataview
TABLE date AS "תאריך", topics AS "נושאים"
FROM "Smart Study Agent/{safe_name}"
WHERE course_key = "{course_key}" AND type = "הרצאה"
SORT date DESC
```

## ✏️ תרגולים

```dataview
TABLE date AS "תאריך", topics AS "נושאים"
FROM "Smart Study Agent/{safe_name}"
WHERE course_key = "{course_key}" AND type = "תרגול"
SORT date DESC
```

## 🧠 חידונים

```dataview
TABLE date AS "תאריך"
FROM "Smart Study Agent/חידונים"
WHERE course_key = "{course_key}"
SORT date DESC
```

## 📅 מטלות פתוחות

```dataview
LIST title
FROM "Smart Study Agent"
WHERE file.name = "📅 מטלות" AND course_key = "{course_key}"
```
"""
    out = d / '_index.md'
    out.write_text(content, encoding='utf-8')
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Home page — a nice home page
# ─────────────────────────────────────────────────────────────────────────────

def _count_notes(course_key: str) -> tuple[int, int]:
    """Returns (number of lectures, number of tutorials) exported to the vault."""
    course_name = _safe_dirname(COURSE_NAMES.get(course_key, course_key))
    d = STUDY_DIR / course_name
    if not d.exists():
        return 0, 0
    all_md = [f for f in d.glob('*.md') if not f.name.startswith('_')]
    lectures  = sum(1 for f in all_md if _note_type(f) == 'הרצאה')
    tutorials = sum(1 for f in all_md if _note_type(f) == 'תרגול')
    return lectures, tutorials


def _note_type(md_path: Path) -> str:
    """Reads the page's type from its frontmatter."""
    try:
        txt = md_path.read_text(encoding='utf-8', errors='replace')
        m = re.search(r'^type:\s*(.+)$', txt, re.MULTILINE)
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    return ''


def _last_updated(course_key: str) -> str:
    """A course's last-updated date."""
    course_name = _safe_dirname(COURSE_NAMES.get(course_key, course_key))
    d = STUDY_DIR / course_name
    if not d.exists():
        return '—'
    mds = list(d.glob('*.md'))
    if not mds:
        return '—'
    latest = max(mds, key=lambda f: f.stat().st_mtime)
    return datetime.fromtimestamp(latest.stat().st_mtime).strftime('%d/%m/%Y')


def write_home_page() -> Path:
    """Writes a nice home page with course cards."""
    today_str = datetime.now().strftime('%d/%m/%Y')
    semester_start = datetime(2026, 4, 12)
    week_num = ((datetime.now() - semester_start).days // 7) + 1

    # Build the course cards
    course_cards = []
    for course_key, course_name in COURSE_NAMES.items():
        emoji  = COURSE_EMOJI.get(course_key, '📚')
        lec, tut = _count_notes(course_key)
        last_upd = _last_updated(course_key)
        safe_name = _safe_dirname(course_name)

        card = f"""> [!note]+ {emoji} {course_name}
>
> | | |
> |---|---|
> | 📖 הרצאות | {lec} |
> | ✏️ תרגולים | {tut} |
> | 🕐 עדכון אחרון | {last_upd} |
>
> [[Smart Study Agent/{safe_name}/_index|📂 פתח קורס →]]"""
        course_cards.append(card)

    cards_text = '\n\n'.join(course_cards)

    content = f"""---
title: בית
tags: [home, smart-study-agent]
cssclasses: [home-page]
---

# 🎓 Smart Study Agent

> **שבוע {week_num} בסמסטר** · {today_str}

---

## 📚 הקורסים שלי

{cards_text}

---

## ⚡ קפיצה מהירה

- [[📅 מטלות]] — מטלות ודדליינים
- [[Smart Study Agent/חידונים|🧠 חידונים]]

---

## 📊 פעילות אחרונה

```dataview
TABLE course AS "קורס", type AS "סוג", date AS "תאריך"
FROM "Smart Study Agent"
WHERE file.name != "_index" AND type != null AND type != "חידון"
SORT date DESC
LIMIT 8
```

---

## 📅 מטלות קרובות

```dataview
TABLE course_key AS "קורס", due_date AS "הגשה"
FROM "Smart Study Agent"
WHERE tags AND contains(tags, "מטלות")
SORT due_date ASC
LIMIT 5
```
"""

    STUDY_DIR.mkdir(parents=True, exist_ok=True)
    out = STUDY_DIR / '🏠 בית.md'
    out.write_text(content, encoding='utf-8')
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Main dashboard page
# ─────────────────────────────────────────────────────────────────────────────

def write_dashboard() -> Path:
    """Writes the 🏠 Dashboard.md page with the main Dataview."""
    content = f"""---
title: Smart Study Agent Dashboard
tags: [dashboard, smart-study-agent]
---

# 🎓 Smart Study Agent

## 📊 סיכום שבועי

```dataview
TABLE course AS "קורס", type AS "סוג", date AS "תאריך"
FROM "Smart Study Agent"
WHERE file.name != "_index" AND type != null
SORT date DESC
LIMIT 10
```

## ✏️ חידונים אחרונים

```dataview
TABLE course AS "קורס", date AS "תאריך"
FROM "Smart Study Agent/חידונים"
SORT date DESC
LIMIT 5
```

## 📅 מטלות דחופות

![[📅 מטלות]]
"""
    STUDY_DIR.mkdir(parents=True, exist_ok=True)
    out = STUDY_DIR / '🏠 Dashboard.md'
    out.write_text(content, encoding='utf-8')
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Full sync
# ─────────────────────────────────────────────────────────────────────────────

def sync_all(force: bool = False) -> dict:
    """Syncs everything to the Obsidian vault."""
    print(f'\n🔄 מסנכרן ל-Obsidian: {VAULT_ROOT}')
    STUDY_DIR.mkdir(parents=True, exist_ok=True)

    n_sum   = export_all_summaries(force=force)
    n_quiz  = export_all_quizzes(force=force)

    try:
        assign_path = export_assignments()
        print(f'  ✅ מטלות: {assign_path.name}')
    except Exception as e:
        print(f'  ⚠️ מטלות: {e}')

    for course_key in COURSE_NAMES:
        if (STUDY_DIR / _safe_dirname(COURSE_NAMES[course_key])).exists():
            write_course_index(course_key)

    home = write_home_page()
    print(f'  ✅ דף בית: {home.name}')

    dash = write_dashboard()
    print(f'  ✅ Dashboard: {dash.name}')

    # Build/refresh the concept graph (concept notes + related: links)
    n_concepts = 0
    n_related = 0
    try:
        from vault_sync.relinker import relink as _relink
        stats = _relink(use_api=True, write_concept_notes=True, force_concepts=False)
        n_concepts = stats.get('concept_notes_written', 0)
        n_related = stats.get('relinked', 0)
        print(f'  ✅ גרף קשרים: {stats["concepts_total"]} מושגים, {n_concepts} concept notes')
    except Exception as e:
        print(f'  ⚠️ relinker: {e}')

    print(f'\n✅ סנכרון הושלם — {n_sum} סיכומים, {n_quiz} חידונים, {n_concepts} concept notes')
    return {'summaries': n_sum, 'quizzes': n_quiz, 'concepts': n_concepts, 'relinked': n_related}


if __name__ == '__main__':
    import sys as _sys
    force = '--force' in _sys.argv
    _sys.path.insert(0, str(ROOT))
    sync_all(force=force)
