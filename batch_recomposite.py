"""전체 작품: Whisper 재추출(원본 mp3) + Gemini 보정 + vocal_lines 동기화 + Step 5 합성.
기존 음원/클립/이미지 안 건드림. lyrics.json + final.mp4만 갱신."""
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from backend.database import DB_PATH

# 환각 필터 (lyrics_sync_service.py와 동일)
async def run_all():
    import aiosqlite
    from faster_whisper import WhisperModel
    from backend.utils.file_manager import (
        lyrics_path, music_path, clip_path, lipsync_clip_path,
    )
    from backend.services.pipeline_service import _correct_lyrics_with_gemini, _apply_corrected_words
    from backend.services.ffmpeg_service import render_video

    model = WhisperModel("large-v3", device="cpu", compute_type="int8")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT id, title, theme FROM projects WHERE status='done' ORDER BY created_at"
        )

    print(f"=== {len(rows)}개 작품 처리 시작 ===\n")

    for i, row in enumerate(rows, 1):
        pid = row["id"]
        title = row["title"]
        lp = Path(lyrics_path(pid))

        if not lp.exists():
            print(f"[{i}/{len(rows)}] {title} - lyrics.json 없음, 스킵\n")
            continue

        data = json.loads(lp.read_text(encoding="utf-8"))
        scenes = data.get("scenes", [])
        lyrics_text = data.get("lyrics", "").replace("[End]", "").strip()
        scene_count = len(scenes)

        audio_file = str(music_path(pid))
        if not Path(audio_file).exists():
            print(f"[{i}/{len(rows)}] {title} - 음원 없음, 스킵\n")
            continue

        # 클립 파일 확인
        clip_files = []
        for sc in scenes:
            sno = sc["scene_no"]
            lp_clip = Path(lipsync_clip_path(pid, sno))
            cp = Path(clip_path(pid, sno))
            if lp_clip.exists():
                clip_files.append(str(lp_clip))
            elif cp.exists():
                clip_files.append(str(cp))
            else:
                clip_files.append(None)
        if any(f is None for f in clip_files):
            missing = [j+1 for j, f in enumerate(clip_files) if f is None]
            print(f"[{i}/{len(rows)}] {title} - 클립 누락 {missing}, 스킵\n")
            continue

        # ── 1) Whisper 재추출 (원본 mp3, demucs 없음) ──
        raw_segs = []
        used_fallback = False
        for cond in [False, True]:
            segs, _ = model.transcribe(
                audio_file, language="ko", word_timestamps=True,
                vad_filter=False, condition_on_previous_text=cond)
            raw_segs = []
            for seg in segs:
                if seg.compression_ratio > 2.4:
                    continue
                text = seg.text.strip()
                entry = {"text": text, "start": seg.start, "end": seg.end}
                if seg.words:
                    entry["words"] = [
                        {"text": w.word.strip(), "start": w.start, "end": w.end}
                        for w in seg.words
                        if w.word.strip() and (w.end - w.start) >= 0.1
                    ]
                raw_segs.append(entry)
            if raw_segs:
                if cond:
                    used_fallback = True
                break

        # 단어 수집 + 5초 세그먼트 배분
        all_words = [w for s in raw_segs for w in s.get("words", [])]
        timed_lines = []
        for si in range(scene_count):
            s, e = si * 5.0, min((si + 1) * 5.0, 30.0)
            words_in = [w for w in all_words if s <= w["start"] < e]
            text = " ".join(w["text"] for w in words_in).strip()
            timed_lines.append({
                "text": text, "start": s, "end": e,
                "words": words_in, "has_vocal": len(words_in) > 0,
            })

        # 마지막 보컬 세그먼트 환각 필터: 원문 가사와 단어 겹침 체크
        lyrics_words_set = set(re.sub(r'[.,!?;:~…\"\'\-\[\]]+', '', lyrics_text).lower().split())
        for sg in reversed(timed_lines):
            if sg["has_vocal"]:
                seg_words = [w["text"].lower() for w in sg.get("words", [])]
                if seg_words:
                    overlap = sum(1 for w in seg_words if w in lyrics_words_set)
                    ratio = overlap / len(seg_words)
                    if ratio == 0:
                        print(f"  환각 제거: '{sg['text'][:20]}' (원문 겹침 {ratio:.0%})")
                        sg["text"] = ""
                        sg["words"] = []
                        sg["has_vocal"] = False
                break

        fb = " (fallback)" if used_fallback else ""
        vocal_count = sum(1 for sg in timed_lines if sg["has_vocal"])
        print(f"[{i}/{len(rows)}] {title}{fb} - Whisper {len(all_words)}단어, {vocal_count}보컬세그")

        # ── 2) Gemini 보정 ──
        raw_lyrics = [sg["text"] for sg in timed_lines if sg["text"].strip() and sg.get("words")]
        if raw_lyrics:
            try:
                corrected_words_raw = await _correct_lyrics_with_gemini(raw_lyrics, lyrics_text)
                _strip_punct = lambda w: re.sub(r'[.,!?;:~…\"\'\-]+', '', w).strip()
                corrected_words = [_strip_punct(w) for w in corrected_words_raw if _strip_punct(w)]
                original_words = []
                for sg in timed_lines:
                    if sg.get("words"):
                        original_words.extend(sg["words"])
                # SequenceMatcher로 원본↔보정 정렬, 타이밍 보존
                _apply_corrected_words(timed_lines, original_words, corrected_words)
                # 세그먼트 text를 words에서 재구성
                for sg in timed_lines:
                    words = sg.get("words", [])
                    sg["text"] = " ".join(w["text"] for w in words) if words else ""
                    sg["has_vocal"] = len(words) > 0
                print(f"  보정 완료 ({len(corrected_words)}단어)")
            except Exception as e:
                print(f"  보정 실패: {e}")

        # ── 3) lyrics.json 갱신 ──
        data["whisper_lyrics"] = timed_lines
        for si, sc in enumerate(scenes):
            if si < len(timed_lines) and timed_lines[si]["text"].strip():
                sc["vocal_lines"] = [timed_lines[si]["text"]]
            elif si < len(timed_lines):
                sc["vocal_lines"] = []
        lp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        # ── 4) Step 5 영상합성 ──
        scenes_dicts = [
            {"scene_no": sc["scene_no"],
             "start_sec": sc.get("start_sec", 0),
             "duration": sc.get("duration", 5),
             "vocal_lines": sc.get("vocal_lines", [])}
            for sc in scenes
        ]
        try:
            final_video = await render_video(
                pid, clip_files, audio_file,
                scenes=scenes_dicts, whisper_lyrics=timed_lines,
                title=title, theme=row["theme"],
            )
            print(f"  합성 완료: {final_video}")
        except Exception as e:
            print(f"  합성 실패: {e}")
        print()

    print("=== 완료 ===")


if __name__ == "__main__":
    asyncio.run(run_all())
