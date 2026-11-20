"""
Whisper Transcriber - transcribes audio/video files with the OpenAI Whisper API
"""

import os
import sys
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except AttributeError:
    pass

import subprocess
import math
import re
from pathlib import Path
from typing import Optional

# Transcription model. gpt-4o-transcribe is ~2x more accurate than whisper-1
# (Real-World Audio WER 15.2% -> 9.0%) and takes richer priming, BUT it does
# NOT return segment timestamps (timestamp_granularities is whisper-1 only).
# So for gpt-4o-* we interpolate approximate per-sentence timestamps from each
# chunk's known time span; whisper-1 keeps exact segment timestamps.
# Override with env SSA_TRANSCRIBE_MODEL (e.g. 'whisper-1' to revert).
TRANSCRIBE_MODEL = os.getenv('SSA_TRANSCRIBE_MODEL', 'gpt-4o-transcribe')

_WIN_FFMPEG = 'ffmpeg'   # set FFMPEG_PATH in .env if ffmpeg is not on PATH
FFMPEG_PATH  = os.getenv('FFMPEG_PATH',  _WIN_FFMPEG if os.name == 'nt' else 'ffmpeg')
FFPROBE_PATH = os.getenv('FFPROBE_PATH', _WIN_FFMPEG.replace('ffmpeg.exe', 'ffprobe.exe') if os.name == 'nt' else 'ffprobe')

MAX_WHISPER_BYTES = 24 * 1024 * 1024  # 24MB (under 25MB limit)
MAX_CHUNK_MINUTES = 20                 # max 20 min per chunk — faster upload & safer
AUDIO_BITRATE = '48k'                  # 48kbps mono — good for speech, small file


_CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0


def _run(cmd: list) -> subprocess.CompletedProcess:
    """Run a subprocess, capturing output as bytes to avoid encoding issues."""
    return subprocess.run(cmd, capture_output=True, creationflags=_CREATE_NO_WINDOW)


def get_duration(video_path: str) -> float:
    """Returns duration of audio/video in seconds."""
    result = _run([FFPROBE_PATH, '-v', 'error', '-show_entries', 'format=duration',
                   '-of', 'default=noprint_wrappers=1:nokey=1', video_path])
    try:
        return float(result.stdout.decode('utf-8', errors='replace').strip())
    except Exception:
        return 0.0


def extract_audio_chunk(video_path: str, output_path: str,
                         start_sec: float, duration_sec: float) -> str:
    """Extract a chunk of audio as MP3 from a video file."""
    cmd = [
        FFMPEG_PATH, '-y',
        '-ss', str(start_sec),
        '-i', video_path,
        '-t', str(duration_sec),
        '-vn',                        # no video
        '-ac', '1',                   # mono
        '-ar', '16000',               # 16kHz — optimal for Whisper
        '-b:a', AUDIO_BITRATE,
        '-f', 'mp3',
        output_path
    ]
    result = _run(cmd)
    if result.returncode != 0:
        err = result.stderr.decode('utf-8', errors='replace')[-500:]
        raise RuntimeError(f'ffmpeg error: {err}')
    return output_path


def estimate_chunk_duration(total_duration: float, max_bytes: int = MAX_WHISPER_BYTES) -> float:
    """Estimate chunk duration: min of size limit and MAX_CHUNK_MINUTES."""
    bitrate_bps = int(AUDIO_BITRATE.rstrip('k')) * 1024
    size_based = (max_bytes * 8) / bitrate_bps * 0.9
    time_based = MAX_CHUNK_MINUTES * 60
    return min(size_based, time_based, total_duration)


# Academic-vocabulary priming hint — boosts accuracy on technical Hebrew terms.
# Both whisper-1 and gpt-4o-transcribe accept a `prompt`.
_HEBREW_HINT = (
    "הרצאה אקדמית באוניברסיטה. נושאים: מתמטיקה (חדו\"א, לינארית, בדידה), "
    "מדעי המחשב (פייתון, רקורסיה, סיבוכיות), כלכלה. "
    "מונחים: משפט, הוכחה, הגדרה, מטריצה, פונקציה, גרף, "
    "אינטגרל, נגזרת, גבול, ערך עצמי, וקטור עצמי, פולינום, "
    "רקורסיה, לולאה, אלגוריתם, סיבוכיות, מחלקה, מערך, "
    "עקומת אדישות, פונקציית תועלת, קו תקציב."
)

_SENT_SPLIT = re.compile(r'(?<=[.!?…])\s+|\n+')


def _interp_segments(text, chunk_start, chunk_dur):
    """gpt-4o-transcribe returns no timestamps — approximate per-sentence times
    by spreading sentences across the chunk's [start, start+dur] span by char
    position (assumes ~uniform speech rate). Coarse but fine for the lecture map;
    whisper-1 keeps exact segment times."""
    text = (text or '').strip()
    if not text:
        return []
    parts = [p.strip() for p in _SENT_SPLIT.split(text) if p.strip()]
    if not parts:
        return []
    total = sum(len(p) for p in parts) or 1
    out, cum = [], 0
    for p in parts:
        out.append((chunk_start + (cum / total) * chunk_dur, p))
        cum += len(p)
    return out


def _whisper1_chunk(client, chunk_path, language, time_offset):
    """Exact-timestamp path via whisper-1 (verbose_json segments)."""
    with open(chunk_path, 'rb') as f:
        resp = client.audio.transcriptions.create(
            model='whisper-1', file=f, language=language,
            prompt=_HEBREW_HINT if language == 'he' else None,
            temperature=0.0,
            response_format='verbose_json',
            timestamp_granularities=['segment'],
        )
    return [(seg.start + time_offset, seg.text) for seg in resp.segments]


def _transcribe_chunk(client, chunk_path, language, time_offset, chunk_dur):
    """Transcribe one chunk. Returns (list[(abs_start_sec, text)], model_used).
    Uses TRANSCRIBE_MODEL (gpt-4o-transcribe by default) and falls back to
    whisper-1 on any error/empty result, so an API change can't break the run."""
    model = TRANSCRIBE_MODEL
    if model != 'whisper-1':
        try:
            with open(chunk_path, 'rb') as f:
                resp = client.audio.transcriptions.create(
                    model=model, file=f, language=language,
                    prompt=_HEBREW_HINT if language == 'he' else None,
                    response_format='json',  # gpt-4o-* has no verbose_json/timestamps
                )
            segs = _interp_segments(getattr(resp, 'text', '') or '',
                                    time_offset, chunk_dur)
            if segs:
                return segs, model
            print(f'    ⚠️ {model} returned empty → whisper-1', flush=True)
        except Exception as e:
            print(f'    ⚠️ {model} failed ({str(e)[:80]}) → whisper-1', flush=True)
    return _whisper1_chunk(client, chunk_path, language, time_offset), 'whisper-1'


def transcribe_video(video_path: str, language: str = None) -> str:
    """
    Transcribe a video/audio file using the OpenAI transcription API.
    Handles files larger than 25MB by splitting into chunks.
    Returns the full transcript as a string with timestamps.
    """
    from openai import OpenAI

    client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
    video_path = str(video_path)

    print(f'🎙️  מתמלל: {Path(video_path).name}')
    duration = get_duration(video_path)
    print(f'⏱️  משך: {duration/60:.1f} דקות')

    chunk_duration = estimate_chunk_duration(duration)
    n_chunks = math.ceil(duration / chunk_duration)
    print(f'📦  מחולק ל-{n_chunks} חלקים')

    tmp_dir = Path('data/tmp_audio')
    tmp_dir.mkdir(parents=True, exist_ok=True)

    full_transcript_parts = []
    time_offset = 0.0

    for i in range(n_chunks):
        start = i * chunk_duration
        actual_dur = min(chunk_duration, duration - start)
        chunk_path = str(tmp_dir / f'chunk_{i}.mp3')

        print(f'  חלק {i+1}/{n_chunks}: {start/60:.1f}-{(start+actual_dur)/60:.1f} דקות')
        extract_audio_chunk(video_path, chunk_path, start, actual_dur)

        file_size = Path(chunk_path).stat().st_size
        print(f'    גודל: {file_size//1024//1024}MB')

        # Transcribe this chunk. Prefer gpt-4o-transcribe (accuracy); fall back
        # to whisper-1 on any error so a model/API change never breaks the run.
        segments, used = _transcribe_chunk(
            client, chunk_path, language, time_offset, actual_dur)
        try:
            from utils.usage_tracker import log_whisper
            log_whisper(audio_seconds=float(actual_dur or 0),
                        op=f'transcribe.{used}', model=used)
        except Exception: pass
        print(f'    ({used})', flush=True)

        # Build SRT-like text with absolute timestamps
        for seg_start, text in segments:
            text = (text or '').strip()
            if text:
                full_transcript_parts.append(f'[{_format_timestamp(seg_start)}] {text}')

        time_offset += actual_dur

        # Clean up chunk
        try:
            Path(chunk_path).unlink()
        except Exception:
            pass

    transcript = '\n'.join(full_transcript_parts)
    print(f'✅ תמלול הושלם! {len(transcript)} תווים')
    return transcript


# 13/11: a recording whose Panopto captions are garbage (probability's tutorials: 0% Hebrew) was paid for again EVERY
# night it stayed the newest one: the collector downloads Panopto's SRT over our transcript, quarantines it as .bad,
# and the transcript is gone — "תרגול 4" of 10/11 was transcribed on 11/11, 12/11 and 13/11 ($0.29 each). A copy of
# every paid transcript is kept aside under its file name, and the same recording is never paid for twice.
PAID_DIR_NAME = '.paid_transcripts'
MIN_REUSE_CHARS = 1000


def _paid_copy(output_srt_path) -> Path:
    out = Path(output_srt_path)
    return out.parent / PAID_DIR_NAME / out.name


def _reusable(path: Path) -> str | None:
    """Our own transcript ('[HH:MM:SS] text' lines, never Panopto's '-->' SRT), long enough to be a real one."""
    try:
        text = path.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return None
    if len(text) < MIN_REUSE_CHARS or '-->' in text[:2000] or not re.match(r'\[\d{2}:\d{2}:\d{2}\] ', text):
        return None
    return text


def transcribe_and_save(video_path: str, output_srt_path: str,
                        language: str = None, reuse: bool = True) -> str:
    """Transcribe video and save result as SRT-like text file. reuse: a transcript already paid for under this file
    name (kept aside, or still in place) is written back instead of paying again."""
    out = Path(output_srt_path)
    paid = _paid_copy(out)
    if reuse:
        for cand in (paid, out):
            text = _reusable(cand)
            if text:
                out.parent.mkdir(parents=True, exist_ok=True)
                if cand != out:
                    out.write_text(text, encoding='utf-8')
                print(f'♻️  תמלול כבר שולם — לא מתמללים שוב: {out.name}')
                return output_srt_path
    # 19/11: the VM's own CPU first ($0, faster-whisper + ivrit-ai, nice 19); the paid API only when that fails,
    # times out or its transcript fails the checks — and the paid fallback still obeys the budget lock.
    transcript = None
    try:
        from collectors import local_asr
        transcript = local_asr.transcribe(video_path)
    except Exception as e:
        print(f'  ⚠️ local ASR: {e!r}'[:200])
    if not transcript:
        from utils.budget import budget_lock_reason
        lock = budget_lock_reason()
        if lock:
            print(f'⛔ תמלול בתשלום נחסם — נעילת תקציב ({lock})')
            return None
        if not os.getenv('OPENAI_API_KEY'):
            print('⚠️  אין OPENAI_API_KEY — אין תמלול בתשלום')
            return None
        transcript = transcribe_video(video_path, language=language)

    out.parent.mkdir(parents=True, exist_ok=True)
    with open(output_srt_path, 'w', encoding='utf-8') as f:
        f.write(transcript)
    try:
        paid.parent.mkdir(parents=True, exist_ok=True)
        paid.write_text(transcript, encoding='utf-8')
    except OSError as e:
        print(f'  ⚠️ paid-transcript copy: {e}')

    print(f'💾 נשמר: {output_srt_path}')
    return output_srt_path


def _format_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f'{h:02d}:{m:02d}:{s:02d}'


if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv(override=True)

    import sys
    if len(sys.argv) < 2:
        print('Usage: python whisper_transcriber.py <video_path> [output_srt]')
        sys.exit(1)

    video = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else video.rsplit('.', 1)[0] + '.srt'
    transcribe_and_save(video, out)
