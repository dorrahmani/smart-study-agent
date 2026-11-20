"""
Board Reader - reads the board / screen content of a lecture video.
Extracts frames, filters out unchanged frames, and reads the content with Gemini Vision.
"""

import os
import sys
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except AttributeError:
    pass

import subprocess
import base64
import math
from pathlib import Path
from typing import List, Tuple, Optional

# Windows: prevent CMD window flash when calling ffmpeg/ffprobe from a detached parent
_CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0

_WIN_FFMPEG  = 'ffmpeg'   # set FFMPEG_PATH in .env if ffmpeg is not on PATH
FFMPEG_PATH  = os.getenv('FFMPEG_PATH',  _WIN_FFMPEG if os.name == 'nt' else 'ffmpeg')
FFPROBE_PATH = os.getenv('FFPROBE_PATH', _WIN_FFMPEG.replace('ffmpeg.exe', 'ffprobe.exe') if os.name == 'nt' else 'ffprobe')

FRAME_INTERVAL_SEC = 30     # sample a frame every 30 seconds
CHANGE_THRESHOLD = 0.04     # min fraction of pixels that must change (4%)
BATCH_SIZE = 20             # frames per Gemini Vision call (Pro handles large batches)
FRAME_WIDTH = 960           # resize for smaller file size

# Pro with thinking_level=low (see _thinking_config): measured 10/2026 — same cost
# as Flash, but the only model that transcribed every frame with correct LaTeX.
BOARD_MODEL_MATH = 'gemini-3.1-pro-preview'
BOARD_MODEL_DEFAULT = 'gemini-3.1-pro-preview'


def _run(cmd: list, timeout: int = 120) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout,
                              creationflags=_CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, returncode=1, stdout=b'', stderr=b'timeout')


def _parse_hls_variant_url(hls_master_url: str) -> Optional[str]:
    """Returns the URL of the smallest variant (960x540) from the HLS master playlist."""
    try:
        import re as _re
        import requests as _req
        r = _req.get(hls_master_url, timeout=15)
        lines = r.text.splitlines()
        base = hls_master_url.rsplit('/', 1)[0] + '/'
        # Return the last listed variant (lowest bandwidth in Panopto's manifest order)
        variant = None
        for line in lines:
            if line and not line.startswith('#'):
                variant = line.strip()
        if variant:
            if not variant.startswith('http'):
                variant = base + variant
            return variant
    except Exception:
        pass
    return None


def _parse_hls_segments(variant_url: str):
    """
    Parses index.panobf2 and returns:
      - init_url, init_range (offset, length)
      - segments: list of (offset, length) — each segment is 9 seconds
"""
    try:
        import re as _re
        import requests as _req
        r = _req.get(variant_url, timeout=15)
        base = variant_url.rsplit('/', 1)[0] + '/'
        lines = r.text.splitlines()

        init_url = None
        init_offset, init_len = 0, 0
        segments = []  # (offset, length, duration_sec)
        cur_range = None
        cur_dur = 0.0

        def parse_byterange(s):
            # format: length@offset or just length (offset=prev_end)
            if '@' in s:
                length, offset = s.split('@')
                return int(offset), int(length)
            return None, int(s)

        prev_end = 0
        for line in lines:
            line = line.strip()
            if line.startswith('#EXT-X-MAP:'):
                m = _re.search(r'URI="([^"]+)"', line)
                br = _re.search(r'BYTERANGE="([^"]+)"', line)
                if m and br:
                    uri = m.group(1)
                    if not uri.startswith('http'):
                        uri = base + uri
                    off, ln = parse_byterange(br.group(1))
                    if off is None:
                        off = 0
                    init_url, init_offset, init_len = uri, off, ln
                    prev_end = off + ln
            elif line.startswith('#EXT-X-BYTERANGE:'):
                br = line.split(':', 1)[1]
                off, ln = parse_byterange(br)
                if off is None:
                    off = prev_end
                cur_range = (off, ln)
                prev_end = off + ln
            elif line.startswith('#EXTINF:'):
                try:
                    cur_dur = float(line.split(':', 1)[1].rstrip(','))
                except Exception:
                    cur_dur = 9.0
            elif line and not line.startswith('#'):
                if cur_range:
                    segments.append((cur_range[0], cur_range[1], cur_dur))
                    cur_range = None

        return init_url, init_offset, init_len, segments
    except Exception as e:
        print(f"  ⚠️ שגיאה בניתוח HLS: {e}")
        return None, 0, 0, []


def _download_range(url: str, offset: int, length: int) -> bytes:
    import requests as _req
    end = offset + length - 1
    r = _req.get(url, headers={'Range': f'bytes={offset}-{end}'}, timeout=30)
    return r.content


def extract_frame_from_hls(hls_master_url: str, timestamp_sec: float,
                            output_path: str) -> bool:
    """
    Extracts a frame from Panopto HLS by downloading a specific byte range.
    Works around ffmpeg's .panobf1 extension problem.
"""
    variant_url = _parse_hls_variant_url(hls_master_url)
    if not variant_url:
        return False

    init_url, init_offset, init_len, segments = _parse_hls_segments(variant_url)
    if not segments:
        return False

    # Find the relevant segment by timestamp
    elapsed = 0.0
    seg_idx = 0
    for i, (off, ln, dur) in enumerate(segments):
        if elapsed + dur > timestamp_sec:
            seg_idx = i
            break
        elapsed += dur
        seg_idx = i

    seg_off, seg_len, _ = segments[seg_idx]
    frag_url = variant_url.rsplit('/', 1)[0] + '/fragmented.panobf1'

    try:
        # Download init + segment
        init_bytes = _download_range(frag_url, init_offset, init_len) if init_url else b''
        seg_bytes = _download_range(frag_url, seg_off, seg_len)
        fmp4_data = init_bytes + seg_bytes

        # Stream to ffmpeg via stdin
        seek_in_seg = timestamp_sec - elapsed
        cmd = [
            FFMPEG_PATH, '-y',
            '-f', 'mp4', '-i', 'pipe:0',
            '-ss', str(max(0, seek_in_seg)),
            '-vframes', '1',
            '-vf', f'scale={FRAME_WIDTH}:-1',
            '-q:v', '5',
            output_path
        ]
        result = subprocess.run(cmd, input=fmp4_data, capture_output=True, timeout=30,
                                creationflags=_CREATE_NO_WINDOW)
        if result.returncode == 0 and Path(output_path).exists():
            return True
        # Fallback: no seek, take first frame of segment
        cmd2 = [
            FFMPEG_PATH, '-y',
            '-f', 'mp4', '-i', 'pipe:0',
            '-vframes', '1',
            '-vf', f'scale={FRAME_WIDTH}:-1',
            '-q:v', '5',
            output_path
        ]
        result2 = subprocess.run(cmd2, input=fmp4_data, capture_output=True, timeout=30,
                                 creationflags=_CREATE_NO_WINDOW)
        return result2.returncode == 0 and Path(output_path).exists()
    except Exception as e:
        return False


def get_hls_duration(hls_master_url: str) -> float:
    """Computes the HLS video duration by summing all the segments."""
    variant_url = _parse_hls_variant_url(hls_master_url)
    if not variant_url:
        return 0.0
    _, _, _, segments = _parse_hls_segments(variant_url)
    return sum(d for _, _, d in segments)


def get_video_duration(video_path: str) -> float:
    is_hls = 'hls' in video_path.lower() or video_path.endswith('.panobf2')
    if is_hls:
        dur = get_hls_duration(video_path)
        if dur > 0:
            return dur
        print(f"  ⚠️ לא נמצא משך — מניח 75 דקות")
        return 75 * 60.0

    result = _run([FFPROBE_PATH, '-v', 'error', '-show_entries', 'format=duration',
                   '-of', 'default=noprint_wrappers=1:nokey=1', video_path])
    try:
        return float(result.stdout.decode('utf-8', errors='replace').strip())
    except Exception:
        return 0.0


def extract_frame(video_path: str, timestamp_sec: float, output_path: str) -> bool:
    """Extract a single frame at timestamp_sec as JPEG. Supports MP4 and HLS URLs."""
    is_hls = 'hls' in video_path.lower() or video_path.endswith('.panobf2')
    cmd = [FFMPEG_PATH, '-y']
    if is_hls:
        cmd += ['-allowed_extensions', 'ALL']
    cmd += [
        '-ss', str(timestamp_sec),
        '-i', video_path,
        '-vframes', '1',
        '-vf', f'scale={FRAME_WIDTH}:-1',
        '-q:v', '5',
        output_path
    ]
    # HLS needs more time per frame (downloads segments on demand)
    timeout = 180 if is_hls else 60
    result = _run(cmd, timeout=timeout)
    return result.returncode == 0 and Path(output_path).exists()


def pixel_change_ratio(img1_path: str, img2_path: str) -> float:
    """Compute fraction of pixels that changed between two images."""
    try:
        from PIL import Image, ImageChops
        import numpy as np
        img1 = Image.open(img1_path).convert('L').resize((192, 108))
        img2 = Image.open(img2_path).convert('L').resize((192, 108))
        diff = np.abs(np.array(img1, dtype=int) - np.array(img2, dtype=int))
        return float((diff > 20).mean())  # fraction of pixels changed by >20 intensity units
    except Exception:
        return 1.0  # if can't compare, assume changed


def extract_key_frames(video_path: str, tmp_dir: str) -> List[Tuple[float, str]]:
    """
    Extract frames every FRAME_INTERVAL_SEC, filter unchanged ones.
    Returns list of (timestamp_sec, frame_path) for significant-change frames.
    """
    tmp_path = Path(tmp_dir)
    # Clear leftover frames from any previous failed run to avoid stale-file confusion
    if tmp_path.exists():
        for _stale in tmp_path.glob("frame_*.jpg"):
            _stale.unlink(missing_ok=True)
    tmp_path.mkdir(parents=True, exist_ok=True)
    is_hls = 'hls' in video_path.lower() or video_path.endswith('.panobf2')
    duration = get_video_duration(video_path)
    if duration <= 0:
        return []

    timestamps = list(range(0, int(duration), FRAME_INTERVAL_SEC))
    print(f"  Extracting {len(timestamps)} frames (every {FRAME_INTERVAL_SEC}s)...")

    frames = []
    prev_path = None

    for i, ts in enumerate(timestamps):
        frame_path = str(Path(tmp_dir) / f"frame_{i:04d}.jpg")

        # Choose extraction method based on source type
        if is_hls:
            ok = extract_frame_from_hls(video_path, ts, frame_path)
            if not ok:
                # fallback: direct ffmpeg HLS streaming
                ok = extract_frame(video_path, ts, frame_path)
        else:
            ok = extract_frame(video_path, ts, frame_path)

        if not ok:
            continue

        # Keep frame if it's the first or if board changed significantly
        if prev_path is None or pixel_change_ratio(prev_path, frame_path) >= CHANGE_THRESHOLD:
            frames.append((float(ts), frame_path))
            prev_path = frame_path
        else:
            Path(frame_path).unlink(missing_ok=True)

    print(f"  {len(frames)}/{len(timestamps)} frames with significant changes")
    return frames


def _thinking_config(genai_types, model: str):
    """Keep reasoning cheap and bounded for transcription-style work:
    2.5-series can switch thinking off; 3.x-series takes a level."""
    if model.startswith('gemini-2.5'):
        return genai_types.ThinkingConfig(thinking_budget=0)
    return genai_types.ThinkingConfig(thinking_level='low')


def read_frames_with_gemini(frames: List[Tuple[float, str]], course_name: str = "",
                            model: str = BOARD_MODEL_DEFAULT) -> Tuple[str, dict]:
    """
    Send batches of frames to Gemini Vision.
    Returns (combined board transcript with timestamps, usage_dict).
    usage_dict: {'model': str, 'input_tokens': int, 'output_tokens': int}
    """
    import google.genai as genai
    import google.genai.types as genai_types
    import time as _time

    # Use GEMINI_API_KEY, don't let GOOGLE_API_KEY override
    _gk = os.environ.pop('GOOGLE_API_KEY', None)
    client = genai.Client(api_key=os.getenv('GEMINI_API_KEY') or _gk or '')
    if _gk:
        os.environ['GOOGLE_API_KEY'] = _gk

    all_text_parts = []
    total_batches = math.ceil(len(frames) / BATCH_SIZE)
    total_input = 0
    total_output = 0

    prompt = f"""אלו frames מהרצאה של {course_name}.
לכל frame, רשום את כל התוכן הנראה על הלוח/מסך:
- נוסחאות מתמטיות — **חובה ב-LaTeX** ($...$ לתוך-שורה, $$...$$ לשורה נפרדת)
- הגדרות, משפטים, טענות
- ציורים/גרפים — תאר במילים
- כל טקסט אחר הנראה על המסך
פורמט:
[HH:MM:SS] - <תוכן מלא של הלוח>
אם frame זהה לקודם, כתוב: [HH:MM:SS] - (ללא שינוי)
**חשוב**: בדוק שכל נוסחה מתמטית נכונה לפני שאתה כותב אותה."""

    for batch_start in range(0, len(frames), BATCH_SIZE):
        batch = frames[batch_start:batch_start + BATCH_SIZE]
        batch_num = batch_start // BATCH_SIZE + 1
        print(f"  Reading board batch {batch_num}/{total_batches} (gemini-flash)")

        # Build parts list: alternating text timestamps and image parts
        parts = []
        for ts, frame_path in batch:
            if not Path(frame_path).exists():
                print(f"  ⚠️ Frame missing, skipping: {frame_path}")
                continue
            h, m, s = int(ts) // 3600, (int(ts) % 3600) // 60, int(ts) % 60
            ts_label = f"[{h:02d}:{m:02d}:{s:02d}]"
            parts.append(genai_types.Part.from_text(text=f"--- Frame {ts_label} ---"))
            with open(frame_path, 'rb') as f:
                img_bytes = f.read()
            parts.append(genai_types.Part.from_bytes(data=img_bytes, mime_type='image/jpeg'))
        parts.append(genai_types.Part.from_text(text=prompt))

        for attempt in range(3):
            try:
                # 10/2026: with default (high) thinking the model spent the whole
                # 4096-token output budget THINKING — board transcripts came back
                # cut mid-sentence (and billed in full). Measured on a real
                # calculus lecture: Pro + thinking_level=low + 12k budget covered
                # all frames with correct LaTeX at the same cost as Flash.
                response = client.models.generate_content(
                    model=model,
                    contents=parts,
                    config=genai_types.GenerateContentConfig(
                        max_output_tokens=12000,
                        temperature=0.1,
                        thinking_config=_thinking_config(genai_types, model),
                    )
                )
                from utils.usage_tracker import log_gemini as _lg; _lg(response, model, op='board_reader')
                try:
                    if 'MAX_TOKENS' in str(response.candidates[0].finish_reason):
                        print(f"  ⚠️ board batch {batch_num}: output hit the token limit (truncated)")
                except Exception:
                    pass
                text = (response.text or "").strip()
                all_text_parts.append(text)
                if response.usage_metadata:
                    total_input += getattr(response.usage_metadata, 'prompt_token_count', 0) or 0
                    total_output += getattr(response.usage_metadata, 'candidates_token_count', 0) or 0
                break
            except Exception as e:
                if attempt < 2:
                    wait = 5 * (2 ** attempt)  # 5s, 10s
                    print(f"  ⚠️ שגיאה בקריאת batch (ניסיון {attempt+1}/3): {e} — מנסה שוב בעוד {wait}s")
                    _time.sleep(wait)
                else:
                    print(f"  ⚠️ שגיאה בקריאת batch לאחר 3 ניסיונות: {e}")

    usage = {'model': model, 'input_tokens': total_input, 'output_tokens': total_output}
    return "\n".join(all_text_parts), usage


# Keep backward-compatible alias
def read_frames_with_claude(frames: List[Tuple[float, str]], course_name: str = "",
                             model: str = BOARD_MODEL_DEFAULT) -> Tuple[str, dict]:
    return read_frames_with_gemini(frames, course_name, model=BOARD_MODEL_DEFAULT)


def extract_board_transcript(video_path: str, course_name: str = "",
                              use_sonnet: bool = False,
                              save_frames_dir: Optional[str] = None) -> Tuple[str, List[Tuple[float, str]], dict]:
    """
    Main entry point: extract board content from lecture video.
    use_sonnet=True for math-heavy courses (calculus, linear algebra).

    Args:
        video_path:       local file path or HLS URL
        course_name:      human-readable course name (for Gemini prompt)
        use_sonnet:       use higher-quality model for math-heavy courses
        save_frames_dir:  if provided, frames are saved here permanently instead of cleaned up.
                          Caller can then embed them in the summary.

    Returns:
        (board_text, frame_paths, usage_dict)
        board_text   — OCR text with [HH:MM:SS] timestamps (already includes ![img](path) refs
                       if save_frames_dir was given)
        frame_paths  — list of (timestamp_sec, absolute_path) for saved frames
        usage_dict   — {'model', 'input_tokens', 'output_tokens', 'frame_count', 'frame_interval_sec'}
    """
    model = BOARD_MODEL_MATH if use_sonnet else BOARD_MODEL_DEFAULT
    tmp_dir = "data/tmp_frames"
    print(f"📷 קורא תוכן לוח מ: {Path(video_path).name if not video_path.startswith('http') else 'HLS stream'} (model={model.split('-')[1]})")
    print(f"   מרווח בין frames: {FRAME_INTERVAL_SEC}s")

    try:
        frames = extract_key_frames(video_path, tmp_dir)
        if not frames:
            return "", [], {}

        board_text, usage = read_frames_with_gemini(frames, course_name, model=BOARD_MODEL_DEFAULT)
        usage['frame_count'] = len(frames)
        usage['frame_interval_sec'] = FRAME_INTERVAL_SEC
        print(f"  ✅ תמלול לוח: {len(board_text)} תווים, {len(frames)} frames")

        # Save frames permanently if requested
        saved_frame_paths: List[Tuple[float, str]] = []
        if save_frames_dir:
            save_dir = Path(save_frames_dir)
            save_dir.mkdir(parents=True, exist_ok=True)
            for ts, fp in frames:
                h = int(ts) // 3600
                m = (int(ts) % 3600) // 60
                s = int(ts) % 60
                dest = save_dir / f"frame_{h:02d}h{m:02d}m{s:02d}s.jpg"
                try:
                    import shutil
                    shutil.copy2(fp, dest)
                    saved_frame_paths.append((ts, str(dest.resolve())))
                except Exception as e_cp:
                    print(f"  ⚠️ לא הצלחתי לשמור frame {ts:.0f}s: {e_cp}")

            if saved_frame_paths:
                print(f"  💾 נשמרו {len(saved_frame_paths)} frames ל: {save_frames_dir}")

                # Enrich board_text: insert image references before each timestamp block
                enriched_lines = []
                ts_to_path = {ts: p for ts, p in saved_frame_paths}
                for line in board_text.splitlines():
                    # Try to match [HH:MM:SS] at line start
                    import re as _re
                    m_ts = _re.match(r'^\[(\d{2}):(\d{2}):(\d{2})\]', line)
                    if m_ts:
                        h2, m2, s2 = int(m_ts.group(1)), int(m_ts.group(2)), int(m_ts.group(3))
                        line_ts = h2*3600 + m2*60 + s2
                        # Find closest saved frame (within 60s)
                        closest = min(ts_to_path.keys(), key=lambda t: abs(t - line_ts), default=None)
                        if closest is not None and abs(closest - line_ts) < 60:
                            frame_rel = ts_to_path[closest]
                            enriched_lines.append(f"\n![📷 צילום {m_ts.group(0)}]({frame_rel})\n")
                    enriched_lines.append(line)
                board_text = '\n'.join(enriched_lines)

        # Cleanup temp frames (saved ones already copied)
        for _, fp in frames:
            try:
                Path(fp).unlink(missing_ok=True)
            except Exception:
                pass
        try:
            Path(tmp_dir).rmdir()
        except Exception:
            pass

        return board_text, saved_frame_paths, usage

    except Exception as e:
        print(f"  ❌ שגיאה בקריאת לוח: {e}")
        import traceback
        traceback.print_exc()
        return "", [], {}
