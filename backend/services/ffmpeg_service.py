"""로컬 FFmpeg 기반 영상 합성 — 비디오 클립 concat + 오디오 오버레이 + 가사 자막"""
import asyncio
import json
import subprocess
from pathlib import Path
from backend.utils.file_manager import video_path, project_dir

TARGET_FPS = 24   # 모든 클립을 이 FPS로 통일
TARGET_W = 736    # 최종 영상 해상도
TARGET_H = 1280


def _wrap_words(text: str, max_words: int = 4) -> str:
    """단어 단위 줄바꿈 (max_words 단어마다 개행)."""
    words = text.split()
    lines = []
    for i in range(0, len(words), max_words):
        lines.append(' '.join(words[i:i + max_words]))
    return '\n'.join(lines)


WPL = 3              # 한 줄에 최대 단어 수
SILENCE_GAP = 1.0        # 이 시간 이상 보컬 공백이면 줄 순차 제거 시작
MIN_LINE_DISPLAY = 1.0   # 줄의 마지막 단어 start 기준 최소 표시 시간 (무음 제거 시)


def _generate_srt(scenes: list, out_path: Path,
                  whisper_lyrics: list = None) -> Path:
    """가사 자막 SRT 생성 — 노래방 스타일 2줄 교대.

    1줄에 단어를 하나씩 채워감. 3단어로 가득 차면 그 줄 고정,
    다른 줄부터 채우기 시작. 그 줄도 가득 차면 처음 줄 리셋 후 반복.
    2초 이상 보컬 공백 시, 먼저 채워진 줄부터 매초 순차 제거.
    """
    srt_path = out_path.parent / "subtitles.srt"

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
        srt_path.write_text('', encoding='utf-8')
        return srt_path

    # 2) 이벤트 수집: (시간, 표시 텍스트)
    #    단어 추가 이벤트 + 무음 갭 줄 제거 이벤트를 시간순으로 모음
    events = []  # [(time, display_text), ...]
    lines = ["", ""]
    line_count = [0, 0]
    line_filled_order = [0, 0]
    line_last_word_start = [0.0, 0.0]  # 각 줄의 마지막 단어 start 시점
    fill_seq = 0
    active = 0

    def _display():
        if lines[0] and lines[1]:
            return f"{lines[0]}\n{lines[1]}"
        return lines[0] or lines[1] or ""

    for wi, w in enumerate(all_words):
        w_start = w['start']
        prev_end = all_words[wi - 1].get('end', all_words[wi - 1]['start']) if wi > 0 else 0

        # 무음 갭 처리: 2초 이상이면 매초 먼저 채워진 줄부터 순차 제거
        if wi > 0:
            gap = w_start - prev_end
            if gap >= SILENCE_GAP and (lines[0] or lines[1]):
                clear_order = sorted([0, 1], key=lambda i: line_filled_order[i])
                clear_time = prev_end + SILENCE_GAP
                for li in clear_order:
                    if not lines[li]:
                        continue
                    if clear_time >= w_start:
                        break
                    # 마지막 단어 start 기준 2초 미만이면 아직 제거하지 않음
                    if clear_time - line_last_word_start[li] < MIN_LINE_DISPLAY:
                        continue
                    lines[li] = ""
                    line_count[li] = 0
                    line_filled_order[li] = 0
                    line_last_word_start[li] = 0.0
                    disp = _display()
                    # 줄 제거 후 표시할 텍스트가 있으면 이벤트, 없으면 빈 표시
                    events.append((clear_time, disp))
                    clear_time += 1.0

        # 현재 줄이 가득 차있으면 다른 줄로 전환 + 리셋
        if line_count[active] >= WPL:
            active = 1 - active
            lines[active] = ""
            line_count[active] = 0

        # 빈 줄에 처음 채울 때 순서 기록
        if line_count[active] == 0:
            fill_seq += 1
            line_filled_order[active] = fill_seq

        # 단어 추가
        lines[active] = (lines[active] + " " + w['text']).strip() if lines[active] else w['text']
        line_count[active] += 1
        line_last_word_start[active] = w_start

        events.append((w_start, _display()))

    # 마지막 단어의 종료 시점 (최소 표시 시간 보장)
    last_word = all_words[-1]
    last_end = max(last_word.get('end', last_word['start'] + 1.0),
                   last_word['start'] + MIN_LINE_DISPLAY)

    # 3) 이벤트 → SRT 변환 (겹침 없이 연결)
    #    빈 텍스트 이벤트 제거 + 시간순 정렬
    events = [(t, text) for t, text in events if text]
    events.sort(key=lambda e: e[0])

    entries = []
    idx = 1
    for i, (t, text) in enumerate(events):
        # 종료 시점: 다음 이벤트 시작 (절대 넘지 않음)
        if i + 1 < len(events):
            t_end = events[i + 1][0]
        else:
            t_end = last_end
        # 시작과 종료가 같거나 역전이면 스킵
        if t_end <= t:
            continue
        entries.append(
            f"{idx}\n"
            f"{_sec_to_srt(t)} --> {_sec_to_srt(t_end)}\n"
            f"{text}\n"
        )
        idx += 1

    srt_path.write_text('\n'.join(entries), encoding='utf-8')
    return srt_path


def _sec_to_srt(sec: float) -> str:
    """초 → SRT 시간 포맷 (HH:MM:SS,mmm)."""
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int((sec % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


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
) -> str:
    """비디오 클립들을 연결하고 오디오 합성 + 가사 자막."""
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
            '-vf', f'scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=decrease,'
                   f'pad={TARGET_W}:{TARGET_H}:(ow-iw)/2:(oh-ih)/2:black',
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

    # 5. 자막 SRT 생성 (비활성화)
    srt_path = None
    # if whisper_lyrics or scenes:
    #     srt_path = _generate_srt(scenes, out_path, whisper_lyrics=whisper_lyrics)

    # 6. 오디오 fade-out (마지막 2초)
    fade_sec = 2.0
    audio_fade = f"afade=t=out:st={audio_dur - fade_sec}:d={fade_sec}" if audio_dur > fade_sec else ""

    # 7. 오디오 합성 + 자막 → 최종 영상
    if srt_path and srt_path.exists() and srt_path.stat().st_size > 10:
        srt_escaped = str(srt_path.resolve()).replace('\\', '/').replace(':', r'\:')
        cmd_final = [
            'ffmpeg', '-y',
            '-i', str(concat_tmp),
            '-i', audio_path,
            '-vf', (
                f"subtitles='{srt_escaped}'"
                f":force_style='FontName=Malgun Gothic,FontSize=18,"
                f"PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
                f"Outline=2,Shadow=1,Alignment=2,"
                f"MarginV=60'"
            ),
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
