"""전체 작품 Whisper 재추출 + Gemini 보정 비교 테스트. 기존 데이터 수정 안 함."""
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from backend.database import DB_PATH


async def test_all():
    import aiosqlite
    from faster_whisper import WhisperModel
    from backend.utils.file_manager import lyrics_path, music_path
    from backend.services.pipeline_service import _correct_lyrics_with_gemini

    model = WhisperModel("large-v3", device="cpu", compute_type="int8")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT id, title, theme FROM projects WHERE status='done' ORDER BY created_at"
        )

    _strip = lambda w: re.sub(r'[.,!?;:~…\"\'\-\[\]]+', '', w).strip()

    print(f"=== {len(rows)}개 작품 전수 테스트 ===\n")

    for i, row in enumerate(rows, 1):
        pid = row["id"]
        title = row["title"]
        mp3 = music_path(pid)
        if not mp3.exists():
            print(f"[{i}/{len(rows)}] {title} - 음원 없음\n")
            continue
        lp = Path(lyrics_path(pid))
        if not lp.exists():
            print(f"[{i}/{len(rows)}] {title} - lyrics.json 없음\n")
            continue

        data = json.loads(lp.read_text(encoding="utf-8"))
        original_lyrics = data.get("lyrics", "").replace("[End]", "").strip()
        scene_count = len(data.get("scenes", [])) or 6
        lyrics_words_set = set(_strip(w).lower() for w in original_lyrics.split() if _strip(w))

        # Whisper (fallback)
        raw_segs = []
        used_fb = False
        for cond in [False, True]:
            segs, _ = model.transcribe(
                str(mp3), language="ko", word_timestamps=True,
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
                        for w in seg.words if w.word.strip() and (w.end - w.start) >= 0.1
                    ]
                raw_segs.append(entry)
            if raw_segs:
                if cond:
                    used_fb = True
                break

        # 5sec segments
        all_words = [w for s in raw_segs for w in s.get("words", [])]
        timed = []
        for si in range(scene_count):
            s, e = si * 5.0, min((si + 1) * 5.0, 30.0)
            wi = [w for w in all_words if s <= w["start"] < e]
            timed.append({"text": " ".join(w["text"] for w in wi), "start": s, "end": e,
                          "words": wi, "has_vocal": len(wi) > 0})

        # 마지막 세그먼트 원문 대조 환각 필터
        halluc_removed = ""
        for sg in reversed(timed):
            if sg["has_vocal"]:
                seg_words = [w["text"].lower() for w in sg.get("words", [])]
                if seg_words:
                    overlap = sum(1 for w in seg_words if w in lyrics_words_set)
                    ratio = overlap / len(seg_words)
                    if ratio < 0.5:
                        halluc_removed = f" [환각제거: '{sg['text'][:20]}' 겹침{ratio:.0%}]"
                        sg["text"] = ""
                        sg["words"] = []
                        sg["has_vocal"] = False
                break

        # Gemini
        original_words = [w for sg in timed for w in sg.get("words", [])]
        raw_lyrics = [sg["text"] for sg in timed if sg["text"].strip() and sg.get("words")]
        whisper_text = " ".join(w["text"] for w in original_words)

        corrected_raw = await _correct_lyrics_with_gemini(raw_lyrics, original_lyrics) if raw_lyrics else []
        corrected = [_strip(w) for w in corrected_raw if _strip(w)]
        corrected_text = " ".join(corrected)

        # 단어 수 트림 체크
        trim_info = ""
        if len(original_words) > 0 and len(corrected) > len(original_words) * 1.3:
            trim_info = f" [트림필요: {len(corrected)}>{len(original_words)}*1.3]"

        fb = " (fb)" if used_fb else ""
        print(f"[{i}/{len(rows)}] {title}{fb}{halluc_removed}{trim_info}")
        print(f"  원문: {original_lyrics[:70]}")
        print(f"  Whisper ({len(original_words)}w): {whisper_text[:70]}")
        print(f"  보정후 ({len(corrected)}w): {corrected_text[:70]}")
        print()

    print("=== 완료 ===")


if __name__ == "__main__":
    asyncio.run(test_all())
