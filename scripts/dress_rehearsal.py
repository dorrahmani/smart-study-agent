"""
scripts/dress_rehearsal.py — end-to-end rehearsal on ONE real past lecture.

Exercises every external dependency the semester needs, with real services,
and reports ✅/❌ + timing per stage:

  1. Panopto SSO login                     5. Gemini summary (the real prompt)
  2. pick a past lecture (shortest)        6. PDF (Playwright, Hebrew RTL)
  3. Panopto captions + Hebrew check       7. email — to the OWNER ONLY
  4. AI transcription (gpt-4o-transcribe,  8. WhatsApp report
     on a short clip to cap the cost)

Isolation: everything is written under data/rehearsal/<timestamp>/ — never
data/panopto or data/summaries — so nothing is vaulted, indexed, or picked up
by the nightly run, and config/sent_log.json is never touched.

    venv/bin/python3 scripts/dress_rehearsal.py                  # default: microeconomics, 6-min clip
    venv/bin/python3 scripts/dress_rehearsal.py --course calculus_2 --clip-min 4 --no-email

Cost: ~$0.04 transcription + ~$0.30 summary.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except AttributeError:
    pass
from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / '.env', override=True)
if os.environ.get('GEMINI_API_KEY'):
    os.environ.pop('GOOGLE_API_KEY', None)

import yaml  # noqa: E402

RESULTS: list[tuple[str, bool, float, str]] = []


def stage(name):
    def deco(fn):
        def run(*a, **k):
            t0 = time.time()
            print(f'\n━━ {name}', flush=True)
            try:
                out = fn(*a, **k)
                RESULTS.append((name, True, time.time() - t0, ''))
                return out
            except Exception as e:
                traceback.print_exc()
                RESULTS.append((name, False, time.time() - t0, str(e)[:200]))
                raise
        return run
    return deco


def hebrew_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    return sum('֐' <= c <= '׿' for c in letters) / max(1, len(letters))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--course', default='microeconomics')
    ap.add_argument('--clip-min', type=float, default=6)
    ap.add_argument('--no-email', action='store_true')
    args = ap.parse_args()

    cfg = yaml.safe_load(open(ROOT / 'config' / 'courses.yaml', encoding='utf-8'))
    course = cfg['courses'][args.course]
    out = ROOT / 'data' / 'rehearsal' / datetime.now().strftime('%Y%m%d-%H%M%S')
    out.mkdir(parents=True, exist_ok=True)
    print(f'rehearsal dir: {out}')

    from collectors.panopto_collector import PanoptoCollector
    pc = PanoptoCollector()
    pc.output_dir = out  # isolation: never data/panopto

    @stage('1. Panopto login')
    def s1():
        pc.login()

    @stage('2. pick a past lecture')
    def s2():
        fid = course['sources']['panopto'].get('folder_id')
        sessions = pc.search_sessions('', days_back=400, folder_id=fid)
        sessions = [s for s in sessions if (s.get('Duration') or 0) > 15 * 60] or sessions
        assert sessions, f'no sessions in folder {fid}'
        s = min(sessions, key=lambda s: s.get('Duration') or 1e9)
        vid = s.get('DeliveryID') or s.get('IosVideoUrl', '').split('/')[-1]
        title = pc._clean_title(s.get('SessionName', 'lecture'))
        print(f'   {title} · {int((s.get("Duration") or 0) / 60)} min · {vid}')
        return vid, title

    @stage('3. Panopto captions + Hebrew check')
    def s3(vid, title):
        from utils.srt_quality import srt_is_good_hebrew
        srt = pc.download_srt(vid, title, args.course, 'rehearsal')
        if not srt:
            print('   (no Panopto captions for this lecture — AI transcript will be used)')
            return None
        good = srt_is_good_hebrew(srt)
        txt = Path(srt).read_text(encoding='utf-8', errors='replace')
        print(f'   {len(txt)} chars · hebrew ratio {hebrew_ratio(txt):.0%} · good={good}')
        return srt if good else None

    @stage('4. AI transcription (clip)')
    def s4(vid, title):
        import collectors.whisper_transcriber as wt
        orig = wt.transcribe_and_save

        def clipped(video_path, output_srt_path, language=None):
            clip = str(Path(video_path).with_name('rehearsal_clip.mp3'))
            dur = wt.get_duration(video_path)
            start = min(600, max(0, dur / 3))
            wt.extract_audio_chunk(video_path, clip, start, args.clip_min * 60)
            print(f'   clip: {args.clip_min:g} min from {start/60:.0f}:00 · model={wt.TRANSCRIBE_MODEL}')
            return orig(clip, output_srt_path, language, reuse=False)   # a rehearsal must really transcribe

        wt.transcribe_and_save = clipped
        try:
            srt = pc.download_and_transcribe(vid, title + ' AI', args.course, 'rehearsal')
        finally:
            wt.transcribe_and_save = orig
        assert srt and Path(srt).exists(), 'no transcript produced'
        txt = Path(srt).read_text(encoding='utf-8', errors='replace')
        ratio = hebrew_ratio(txt)
        print(f'   {len(txt)} chars · hebrew ratio {ratio:.0%}')
        print('   sample: ' + ' '.join(txt.split())[:160])
        assert ratio > 0.6, f'transcript is not Hebrew ({ratio:.0%})'
        return srt

    @stage('5. Gemini summary')
    def s5(srt, title):
        from generators.summarizer import AISummarizer
        sm = AISummarizer()
        summary, sources = sm.generate_summary(
            course_name=course['name'], lecture_title=f'[חזרה] {title}',
            caption_path=str(srt), pdf_path=None, pdf_source_type='moodle', video_path=None,
            course_key=args.course)
        assert summary and len(summary) > 2000 and not summary.startswith('# שגיאה'), \
            f'summary too short/failed ({len(summary or "")} chars)'
        md = out / 'summary.md'
        sm.save_summary(summary=summary, output_path=str(md), course_name=course['name'],
                        lecture_title=f'[חזרה] {title}', sources=sources)
        assert 'mermaid' not in summary.lower(), 'Mermaid diagram in summary (banned)'
        print(f'   {len(summary)} chars → {md.name}')
        return md

    @stage('6. PDF')
    def s6(md):
        pdf = md.with_suffix('.pdf')
        if not pdf.exists():
            from generators.pdf_generator import markdown_to_pdf
            markdown_to_pdf(md.read_text(encoding='utf-8'), str(pdf), title='חזרה', course_name=course['name'])
        from pypdf import PdfReader
        r = PdfReader(str(pdf))
        txt = ''.join((p.extract_text() or '') for p in r.pages[:2])
        assert len(r.pages) >= 2 and hebrew_ratio(txt) > 0.3, 'PDF empty or not Hebrew'
        print(f'   {len(r.pages)} pages · {pdf.stat().st_size // 1024} KB')
        return pdf

    @stage('7. email (owner only)')
    def s7(pdf, title):
        from config.paths import owner_email
        from generators.gmail_sender import GmailSender
        to = owner_email()
        assert to, 'no owner email configured'
        ok = GmailSender().send_email(
            to=to, subject=f'[חזרה גנרלית] {course["name"]} — {title}',
            body_html=("<div dir='rtl' style='font-family:Arial'><h2>🧪 חזרה גנרלית לפני הסמסטר</h2>"
                       "<p>זה מייל בדיקה. הסיכום המצורף נוצר עכשיו מקצה לקצה: פנופטו → תמלול → Gemini → PDF.</p></div>"),
            attachment_path=str(pdf))
        assert ok, 'Gmail send returned False'
        print(f'   sent to {to.split("@")[0][:3]}***@{to.split("@")[-1]}')

    vid = srt = None
    try:
        s1()
        vid, title = s2()
        srt = s3(vid, title)
        ai_srt = s4(vid, title)
        md = s5(srt or ai_srt, title)
        pdf = s6(md)
        if not args.no_email:
            s7(pdf, title)
    except Exception:
        pass  # recorded in RESULTS; report below

    ok = all(r[1] for r in RESULTS) and len(RESULTS) >= (6 if args.no_email else 7)
    lines = ['🧪 *חזרה גנרלית* — ' + ('✅ הכול עבר' if ok else '❌ יש כשל'), '']
    for name, passed, secs, err in RESULTS:
        lines.append(f'{"✅" if passed else "❌"} {name} ({secs:.0f}s)' + (f' — {err}' if err else ''))
    report = '\n'.join(lines)
    print('\n' + report)
    try:
        from utils.notify import deliver                # WhatsApp only (10/11: Telegram deleted)
        ch = deliver(report, markdown=True)
        RESULTS.append((f'8. report ({ch})', bool(ch), 0, ''))
    except Exception as e:
        print(f'report failed: {e}')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
