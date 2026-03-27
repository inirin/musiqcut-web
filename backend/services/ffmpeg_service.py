"""로컬 FFmpeg 기반 영상 합성 — 비디오 클립 concat + 오디오 오버레이 + 가사 자막"""
import asyncio
import json
import subprocess
from pathlib import Path
from backend.utils.file_manager import video_path, project_dir

TARGET_FPS = 24   # 모든 클립을 이 FPS로 통일
TARGET_W = 736    # 최종 영상 해상도
TARGET_H = 1280

# 폰트 경로
_FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
FONT_TITLE = str(_FONTS_DIR / "GmarketSansTTFBold.ttf")
FONT_LYRICS = str(_FONTS_DIR / "NanumSquareBold.ttf")


def _wrap_words(text: str, max_words: int = 4) -> str:
    """단어 단위 줄바꿈 (max_words 단어마다 개행)."""
    words = text.split()
    lines = []
    for i in range(0, len(words), max_words):
        lines.append(' '.join(words[i:i + max_words]))
    return '\n'.join(lines)


WPL = 3              # 한 줄에 최대 단어 수
SILENCE_GAP = 3.0        # 이 시간 이상 보컬 공백이면 줄 순차 제거 시작
MIN_LINE_DISPLAY = 2.0   # 줄의 마지막 단어 start 기준 최소 표시 시간 (무음 제거 시)
FADE_OUT_MS = 300        # 줄 사라질 때 fade-out (밀리초)


def _generate_ass(scenes: list, out_path: Path,
                  whisper_lyrics: list = None) -> Path:
    """가사 자막 ASS 생성 — 노래방 스타일 2줄 교대 + fade-out.

    슬롯 0(윗줄) / 슬롯 1(아랫줄) 독립 추적.
    무음 갭 제거(케이스2) 및 곡 종료(케이스3) 시 fade-out 적용.
    """
    ass_path = out_path.parent / "subtitles.ass"
    fonts_escaped = str(_FONTS_DIR.resolve()).replace('\\', '/')

    # 1) 전체 단어 스트림 수집
    source = whisper_lyrics if whisper_lyrics else scenes
    all_words = []
    for seg in source:
        words = seg.get('words', [])
        valid = [w for w in words
                 if w.get('text', '').strip() and w.get('end', 0) > w.get('start', 0)]
        if valid:
            all_words.extend(valid)
        elif not valid:
            if 'has_vocal' in seg:
                if not seg.get('has_vocal', False):
                    continue
                text = seg.get('text', '').strip()
                start = seg.get('start', 0)
                end = seg.get('end', start + 5)
            else:
                vocal_lines = seg.get('vocal_lines', [])
                if not vocal_lines or not any(l.strip() for l in vocal_lines):
                    continue
                text = ' '.join(l.strip() for l in vocal_lines if l.strip())
                start = seg.get('start_sec', 0)
                end = start + seg.get('duration', 5)
            if text:
                all_words.append({'text': text, 'start': start + 0.3, 'end': end - 0.2})

    if not all_words:
        ass_path.write_text('', encoding='utf-8')
        return ass_path

    # 2) 2줄 push-up 세그먼트 추적
    #    슬롯 0 = 윗줄 (이전 완성 문구), 슬롯 1 = 아랫줄 (현재 채우는 줄)
    #    아랫줄 3단어 차면 → 윗줄로 즉시 이동, 아랫줄 새로 시작
    slot_segs = [[], []]   # [(start, end, text, fade), ...]
    slot_start = [0.0, 0.0]
    slot_text = ["", ""]

    def _close(si, t, fade=False):
        """슬롯의 현재 텍스트를 세그먼트로 확정."""
        if slot_text[si]:
            end = t + (FADE_OUT_MS / 1000 if fade else 0)
            slot_segs[si].append((slot_start[si], end, slot_text[si], fade))
        slot_text[si] = ""
        slot_start[si] = t

    bottom = ""          # 아랫줄 텍스트
    bcount = 0           # 아랫줄 단어 수
    top_lws = 0.0        # 윗줄 마지막 단어 start
    bottom_lws = 0.0     # 아랫줄 마지막 단어 start
    top_order = 0
    bottom_order = 0
    seq = 0

    for wi, w in enumerate(all_words):
        ws = w['start']
        prev_end = all_words[wi - 1].get('end', all_words[wi - 1]['start']) if wi > 0 else 0

        # 케이스 2: 무음 갭 → 먼저 채워진 줄부터 순차 fade-out
        if wi > 0:
            gap = ws - prev_end
            if gap >= SILENCE_GAP and (slot_text[0] or slot_text[1]):
                pairs = []
                if slot_text[0]:
                    pairs.append((0, top_order, top_lws))
                if slot_text[1]:
                    pairs.append((1, bottom_order, bottom_lws))
                pairs.sort(key=lambda x: x[1])
                ct = prev_end + SILENCE_GAP
                for si, _, lws in pairs:
                    if ct >= ws:
                        break
                    if ct - lws < MIN_LINE_DISPLAY:
                        continue
                    _close(si, ct, fade=True)
                    if si == 0:
                        top_order = 0
                        top_lws = 0.0
                    else:
                        bottom = ""
                        bcount = 0
                        bottom_order = 0
                        bottom_lws = 0.0
                    ct += 1.0

        # 아랫줄 가득 참 → 윗줄로 즉시 이동 + 아랫줄 리셋
        if bcount >= WPL:
            _close(0, ws)                   # 윗줄 이전 내용 마감
            # fade-out 중인 이전 Top 세그먼트가 현재 시점 넘으면 잘라내기
            if slot_segs[0] and slot_segs[0][-1][1] > ws:
                prev = slot_segs[0][-1]
                slot_segs[0][-1] = (prev[0], ws, prev[2], False)
            _close(1, ws)                   # 아랫줄 마감
            slot_text[0] = bottom           # 아랫줄 텍스트를 윗줄에 즉시 표시
            slot_start[0] = ws
            top_lws = bottom_lws
            top_order = bottom_order
            bottom = ""
            bcount = 0

        # 아랫줄 첫 단어 시 순서 기록
        if bcount == 0:
            seq += 1
            bottom_order = seq

        # 단어 추가 (항상 아랫줄)
        bottom = (bottom + " " + w['text']).strip() if bottom else w['text']
        bcount += 1
        bottom_lws = ws

        # 슬롯 1(아랫줄) 세그먼트 갱신
        if bottom != slot_text[1]:
            _close(1, ws)
            slot_text[1] = bottom
            slot_start[1] = ws

    # 케이스 3: 곡 종료 → 모든 슬롯 fade-out
    last_word = all_words[-1]
    last_end = max(last_word.get('end', last_word['start'] + 1.0),
                   last_word['start'] + MIN_LINE_DISPLAY)
    for s in range(2):
        _close(s, last_end, fade=True)

    # 3) ASS 파일 생성
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {TARGET_W}\n"
        f"PlayResY: {TARGET_H}\n"
        "WrapStyle: 0\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Top,NanumSquareOTFB00,48,&H00FFFFFF,&H000000FF,"
        "&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,3,1,2,10,10,375,1\n"
        "Style: Bottom,NanumSquareOTFB00,48,&H00FFFFFF,&H000000FF,"
        "&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,3,1,2,10,10,320,1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    style_names = ["Top", "Bottom"]
    dialogues = []
    for si in range(2):
        for start, end, text, fade in slot_segs[si]:
            if end <= start:
                continue
            s_str = _sec_to_ass(start)
            e_str = _sec_to_ass(end)
            prefix = f"{{\\fad(0,{FADE_OUT_MS})}}" if fade else ""
            dialogues.append((start, f"Dialogue: 0,{s_str},{e_str},{style_names[si]},,0,0,0,,{prefix}{text}"))

    dialogues.sort(key=lambda x: x[0])
    content = header + '\n'.join(d[1] for d in dialogues) + '\n'
    ass_path.write_text(content, encoding='utf-8-sig')
    return ass_path


def _sec_to_ass(sec: float) -> str:
    """초 → ASS 시간 포맷 (H:MM:SS.cc)."""
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    cs = int((sec % 1) * 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


async def _get_duration(file_path: str) -> float:
    """ffprobe로 파일 길이(초) 반환."""
    cmd = [
        'ffprobe', '-v', 'quiet', '-print_format', 'json',
        '-show_format', file_path,
    ]
    result = await asyncio.to_thread(
        subprocess.run, cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return 0.0
    data = json.loads(result.stdout)
    return float(data.get("format", {}).get("duration", 0))


async def render_video(
    project_id: str,
    clip_paths: list[str],
    audio_path: str,
    scenes: list = None,
    whisper_lyrics: list = None,
    title: str = None,
    theme: str = None,
) -> str:
    """비디오 클립들을 연결하고 오디오 합성 + 가사 자막 + 제목 + 테마."""
    out_path = video_path(project_id)
    proj = project_dir(project_id)

    # 장면별 목표 duration 계산 (클립 trim용)
    scene_durations = []
    if whisper_lyrics:
        for seg in whisper_lyrics:
            scene_durations.append(seg.get('end', 5) - seg.get('start', 0))
    elif scenes:
        for sc in scenes:
            scene_durations.append(sc.get('duration', 5))

    # 1. 클립 해상도/FPS 정규화 + 정확한 duration trim
    norm_dir = proj / "clips" / "normalized"
    norm_dir.mkdir(parents=True, exist_ok=True)
    normalized = []
    for i, p in enumerate(clip_paths):
        norm_path = norm_dir / f"norm_{i:02d}.mp4"
        # 장면 duration이 있으면 정확히 trim하여 드리프트 방지
        trim_args = []
        if i < len(scene_durations):
            trim_args = ['-t', f'{scene_durations[i]:.3f}']
        cmd_norm = [
            'ffmpeg', '-y', '-i', p,
            '-vf', f'scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,'
                   f'crop={TARGET_W}:{TARGET_H}',
            '-r', str(TARGET_FPS),
            '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-an',
            *trim_args,
            str(norm_path),
        ]
        result = await asyncio.to_thread(
            subprocess.run, cmd_norm, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg 정규화 오류 (clip {i}):\n{result.stderr[-500:]}")
        normalized.append(norm_path)

    # 2. FFmpeg concat 리스트 생성
    concat_file = proj / "concat.txt"
    lines_txt = []
    for p in normalized:
        abs_path = p.resolve().as_posix()
        lines_txt.append(f"file '{abs_path}'")
    concat_file.write_text('\n'.join(lines_txt), encoding='utf-8')

    # 3. 클립 연결
    concat_tmp = proj / "video" / "concat_tmp.mp4"
    cmd_concat = [
        'ffmpeg', '-y',
        '-f', 'concat', '-safe', '0', '-i', str(concat_file),
        '-c:v', 'copy', '-an',
        str(concat_tmp),
    ]
    result = await asyncio.to_thread(
        subprocess.run, cmd_concat, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg concat 오류:\n{result.stderr[-500:]}")

    # 4. 비디오/오디오 길이 비교 → 부족하면 마지막 프레임 freeze
    video_dur = await _get_duration(str(concat_tmp))
    audio_dur = await _get_duration(audio_path)

    if audio_dur > video_dur + 0.04:
        # 마지막 프레임으로 나머지 채움 (tpad filter)
        padded = proj / "video" / "padded_tmp.mp4"
        pad_dur = audio_dur - video_dur
        cmd_pad = [
            'ffmpeg', '-y',
            '-i', str(concat_tmp),
            '-vf', f'tpad=stop_mode=clone:stop_duration={pad_dur:.2f}',
            '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
            str(padded),
        ]
        result = await asyncio.to_thread(
            subprocess.run, cmd_pad, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg pad 오류:\n{result.stderr[-500:]}")
        concat_tmp.unlink(missing_ok=True)
        concat_tmp = padded

    # 5. 자막 ASS 생성 (fade-out 지원)
    ass_path = None
    if whisper_lyrics or scenes:
        ass_path = _generate_ass(scenes, out_path, whisper_lyrics=whisper_lyrics)

    # 6. 오디오 fade-out (마지막 2초)
    fade_sec = 2.0
    audio_fade = f"afade=t=out:st={audio_dur - fade_sec}:d={fade_sec}" if audio_dur > fade_sec else ""

    # 7. 오디오 합성 + 자막 + 제목 → 최종 영상
    vf_parts = []

    # 7a. 가사 자막 (ASS — NanumSquare Bold, fade-out 포함)
    if ass_path and ass_path.exists() and ass_path.stat().st_size > 10:
        ass_escaped = str(ass_path.resolve()).replace('\\', '/').replace(':', r'\:')
        fonts_escaped = str(_FONTS_DIR.resolve()).replace('\\', '/').replace(':', r'\:')
        vf_parts.append(
            f"ass='{ass_escaped}':fontsdir='{fonts_escaped}'"
        )

    # 7b. 제목 (Gmarket Sans Bold, 상단, 0~3초 + fade-out only)
    if title:
        font_escaped = FONT_TITLE.replace('\\', '/').replace(':', r'\:')

        def _escape_dt(t):
            return t.replace("\\", "\\\\").replace("'", "\u2019").replace(":", "\\:")

        def _est_w(t):
            return sum(0.9 if ord(c) > 127 else 0.5 for c in t)

        # 제목 길이에 따라 폰트 크기 가변 (양쪽 여백 40px 확보)
        max_w = TARGET_W - 80  # 656px 사용 가능 영역

        # 줄 수 결정 — 폰트 48px 이상 될 때까지 줄 수 늘림
        MIN_TITLE_FS = 48
        words = title.split()
        title_lines = [title]

        for num_lines in range(1, min(len(words), 3) + 1):
            if num_lines == 1:
                title_lines = [title]
            else:
                title_lines = []
                remaining = list(words)
                for li in range(num_lines):
                    lines_left = num_lines - li
                    total_rem = _est_w(' '.join(remaining))
                    target = total_rem / lines_left
                    accum = 0
                    cut = len(remaining)
                    for wi, wd in enumerate(remaining):
                        accum += _est_w(wd) + (0.5 if wi < len(remaining) - 1 else 0)
                        if accum >= target and wi > 0:
                            cut = wi + 1
                            break
                    if li == num_lines - 1:
                        cut = len(remaining)
                    title_lines.append(' '.join(remaining[:cut]))
                    remaining = remaining[cut:]
                if remaining:
                    title_lines[-1] += ' ' + ' '.join(remaining)

            longest_w = max(_est_w(l) for l in title_lines)
            fs = int(max_w / max(longest_w, 1))
            if fs >= MIN_TITLE_FS:
                break

        # 최종 폰트 크기 (최대 88)
        longest_w = max(_est_w(l) for l in title_lines)
        title_fs = min(88, max(48, int(max_w / max(longest_w, 1))))
        border_w = max(3, title_fs // 16)
        # 0~2.7초 완전 표시, 2.7~3.0초 fade-out
        fade_start = 2.7
        fade_end = 3.0
        title_common = (
            f":fontcolor=#FFF700"
            f":borderw={border_w}:bordercolor=black@0.8"
            f":shadowx=3:shadowy=3:shadowcolor=black@0.5"
            f":enable='between(t,0,{fade_end})'"
            f":alpha='if(gt(t,{fade_start}),max(({fade_end}-t)/{fade_end - fade_start},0),1)'"
        )

        for li, line in enumerate(title_lines):
            y = 180 + li * (title_fs + 6)
            vf_parts.append(
                f"drawtext=fontfile='{font_escaped}'"
                f":text='{_escape_dt(line)}'"
                f":fontsize={title_fs}"
                f":x=(w-text_w)/2:y={y}"
                + title_common
            )

    # 7c. 테마 (제목 아래, 노란색, 가변 크기, 길면 2줄)
    if theme and title:
        # " - " 또는 " — " 뒤쪽만 표시 (앞쪽은 제목과 중복)
        for sep in [' - ', ' — ', ' – ']:
            if sep in theme:
                theme_display = theme.split(sep, 1)[1].strip()
                break
        else:
            theme_display = theme

        # 1) 줄 수 결정 — 폰트 40px 이상 될 때까지 줄 수 늘림
        MIN_THEME_FS = 40
        words = theme_display.split()
        theme_lines = [theme_display]

        for num_lines in range(1, min(len(words), 4) + 1):
            # N줄 균등 분할 (윗줄이 약간 더 길게)
            if num_lines == 1:
                theme_lines = [theme_display]
            else:
                theme_lines = []
                remaining = list(words)
                for li in range(num_lines):
                    lines_left = num_lines - li
                    total_rem = _est_w(' '.join(remaining))
                    target = total_rem / lines_left
                    accum = 0
                    cut = len(remaining)
                    for wi, wd in enumerate(remaining):
                        accum += _est_w(wd) + (0.5 if wi < len(remaining) - 1 else 0)
                        if accum >= target and wi > 0:
                            cut = wi + 1
                            break
                    if li == num_lines - 1:
                        cut = len(remaining)
                    theme_lines.append(' '.join(remaining[:cut]))
                    remaining = remaining[cut:]
                if remaining:
                    theme_lines[-1] += ' ' + ' '.join(remaining)

            longest_w = max(_est_w(l) for l in theme_lines)
            fs = int(max_w / max(longest_w, 1))
            if fs >= MIN_THEME_FS:
                break

        # 2) 최종 가장 긴 줄 기준 폰트 크기 (최대 80)
        longest_w = max(_est_w(l) for l in theme_lines)
        theme_fs = min(80, max(32, int(max_w / max(longest_w, 1))))
        theme_bw = max(2, theme_fs // 16)
        theme_y = 180 + len(title_lines) * (title_fs + 6) + 10
        theme_common = (
            f":fontcolor=white"
            f":borderw={theme_bw}:bordercolor=black@0.6"
            f":shadowx=1:shadowy=1:shadowcolor=black@0.4"
            f":enable='between(t,0,{fade_end})'"
            f":alpha='if(gt(t,{fade_start}),max(({fade_end}-t)/{fade_end - fade_start},0),1)'"
        )

        for li, line in enumerate(theme_lines):
            y = theme_y + li * (theme_fs + 6)
            vf_parts.append(
                f"drawtext=fontfile='{font_escaped}'"
                f":text='{_escape_dt(line)}'"
                f":fontsize={theme_fs}"
                f":x=(w-text_w)/2:y={y}"
                + theme_common
            )

    if vf_parts:
        cmd_final = [
            'ffmpeg', '-y',
            '-i', str(concat_tmp),
            '-i', audio_path,
            '-vf', ','.join(vf_parts),
            *(['-af', audio_fade] if audio_fade else []),
            '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '192k',
            '-shortest',
            '-movflags', '+faststart',
            str(out_path),
        ]
    else:
        cmd_final = [
            'ffmpeg', '-y',
            '-i', str(concat_tmp),
            '-i', audio_path,
            *(['-af', audio_fade] if audio_fade else []),
            '-c:v', 'copy',
            '-c:a', 'aac', '-b:a', '192k',
            '-shortest',
            '-movflags', '+faststart',
            str(out_path),
        ]
    result = await asyncio.to_thread(
        subprocess.run, cmd_final, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg 최종 합성 오류:\n{result.stderr[-500:]}")

    # 임시 파일 정리
    concat_tmp.unlink(missing_ok=True)
    import shutil
    shutil.rmtree(str(norm_dir), ignore_errors=True)

    return str(out_path)
