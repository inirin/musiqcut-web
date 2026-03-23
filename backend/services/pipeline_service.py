import asyncio
import json
import math
import sys
import uuid
import aiosqlite
from datetime import datetime
from pathlib import Path
from backend.config import settings
from backend.database import DB_PATH
from backend.utils.progress import ProgressEmitter
from backend.utils.file_manager import (
    lyrics_path, music_path, image_path, clip_path, video_path
)
from backend.services.claude_service import generate_story, generate_scenes
from backend.services.gemini_image_service import generate_images
from backend.services.suno_service import generate_music, measure_audio_duration
from backend.services.wan_video_service import generate_video_clips as wan_generate_clips
from backend.services.wan_video_service import get_clip_duration
from backend.services.echomimic_service import is_available as echo_available
from backend.services.echomimic_service import generate_lipsync_clip
from backend.services.lipsync_precheck import separate_vocals
from backend.services.creatomate_service import render_video
from backend.services.lyrics_sync_service import extract_lyrics_timestamps
from backend.models.project import GeneratedScript, ScriptScene


def _free_comfyui_vram(tag: str = ""):
    """ComfyUI 모델 언로드 + VRAM 해제 — 모델 전환 시점에만 호출."""
    try:
        import urllib.request as _ur
        _req = _ur.Request(
            "http://127.0.0.1:8189/free",
            data=json.dumps({"unload_models": True, "free_memory": True}).encode(),
            headers={"Content-Type": "application/json"})
        _ur.urlopen(_req)
        print(f"[STEP4] VRAM 해제 완료 ({tag})", file=sys.stderr)
    except Exception:
        pass


async def _update_project(project_id: str, **kwargs):
    async with aiosqlite.connect(DB_PATH) as db:
        sets = ", ".join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values()) + [project_id]
        await db.execute(
            f"UPDATE projects SET {sets}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            vals
        )
        await db.commit()


async def _log_step(project_id: str, step_no: int, step_name: str,
                    status: str, output_data: dict = None,
                    error_msg: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        now = datetime.utcnow().isoformat()
        if status == "running":
            # 시작: started_at 기록, finished_at은 비움
            await db.execute(
                """INSERT OR REPLACE INTO pipeline_steps
                   (project_id, step_no, step_name, status, started_at,
                    finished_at, output_data, error_msg)
                   VALUES (?,?,?,?,?,NULL,?,?)""",
                (project_id, step_no, step_name, status, now,
                 json.dumps(output_data or {}, ensure_ascii=False),
                 error_msg)
            )
        else:
            # 완료/실패: finished_at 기록, started_at 유지
            await db.execute(
                """UPDATE pipeline_steps SET status=?, finished_at=?,
                   output_data=?, error_msg=?
                   WHERE project_id=? AND step_no=?""",
                (status, now,
                 json.dumps(output_data or {}, ensure_ascii=False),
                 error_msg, project_id, step_no)
            )
        await db.commit()


async def _update_step_progress(project_id: str, step_no: int,
                                step_name: str, current: int, total: int):
    """running 중인 스텝의 진행률(current/total)을 DB에 업데이트."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE pipeline_steps SET output_data=?
               WHERE project_id=? AND step_no=? AND status='running'""",
            (json.dumps({"current": current, "total": total}, ensure_ascii=False),
             project_id, step_no)
        )
        await db.commit()


async def _get_completed_steps(project_id: str) -> dict:
    """DB에서 완료된 STEP 번호와 output_data를 반환."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT step_no, output_data FROM pipeline_steps "
            "WHERE project_id=? AND status='done' ORDER BY step_no",
            (project_id,)
        )
        rows = await cursor.fetchall()
        return {r["step_no"]: json.loads(r["output_data"] or "{}") for r in rows}


async def _merge_audio_to_clip(clip_path: str, audio_path: str,
                               start_sec: float, duration: float):
    """클립 영상에 오디오 구간을 합성 (in-place)."""
    import subprocess, tempfile
    tmp = tempfile.NamedTemporaryFile(suffix='.mp4', delete=False)
    tmp.close()
    cmd = [
        'ffmpeg', '-y',
        '-i', clip_path,
        '-ss', f'{start_sec:.2f}', '-t', f'{duration:.2f}', '-i', audio_path,
        '-c:v', 'copy', '-c:a', 'aac', '-b:a', '128k',
        '-map', '0:v:0', '-map', '1:a:0',
        '-shortest', '-movflags', '+faststart',
        tmp.name,
    ]
    result = await asyncio.to_thread(
        subprocess.run, cmd, capture_output=True, text=True)
    if result.returncode == 0:
        Path(tmp.name).replace(clip_path)
    else:
        Path(tmp.name).unlink(missing_ok=True)
        raise RuntimeError(result.stderr[-200:])


async def _correct_lyrics_with_gemini(raw_lyrics: list[str], story_text: str) -> list[str]:
    """Gemini Flash로 Whisper 가사 오타 보정."""
    from backend.utils.gemini_client import gemini_generate

    prompt = f"""AI 음성 인식(Whisper)으로 추출한 한국어 노래 가사를 스토리/컨셉 원문과 비교하여 교정하세요.

규칙:
- 스토리/컨셉에 나오는 단어와 발음이 비슷한 Whisper 오인식을 적극 교정 (예: "곤 엮고" → "곤룡포", "벌" → "궁궐")
- 스토리/컨셉에 등장하는 고유명사, 키워드를 우선 매칭하세요
- 없는 내용을 새로 만들거나, 있는 단어를 삭제하지 마세요
- 줄 수를 반드시 {len(raw_lyrics)}줄로 유지하세요
- 다른 설명 없이 보정된 가사만 줄바꿈으로 출력하세요

[스토리/컨셉 원문]
{story_text[:500]}

[Whisper 추출 가사 ({len(raw_lyrics)}줄)]
{chr(10).join(raw_lyrics)}"""

    resp = await gemini_generate(
        model="gemini-2.5-flash",
        contents=prompt)
    lines = [l.strip() for l in resp.text.strip().split('\n') if l.strip()]
    # 줄 수가 다르면 Gemini가 지어낸 것 → 원본 유지
    if len(lines) != len(raw_lyrics):
        print(f"[LyricsSync] Gemini 보정 줄 수 불일치 ({len(lines)} vs {len(raw_lyrics)}), 원본 유지",
              file=sys.stderr)
        return raw_lyrics
    return lines


async def _clean_step_files(project_id: str, from_step: int):
    """from_step 이후 스텝의 캐시 파일 삭제 — 이전 스텝이 재생성되면 후속 스텝도 새로 만들도록."""
    import shutil
    pdir = Path(lyrics_path(project_id)).parent

    if from_step <= 2:
        mp = music_path(project_id)
        if mp.exists():
            mp.unlink()
        # demucs 캐시 삭제 (새 음원이면 보컬 분리도 새로)
        demucs_dir = pdir / "demucs"
        if demucs_dir.exists():
            shutil.rmtree(demucs_dir, ignore_errors=True)
    # lyrics.json에서 scenes 데이터 초기화 (과거 vocal_lines 제거)
    if from_step <= 3:
        lp = pdir / "lyrics.json"
        if lp.exists():
            try:
                import json
                data = json.loads(lp.read_text(encoding="utf-8"))
                changed = False
                keys_to_del = ["scenes"]
                if from_step <= 2:
                    keys_to_del.append("whisper_lyrics")
                for key in keys_to_del:
                    if key in data:
                        del data[key]
                        changed = True
                if changed:
                    lp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
            except Exception:
                pass
    if from_step <= 3:
        imgs_dir = pdir / "images"
        if imgs_dir.exists():
            shutil.rmtree(imgs_dir, ignore_errors=True)
            imgs_dir.mkdir(parents=True, exist_ok=True)
    if from_step <= 4:
        clips_dir = pdir / "clips"
        if clips_dir.exists():
            shutil.rmtree(clips_dir, ignore_errors=True)
            clips_dir.mkdir(parents=True, exist_ok=True)
    if from_step <= 5:
        video_dir = pdir / "video"
        if video_dir.exists():
            shutil.rmtree(video_dir, ignore_errors=True)
        concat_file = pdir / "concat.txt"
        if concat_file.exists():
            concat_file.unlink()


def _count_files(directory: Path, pattern: str) -> int:
    """디렉토리에서 패턴에 맞는 파일 수를 센다."""
    if not directory.exists():
        return 0
    return len(list(directory.glob(pattern)))


async def run_pipeline(
    project_id: str,
    theme: str,
    mood: str,
    emitter: ProgressEmitter,
    resume_from: int = 0,
    length: str = "short",
):
    current_step = 0
    try:
        await _update_project(project_id, status="running")

        # 재시도 시 완료된 STEP 확인 — resume_from 이전 스텝만 캐시 사용
        if resume_from > 0:
            all_completed = await _get_completed_steps(project_id)
            completed = {k: v for k, v in all_completed.items() if k < resume_from}
            await _clean_step_files(project_id, resume_from)
        else:
            completed = {}

        # ── STEP 1: 스토리/컨셉 생성 ──────────────────────────
        current_step = 1
        lp = lyrics_path(project_id)
        if 1 in completed and lp.exists():
            story_data = json.loads(lp.read_text(encoding="utf-8"))
            await emitter.update(1, "done",
                f"스토리 완성: '{story_data['title']}'",
                {"title": story_data["title"], "lyrics": story_data["lyrics"]})
        else:
            await emitter.update(1, "running", "Gemini가 스토리와 작곡 지시를 구성하는 중...")
            await _log_step(project_id, 1, "스토리 생성", "running")
            story_data = await generate_story(theme, mood, length=length)
            lp.write_text(
                json.dumps(story_data, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            await _update_project(project_id, title=story_data["title"])
            await _log_step(project_id, 1, "스토리 생성", "done",
                            {"title": story_data["title"]})
            await emitter.update(1, "done",
                f"스토리 완성: '{story_data['title']}'",
                {"title": story_data["title"], "lyrics": story_data["lyrics"],
                 "art_style": story_data.get("art_style", "")})

        title = story_data["title"]
        lyrics = story_data["lyrics"]
        music_prompt = story_data["music_prompt"]
        characters = story_data.get("characters", [])
        art_style = story_data.get("art_style", "Pixar-style 3D animation")

        # ── STEP 2: 음악 생성 → 곡 길이 측정 → scene_count 결정 ──
        current_step = 2
        audio = music_path(project_id)
        if 2 in completed and audio.exists():
            audio_file = str(audio)
            actual_duration = await measure_audio_duration(audio_file)
            scene_count = max(3, math.ceil(actual_duration / get_clip_duration()))
            await emitter.update(2, "running",
                f"음악 로드 완료, 가사 분석 중...",
                {"audio_url": f"/storage/projects/{project_id}/music/output.mp3"})
        else:
            await emitter.update(2, "running",
                "Suno AI가 음악을 생성하는 중... ")
            await _log_step(project_id, 2, "음악 생성", "running")
            audio_file, actual_duration = await generate_music(
                project_id, music_prompt, lyrics, length=length
            )
            scene_count = max(3, math.ceil(actual_duration / get_clip_duration()))
            await emitter.update(2, "running",
                f"음악 생성 완료! 가사 분석 준비 중...",
                {"audio_url": f"/storage/projects/{project_id}/music/output.mp3"})

        duration = int(scene_count * 8)

        # ── 가사 타임스탬프 추출 (Whisper) — 캐시 있으면 스킵 ──
        demucs_dir = str(Path(lyrics_path(project_id)).parent / "demucs")
        timed_lines = None
        _cached_lyrics = json.loads(lp.read_text(encoding="utf-8")).get("whisper_lyrics")
        if _cached_lyrics and len(_cached_lyrics) > 0 and resume_from >= 3:
            # 이미 분석된 결과 재사용 (구 문자열 형식 + 신 dict 형식 모두 지원)
            print("[LyricsSync] 캐시된 가사 사용 (Whisper 스킵)", file=sys.stderr)
            is_legacy = any(isinstance(t, str) for t in _cached_lyrics)
            if is_legacy:
                # 구 형식: 보컬만 저장 → 곡 길이 기준으로 전체 세그먼트 재구성
                print("[LyricsSync] 구 형식 캐시 → Whisper 재분석 필요", file=sys.stderr)
                timed_lines = None  # 강제 재분석
            else:
                # 신 형식: instrumental 포함 전체
                timed_lines = []
                for i, t in enumerate(_cached_lyrics):
                    timed_lines.append({"text": t.get("text", ""),
                                        "start": t.get("start", i * 5.0),
                                        "end": t.get("end", (i+1) * 5.0),
                                        "has_vocal": t.get("has_vocal", False),
                                        "words": []})
                scene_count = max(scene_count, len(timed_lines))
        try:
            if not timed_lines:
                if resume_from <= 2:
                    await emitter.update(2, "running",
                        f"가사 싱크 분석 중... (Whisper large-v3)",
                        {"audio_url": f"/storage/projects/{project_id}/music/output.mp3"})
                else:
                    print("[LyricsSync] 가사 캐시 없음, Whisper 재분석 (STEP 2 UI 유지)",
                          file=sys.stderr)
                timed_lines = await extract_lyrics_timestamps(
                    audio_file, lyrics, demucs_dir,
                    total_duration=actual_duration)

            # Gemini Flash로 가사 오타 보정 (캐시 아닐 때만)
            if not _cached_lyrics:
                if resume_from <= 2:
                    await emitter.update(2, "running", "가사 보정 중... (Gemini Flash)")
                raw_lyrics = [sg["text"] for sg in timed_lines if sg["text"].strip()]
                try:
                    corrected = await _correct_lyrics_with_gemini(
                        raw_lyrics, lyrics)
                    ci = 0
                    for sg in timed_lines:
                        if sg["text"].strip() and ci < len(corrected):
                            sg["text"] = corrected[ci]
                            ci += 1
                    print(f"[LyricsSync] Gemini 가사 보정 완료 ({ci}줄)", file=sys.stderr)
                except Exception as e:
                    print(f"[LyricsSync] Gemini 보정 실패 (원본 사용): {e}", file=sys.stderr)

            # 곡 길이 기준 scene_count가 timed_lines보다 크면 유지
            # (구 형식 캐시에서 보컬만 복원된 경우 대비)
            scene_count = max(scene_count, len(timed_lines))
            for sg in timed_lines:
                label = sg["text"][:30] if sg["text"] else "(instrumental)"
                vocal = "♪" if sg["has_vocal"] else " "
                print(f"  {vocal} [{sg['start']:.1f}~{sg['end']:.1f}초] {label}",
                      file=sys.stderr)
        except Exception as e:
            print(f"[LyricsSync] 가사 싱크 실패: {e}", file=sys.stderr)
            timed_lines = None

        # Whisper 가사를 즉시 lyrics.json에 저장 (STEP 3 전에)
        # instrumental 포함 전체 세그먼트 저장 (resume 시 scene_count 동기화)
        early_lyrics = []
        if timed_lines:
            early_lyrics = [{"text": sg["text"], "start": sg["start"],
                             "end": sg["end"], "has_vocal": sg.get("has_vocal", False)}
                            for sg in timed_lines]
            script_data_tmp = json.loads(lp.read_text(encoding="utf-8"))
            script_data_tmp["whisper_lyrics"] = early_lyrics
            lp.write_text(json.dumps(script_data_tmp, ensure_ascii=False, indent=2),
                          encoding="utf-8")

        # STEP 2 완료 (음악 생성 + 가사 분석 모두 끝남) — DB + 소켓 동시
        await _log_step(project_id, 2, "음악 생성", "done",
            {"audio_path": audio_file, "actual_duration": round(actual_duration, 1)})
        await emitter.update(2, "done",
            f"음악 + 가사 분석 완료! ({actual_duration:.0f}초, {scene_count}장면)",
            {"audio_url": f"/storage/projects/{project_id}/music/output.mp3",
             "whisper_lyrics": early_lyrics})

        # ── STEP 3: 장면 구성 + 이미지 생성 ──────────────────────
        current_step = 3
        # lyrics.json에 scenes가 있으면 장면 구성 캐시 사용
        script_data = json.loads(lp.read_text(encoding="utf-8"))
        img1 = image_path(project_id, 1)

        if 3 in completed and img1.exists():
            imgs_dir = image_path(project_id, 1).parent
            img_count = _count_files(imgs_dir, "scene_*.png")
            scenes = [ScriptScene(**s) for s in script_data.get("scenes", [])]
            if not scenes:
                scenes = [ScriptScene(scene_no=i+1, description="", image_prompt="")
                          for i in range(img_count)]
            image_files = [str(image_path(project_id, i + 1))
                          for i in range(img_count)]
            await emitter.update(3, "done", f"이미지 {img_count}장",
                {"image_urls": [
                    f"/storage/projects/{project_id}/images/scene_{i+1:02d}.png"
                    for i in range(img_count)]})
        else:
            # 3-1: 장면 구성 (Gemini) — 가사 싱크 타이밍 전달
            await emitter.update(3, "running",
                f"Gemini가 {scene_count}장면 구성 중...")
            await _log_step(project_id, 3, "장면 구성 + 이미지", "running")
            scenes = await generate_scenes(
                title, lyrics, mood, scene_count, duration,
                scene_timing=timed_lines,
                characters=characters,
                art_style=art_style)

            # 5초 세그먼트를 장면에 1:1 매칭
            if timed_lines:
                for i, sc in enumerate(scenes):
                    if i < len(timed_lines):
                        sg = timed_lines[i]
                        sc.start_sec = sg["start"]
                        sc.end_sec = sg["end"]
                        sc.duration = round(sg["end"] - sg["start"], 2)
                        sc.vocal_lines = [sg["text"]] if sg["text"] else []

            # _has_vocal 플래그를 scenes에 기록 (STEP 3/4 보컬 태그 통일)
            if timed_lines:
                for i, sc in enumerate(scenes):
                    if i < len(timed_lines):
                        sd = sc.dict() if hasattr(sc, 'dict') else vars(sc)
                        sd['_has_vocal'] = timed_lines[i].get("has_vocal", False)

            # whisper_lyrics 저장 + 프론트 전송
            # instrumental 포함 전체 세그먼트 저장 (resume 시 scene_count 동기화)
            whisper_lyrics = []
            for i, sc in enumerate(scenes):
                entry = {"text": sc.vocal_lines[0] if sc.vocal_lines else "",
                         "start": sc.start_sec, "end": sc.end_sec,
                         "has_vocal": bool(sc.vocal_lines and sc.vocal_lines[0].strip())}
                if timed_lines and i < len(timed_lines):
                    entry["has_vocal"] = timed_lines[i].get("has_vocal", False)
                whisper_lyrics.append(entry)
            script_data["whisper_lyrics"] = whisper_lyrics
            scenes_data = []
            for i, sc in enumerate(scenes):
                sd = sc.dict() if hasattr(sc, 'dict') else dict(sc)
                if timed_lines and i < len(timed_lines):
                    sd['_has_vocal'] = timed_lines[i].get("has_vocal", False)
                scenes_data.append(sd)
            script_data["scenes"] = scenes_data
            lp.write_text(
                json.dumps(script_data, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            await emitter.update(2, "done",
                f"음악 생성 완료! ({actual_duration:.0f}초, 가사 {len(whisper_lyrics)}줄)",
                {"audio_url": f"/storage/projects/{project_id}/music/output.mp3",
                 "whisper_lyrics": whisper_lyrics})

            # 3-2: 이미지 생성
            await emitter.update(3, "running",
                f"Imagen으로 {scene_count}개 이미지 생성 중...")
            async def _step3_progress(current, total):
                urls = [f"/storage/projects/{project_id}/images/scene_{i+1:02d}.png"
                        for i in range(current)]
                await emitter.update(3, "running",
                    f"이미지 생성 중... {current}/{total}",
                    {"current": current, "total": total, "image_urls": urls})
                await _update_step_progress(project_id, 3,
                    "장면 구성 + 이미지", current, total)

            image_files = await generate_images(
                project_id, scenes, progress_cb=_step3_progress)
            await _log_step(project_id, 3, "장면 구성 + 이미지", "done",
                            {"image_count": len(image_files)})
            await emitter.update(3, "done",
                f"이미지 {len(image_files)}장 생성 완료!",
                {"image_urls": [
                    f"/storage/projects/{project_id}/images/scene_{i+1:02d}.png"
                    for i in range(len(image_files))]})

        # ── STEP 4: 이미지→영상 클립 (클로즈업=EchoMimicV3, 나머지=Wan I2V) ──
        current_step = 4
        use_echo = echo_available()
        clip1 = clip_path(project_id, 1)
        if 4 in completed and clip1.exists():
            clips_dir = clip_path(project_id, 1).parent
            clip_count = _count_files(clips_dir, "clip_*.mp4")
            clip_files = [str(clip_path(project_id, i + 1)) for i in range(clip_count)]
            await emitter.update(4, "done", f"영상 클립 {clip_count}개",
                                 {"clip_urls": [
                                     f"/storage/projects/{project_id}/clips/clip_{i+1:02d}.mp4"
                                     for i in range(clip_count)
                                 ]})
        else:
            # STEP 4 시작 전 VRAM 정리 (이전 실행 잔여 모델 해제)
            await asyncio.to_thread(_free_comfyui_vram, "STEP4 시작 전 초기화")

            # 보컬 분리 (EchoMimic + Whisper 판단용)
            demucs_dir = str(Path(lyrics_path(project_id)).parent / "demucs")
            vocals_path = await separate_vocals(audio_file, demucs_dir)
            print(f"[STEP4] 보컬 분리 완료: {vocals_path}", file=sys.stderr)

            # lyrics.json에서 _has_vocal 로드 (STEP 2에서 Whisper 분석한 결과 재사용)
            _vocal_map = {}
            try:
                _scenes_data = json.loads(lp.read_text(encoding="utf-8")).get("scenes", [])
                for sd in _scenes_data:
                    if "_has_vocal" in sd:
                        _vocal_map[sd.get("scene_no", 0)] = sd["_has_vocal"]
            except Exception:
                pass

            if _vocal_map:
                print(f"[STEP4] STEP 2 보컬 분석 결과 재사용 ({sum(_vocal_map.values())}/{len(_vocal_map)} 보컬)",
                      file=sys.stderr)
            else:
                print("[STEP4] _has_vocal 캐시 없음, 전체 보컬 판정 스킵", file=sys.stderr)

            def _has_vocals(sc) -> bool:
                return _vocal_map.get(sc.scene_no, False)

            echo_indices = [i for i, s in enumerate(scenes)
                            if getattr(s, 'shot_type', '') in ('closeup', 'medium')
                            and _has_vocals(s)
                            and getattr(s, 'is_vocalist', True)]  # 보컬 주체만
            has_echo = use_echo and len(echo_indices) > 0

            engine_parts = []
            if has_echo:
                engine_parts.append(f"EchoMimicV3 립싱크 {len(echo_indices)}개 (closeup+medium)")
            non_closeup = len(scenes) - len(echo_indices) if has_echo else len(scenes)
            if non_closeup > 0:
                engine_parts.append(f"Wan 2.2 I2V {non_closeup}개")
            engine_desc = " + ".join(engine_parts)

            # 비보컬 클립 인덱스 (Wan이 먼저 처리됨)
            non_closeup_idx = [i for i in range(len(scenes)) if i not in echo_indices] if has_echo else list(range(len(scenes)))

            # 초기 클립 슬롯 — 실제 첫 처리 클립에 스피너
            init_slots = []
            first_idx = non_closeup_idx[0] if non_closeup_idx else (echo_indices[0] if echo_indices else 0)
            for i, sc in enumerate(scenes):
                sno = sc.scene_no
                init_slots.append({
                    "status": "running" if i == first_idx else "pending",
                    "image_url": f"/storage/projects/{project_id}/images/scene_{sno:02d}.png",
                    "start_sec": getattr(sc, 'start_sec', 0),
                    "end_sec": getattr(sc, 'end_sec', 0),
                    "duration": getattr(sc, 'duration', 0),
                    "vocal_lines": getattr(sc, 'vocal_lines', []),
                    "description": getattr(sc, 'description', ''),
                    "shot_type": getattr(sc, 'shot_type', 'medium'),
                    "_has_vocal": _has_vocals(sc),
                    "is_vocalist": getattr(sc, "is_vocalist", True),
                })
            await emitter.update(4, "running",
                                 f"{engine_desc} 영상 클립 생성 중...",
                                 {"current": 0, "total": len(scenes),
                                  "clip_slots": init_slots})
            await _log_step(project_id, 4, "영상 클립 생성", "running")

            clip_duration = get_clip_duration()
            clip_files = [None] * len(scenes)
            done_count = 0
            _current_clip_idx = -1  # 현재 제작중인 클립 인덱스

            async def _step4_progress_update():
                nonlocal done_count
                done_count = sum(1 for f in clip_files if f)
                # 전체 장면 슬롯: 완료된 건 clip URL, 미완료는 이미지 URL
                clip_slots = []
                for i, sc in enumerate(scenes):
                    sno = sc.scene_no
                    slot = {
                        "image_url": f"/storage/projects/{project_id}/images/scene_{sno:02d}.png",
                        "start_sec": getattr(sc, 'start_sec', 0),
                        "end_sec": getattr(sc, 'end_sec', 0),
                        "duration": getattr(sc, 'duration', 0),
                        "vocal_lines": getattr(sc, 'vocal_lines', []),
                        "description": getattr(sc, 'description', ''),
                        "shot_type": getattr(sc, 'shot_type', 'medium'),
                        "is_vocalist": getattr(sc, "is_vocalist", True),
                    }
                    if clip_files[i]:
                        slot["status"] = "done"
                        slot["url"] = f"/storage/projects/{project_id}/clips/clip_{sno:02d}.mp4"
                    elif i == _current_clip_idx:
                        slot["status"] = "running"
                    else:
                        slot["status"] = "pending"
                    clip_slots.append(slot)
                data = {"current": done_count, "total": len(scenes),
                        "clip_slots": clip_slots}
                await emitter.update(4, "running",
                    f"영상 클립 생성 중... {done_count}/{len(scenes)}", data)
                # DB에도 저장 (dashboard 폴링용)
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        """UPDATE pipeline_steps SET output_data=?
                           WHERE project_id=? AND step_no=? AND status='running'""",
                        (json.dumps(data, ensure_ascii=False), project_id, 4))
                    await db.commit()

            # 비클로즈업 장면 → Wan I2V
            non_closeup_scenes = [s for i, s in enumerate(scenes)
                                  if i not in echo_indices] if has_echo else scenes
            non_closeup_images = [f for i, f in enumerate(image_files)
                                  if i not in echo_indices] if has_echo else image_files


            if non_closeup_scenes:
                _wan_done_count = 0
                async def _wan_progress(current, total):
                    nonlocal _wan_done_count, _current_clip_idx
                    if current > _wan_done_count:
                        idx = non_closeup_idx[current - 1]
                        clip_files[idx] = str(clip_path(project_id,
                                              scenes[idx].scene_no))
                        _wan_done_count = current
                    # 다음 클립을 running으로 표시
                    if current < total:
                        _current_clip_idx = non_closeup_idx[current]
                    else:
                        _current_clip_idx = -1
                    await _step4_progress_update()

                wan_results = await wan_generate_clips(
                    project_id, non_closeup_scenes, non_closeup_images,
                    progress_cb=_wan_progress)
                for j, idx in enumerate(non_closeup_idx):
                    clip_files[idx] = wan_results[j]

            # 모델 전환: Wan → EchoMimic
            if has_echo and vocals_path:
                if non_closeup_scenes:
                    await asyncio.to_thread(_free_comfyui_vram, "Wan→EchoMimic")
                for idx in echo_indices:
                    _current_clip_idx = idx
                    await _step4_progress_update()  # 시작 시 스피너 표시
                    scene = scenes[idx]
                    start_sec = scene.start_sec if scene.start_sec > 0 else idx * clip_duration
                    scene_dur = scene.duration if scene.duration > 0 else clip_duration
                    try:
                        result = await generate_lipsync_clip(
                            project_id, scene.scene_no, vocals_path,
                            scene_start_sec=start_sec,
                            clip_duration=scene_dur,
                            prompt=scene.description)
                        clip_files[idx] = result
                    except Exception as e:
                        print(f"[STEP4] EchoMimic 실패 (장면 {scene.scene_no}), "
                              f"Wan I2V 폴백: {e}", file=sys.stderr)
                        await asyncio.to_thread(_free_comfyui_vram, "EchoMimic→Wan 폴백")
                        fallback = await wan_generate_clips(
                            project_id, [scene], [image_files[idx]])
                        clip_files[idx] = fallback[0]
                    await _step4_progress_update()

            clip_files = [f for f in clip_files if f]  # None 제거

            # 최종 clip_slots (뱃지 표시용)
            _current_clip_idx = -1
            final_slots = []
            for i, sc in enumerate(scenes):
                sno = sc.scene_no
                final_slots.append({
                    "status": "done",
                    "url": f"/storage/projects/{project_id}/clips/clip_{sno:02d}.mp4",
                    "image_url": f"/storage/projects/{project_id}/images/scene_{sno:02d}.png",
                    "start_sec": getattr(sc, 'start_sec', 0),
                    "end_sec": getattr(sc, 'end_sec', 0),
                    "duration": getattr(sc, 'duration', 0),
                    "vocal_lines": getattr(sc, 'vocal_lines', []),
                    "description": getattr(sc, 'description', ''),
                    "shot_type": getattr(sc, 'shot_type', 'medium'),
                    "is_vocalist": getattr(sc, "is_vocalist", True),
                })
            await _log_step(project_id, 4, "영상 클립 생성", "done",
                            {"clip_count": len(clip_files), "clip_slots": final_slots})
            await emitter.update(4, "done",
                                 f"영상 클립 {len(clip_files)}개 생성 완료!",
                                 {"clip_slots": final_slots})

        # ── STEP 5: 최종 영상 합성 (FFmpeg) ──────────────────────
        current_step = 5
        await emitter.update(5, "running", "최종 영상 합성 중...")
        await _log_step(project_id, 5, "영상 합성", "running")

        final_video = await render_video(
            project_id, clip_files, audio_file
        )
        await _log_step(project_id, 5, "영상 합성", "done",
                        {"video_path": final_video})
        await _update_project(project_id, status="done", video_path=final_video)

        await emitter.update(5, "done", "영상 합성 완료!")
        await emitter.complete(final_video)

    except Exception as e:
        await _update_project(project_id, status="failed", error_msg=str(e))
        step_names = {
            1: "스토리 생성", 2: "음악 생성", 3: "장면 구성 + 이미지",
            4: "영상 클립 생성", 5: "영상 합성"
        }
        await _log_step(
            project_id, current_step,
            step_names.get(current_step, f"STEP {current_step}"),
            "failed", error_msg=str(e)
        )
        await emitter.error(current_step, str(e))
        raise
