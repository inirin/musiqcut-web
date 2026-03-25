"""가사 타임스탬프 추출 — Demucs 보컬 분리 + faster-whisper"""
import asyncio
import math
import sys
from difflib import SequenceMatcher
from pathlib import Path


# 장면 길이 제약
MIN_SCENE_SEC = 3.0
MAX_SCENE_SEC = 8.0
TARGET_SCENE_SEC = 5.0


async def extract_lyrics_timestamps(
    audio_path: str,
    lyrics_text: str,
    demucs_dir: str,
    total_duration: float = 0,
) -> list[dict]:
    """Suno 음원에서 Whisper로 가사 추출 → 5초(클립 길이) 고정 간격 세그먼트.

    Returns:
        [{"text": "가사", "start": 0.0, "end": 5.0,
          "words": [...], "has_vocal": True}, ...]
    """
    from backend.services.lipsync_precheck import separate_vocals

    # 1) 보컬 분리
    vocals_path = await separate_vocals(audio_path, demucs_dir)
    print(f"[LyricsSync] 보컬 분리 완료: {vocals_path}", file=sys.stderr)

    # 2) Whisper 전사 → 단어 타임스탬프
    raw_segments = await asyncio.to_thread(_transcribe_vocals, vocals_path)
    all_words = []
    for seg in raw_segments:
        for w in seg.get("words", []):
            if w["text"].strip():
                all_words.append(w)
    print(f"[LyricsSync] Whisper 단어 {len(all_words)}개 추출", file=sys.stderr)

    # 3) 5초 고정 간격으로 단어 배분
    if total_duration <= 0:
        from backend.services.suno_service import measure_audio_duration
        total_duration = await measure_audio_duration(audio_path)

    clip_sec = TARGET_SCENE_SEC

    # 보컬 시작점 감지 — 인트로 무음 구간 최소화
    vocal_start = 0.0
    if all_words:
        first_word_start = all_words[0]["start"]
        # 첫 보컬이 clip_sec 이후에 시작하면, 보컬 직전부터 세그먼트 시작
        if first_word_start >= clip_sec:
            # 보컬 1초 전부터 시작 (최소 0)
            vocal_start = max(0.0, first_word_start - 1.0)
            # clip_sec 단위로 정렬
            vocal_start = round(math.floor(vocal_start / clip_sec) * clip_sec, 2)
            print(f"[LyricsSync] 인트로 스킵: 보컬 시작 {first_word_start:.1f}초 → "
                  f"세그먼트 시작 {vocal_start:.1f}초", file=sys.stderr)

    effective_duration = total_duration - vocal_start
    # 부동소수점 오차 방지 (30.001초 → 7세그먼트 되는 문제)
    n_clips = max(1, round(effective_duration / clip_sec))
    segments = []
    for i in range(n_clips):
        s = round(vocal_start + i * clip_sec, 2)
        e = round(min(vocal_start + (i + 1) * clip_sec, total_duration), 2)
        # 단어의 시작점이 이 세그먼트에 속하면 포함
        words_in = [w for w in all_words
                    if s <= w["start"] < e]
        text = " ".join(w["text"] for w in words_in).strip()
        segments.append({
            "text": text,
            "start": s,
            "end": e,
            "words": words_in,
            "has_vocal": len(words_in) > 0,
        })

    vocal_count = sum(1 for sg in segments if sg["has_vocal"])
    print(f"[LyricsSync] {n_clips}개 세그먼트 (보컬 {vocal_count}개, "
          f"시작 {vocal_start:.1f}초)", file=sys.stderr)
    return segments


def _validate_alignment(wx_words: list, raw_words: list) -> bool:
    """whisperx 결과 최소 검증: words 비어있음 + 순서 역전만 체크."""
    if not wx_words:
        return False
    for i in range(len(wx_words) - 1):
        if wx_words[i+1]["start"] < wx_words[i]["start"]:
            return False
    return True


def _transcribe_vocals(vocals_path: str) -> list[dict]:
    """faster-whisper 전사 + whisperx forced alignment로 정밀 단어 타임스탬프."""
    import sys

    # 1) faster-whisper로 텍스트 + 대략적 세그먼트 추출
    from faster_whisper import WhisperModel
    model = WhisperModel("large-v3", device="cpu", compute_type="int8")
    segments, info = model.transcribe(
        vocals_path,
        language="ko",
        word_timestamps=True,
        vad_filter=False,
        condition_on_previous_text=False,  # 환각 전파 방지
    )
    raw_segments = []
    for seg in segments:
        # 환각 필터링: 압축률 비정상이면 스킵 (반복 환각)
        # no_speech_prob는 노래 보컬에서 오탐이 심해 사용하지 않음
        if seg.compression_ratio > 2.4:
            print(f"[LyricsSync] 환각 스킵 (compress={seg.compression_ratio:.1f}): "
                  f"'{seg.text.strip()[:30]}'", file=sys.stderr)
            continue
        entry = {"text": seg.text.strip(), "start": seg.start, "end": seg.end}
        if seg.words:
            entry["words"] = [{"text": w.word.strip(), "start": w.start, "end": w.end}
                              for w in seg.words if w.word.strip()]
        raw_segments.append(entry)

    # 2) whisperx forced alignment으로 단어 타임스탬프 보정
    try:
        import whisperx
        import torch

        # align 모델은 CPU 사용 (CUDA는 ComfyUI가 점유, cuDNN 호환성 이슈 방지)
        device = "cpu"
        audio = whisperx.load_audio(vocals_path)

        # whisperx align 입력 형식으로 변환
        align_segments = [{"text": s["text"], "start": s["start"], "end": s["end"]}
                          for s in raw_segments if s["text"].strip()]

        if align_segments:
            align_model, align_metadata = whisperx.load_align_model(
                language_code="ko", device=device)
            aligned = whisperx.align(
                align_segments, align_model, align_metadata,
                audio, device, return_char_alignments=False)

            # 정렬된 결과를 세그먼트별로 검증 + fallback
            aligned_segs = aligned.get("segments", [])

            # align 모델 메모리 해제
            del align_model

            # raw_segments와 1:1 매칭 (Whisper 결과를 fallback용으로 보존)
            raw_by_text = {}
            for rs in raw_segments:
                if rs["text"].strip():
                    raw_by_text[rs["text"].strip()] = rs

            result = []
            for aseg in aligned_segs:
                text = aseg.get("text", "").strip()
                entry = {"text": text,
                         "start": aseg.get("start", 0),
                         "end": aseg.get("end", 0)}
                words = aseg.get("words", [])
                wx_words = [{"text": w.get("word", "").strip(),
                             "start": w.get("start", 0),
                             "end": w.get("end", 0)}
                            for w in words if w.get("word", "").strip()
                            and "start" in w and "end" in w]

                raw = raw_by_text.get(text)
                raw_words = raw.get("words", []) if raw else []

                # 검증: Whisper vs whisperx 단어별 비교
                use_whisperx = _validate_alignment(wx_words, raw_words)

                if use_whisperx:
                    entry["words"] = wx_words
                elif raw_words:
                    entry["words"] = raw_words
                    print(f"[LyricsSync] whisperx 검증 실패 → Whisper fallback: "
                          f"'{text[:20]}...'", file=sys.stderr)
                else:
                    entry["words"] = wx_words  # 둘 다 없으면 그냥 사용

                result.append(entry)

            print(f"[LyricsSync] whisperx forced alignment 완료 ({len(result)}개 세그먼트)",
                  file=sys.stderr)
            return result

    except Exception as e:
        print(f"[LyricsSync] whisperx alignment 실패, faster-whisper 결과 사용: {e}",
              file=sys.stderr)

    return raw_segments


def _align_lyrics_to_segments(
    vocal_lines: list[str],
    segments: list[dict],
) -> list[dict]:
    """원본 가사 라인을 Whisper 세그먼트에 순차 매칭.

    Whisper 인식이 불완전할 수 있으므로 fuzzy matching + 순차 진행.
    매칭 실패한 라인은 인접 세그먼트 기반으로 보간.
    """
    if not segments:
        return [{"line": l, "start": 0.0, "end": 0.0} for l in vocal_lines], 0

    total_duration = segments[-1]["end"] if segments else 0.0
    timed = []
    seg_idx = 0

    for line in vocal_lines:
        best_score = 0.0
        best_seg = None
        # 현재 위치부터 앞으로 검색 (순차 매칭)
        search_range = min(seg_idx + len(segments) // 2 + 3, len(segments))
        for j in range(seg_idx, search_range):
            score = SequenceMatcher(None, line, segments[j]["text"]).ratio()
            if score > best_score:
                best_score = score
                best_seg = j

        if best_seg is not None and best_score > 0.3:
            timed.append({
                "line": line,
                "start": segments[best_seg]["start"],
                "end": segments[best_seg]["end"],
                "_matched": True,
            })
            seg_idx = best_seg + 1
        else:
            timed.append({"line": line, "start": -1, "end": -1, "_matched": False})

    matched_count = sum(1 for t in timed if t["_matched"])

    # 매칭 실패한 라인 보간
    _interpolate_missing(timed, total_duration)

    # _matched 플래그 제거
    for t in timed:
        t.pop("_matched", None)

    return timed, matched_count


def _interpolate_missing(timed: list[dict], total_duration: float):
    """매칭 실패(-1) 라인의 타임스탬프를 인접 값으로 보간."""
    n = len(timed)
    for i in range(n):
        if timed[i]["start"] < 0:
            # 이전/다음 유효 타임스탬프 찾기
            prev_end = 0.0
            for p in range(i - 1, -1, -1):
                if timed[p]["end"] >= 0:
                    prev_end = timed[p]["end"]
                    break
            next_start = total_duration
            gap_count = 1
            for q in range(i + 1, n):
                if timed[q]["start"] >= 0:
                    next_start = timed[q]["start"]
                    break
                gap_count += 1

            # 균등 분할
            step = (next_start - prev_end) / gap_count
            offset = 0
            for k in range(i, min(i + gap_count, n)):
                if timed[k]["start"] < 0:
                    timed[k]["start"] = prev_end + step * offset
                    timed[k]["end"] = prev_end + step * (offset + 1)
                    offset += 1


def group_lines_into_scenes(
    timed_lines: list[dict],
    total_duration: float,
) -> list[dict]:
    """타임스탬프 라인들을 장면 단위로 그룹핑.

    인트로(가사 전)/아웃트로(가사 후) 구간도 별도 장면으로 생성하고,
    MAX_SCENE_SEC 초과 장면은 자동 분할합니다.

    Returns:
        [{"scene_no": 1, "vocal_lines": [...], "start_sec": 0.0,
          "end_sec": 5.2, "duration": 5.2}, ...]
    """
    if not timed_lines:
        return []

    scenes = []

    # ── 인트로 구간: 가사 시작 전 (악기 인트로 등) ──
    first_vocal = timed_lines[0]["start"]
    if first_vocal >= MIN_SCENE_SEC:
        intro_scenes = _split_gap(0.0, first_vocal, lyrics_label="(intro)")
        scenes.extend(intro_scenes)

    # ── 가사 구간: 라인 기반 그룹핑 ──
    current_lines = []
    scene_start = first_vocal

    for i, tl in enumerate(timed_lines):
        current_lines.append(tl["line"])
        current_end = tl["end"]
        current_dur = current_end - scene_start

        next_dur = 0
        if i + 1 < len(timed_lines):
            next_dur = timed_lines[i + 1]["end"] - scene_start

        should_split = (
            (current_dur >= TARGET_SCENE_SEC and next_dur > MAX_SCENE_SEC)
            or current_dur >= MAX_SCENE_SEC
            or i == len(timed_lines) - 1
        )

        if should_split:
            scenes.append({
                "scene_no": 0,  # 나중에 재번호 부여
                "vocal_lines": current_lines[:],
                "start_sec": round(scene_start, 2),
                "end_sec": round(current_end, 2),
                "duration": round(current_end - scene_start, 2),
            })
            current_lines = []
            if i + 1 < len(timed_lines):
                scene_start = timed_lines[i + 1]["start"]

    # ── 장면 사이 갭(간주 등)을 별도 장면으로 삽입 ──
    scenes_with_gaps = []
    for j, sc in enumerate(scenes):
        if j > 0:
            prev_end = scenes[j - 1]["end_sec"]
            cur_start = sc["start_sec"]
            if cur_start - prev_end >= MIN_SCENE_SEC:
                gap_scenes = _split_gap(prev_end, cur_start,
                                        lyrics_label="(interlude)")
                scenes_with_gaps.extend(gap_scenes)
        scenes_with_gaps.append(sc)
    scenes = scenes_with_gaps

    # 마지막 가사 장면이 너무 짧으면 이전 장면에 합치기
    vocal_start_idx = len(scenes) - 1
    while vocal_start_idx >= 0 and scenes[vocal_start_idx].get("_is_gap"):
        vocal_start_idx -= 1
    if (len(scenes) >= 2
            and not scenes[-1].get("_is_gap")
            and scenes[-1]["duration"] < MIN_SCENE_SEC
            and vocal_start_idx >= 1
            and not scenes[-2].get("_is_gap")):
        last = scenes.pop()
        scenes[-1]["vocal_lines"].extend(last["vocal_lines"])
        scenes[-1]["end_sec"] = last["end_sec"]
        scenes[-1]["duration"] = round(
            scenes[-1]["end_sec"] - scenes[-1]["start_sec"], 2)

    # ── 아웃트로 구간: 마지막 가사 이후 ──
    last_vocal_end = timed_lines[-1]["end"]
    if total_duration - last_vocal_end >= MIN_SCENE_SEC:
        outro_scenes = _split_gap(last_vocal_end, total_duration,
                                  lyrics_label="(outro)")
        scenes.extend(outro_scenes)
    elif total_duration - last_vocal_end > 0.5:
        # 짧은 잔여 → 마지막 장면에 합치기
        scenes[-1]["end_sec"] = round(total_duration, 2)
        scenes[-1]["duration"] = round(
            scenes[-1]["end_sec"] - scenes[-1]["start_sec"], 2)

    # ── MAX_SCENE_SEC 초과 장면 분할 ──
    scenes = _split_long_scenes(scenes)

    # ── 장면 번호 재부여 ──
    for i, sc in enumerate(scenes):
        sc["scene_no"] = i + 1
        sc.pop("_is_gap", None)

    return scenes


def _split_gap(start: float, end: float,
               lyrics_label: str = "") -> list[dict]:
    """가사 없는 구간(인트로/아웃트로)을 TARGET_SCENE_SEC 단위로 분할."""
    dur = end - start
    if dur <= 0:
        return []
    n = max(1, round(dur / TARGET_SCENE_SEC))
    step = dur / n
    result = []
    for i in range(n):
        s = start + step * i
        e = start + step * (i + 1) if i < n - 1 else end
        result.append({
            "scene_no": 0,
            "vocal_lines": [lyrics_label] if lyrics_label else [],
            "start_sec": round(s, 2),
            "end_sec": round(e, 2),
            "duration": round(e - s, 2),
            "_is_gap": True,
        })
    return result


def _split_long_scenes(scenes: list[dict]) -> list[dict]:
    """MAX_SCENE_SEC 초과 장면을 TARGET_SCENE_SEC 단위로 분할."""
    result = []
    for sc in scenes:
        if sc["duration"] <= MAX_SCENE_SEC:
            result.append(sc)
            continue
        # 분할 필요
        n = max(2, round(sc["duration"] / TARGET_SCENE_SEC))
        step = sc["duration"] / n
        for i in range(n):
            s = sc["start_sec"] + step * i
            e = sc["start_sec"] + step * (i + 1) if i < n - 1 else sc["end_sec"]
            result.append({
                "scene_no": 0,
                "vocal_lines": sc["vocal_lines"] if i == 0 else [],
                "start_sec": round(s, 2),
                "end_sec": round(e, 2),
                "duration": round(e - s, 2),
            })
    return result
