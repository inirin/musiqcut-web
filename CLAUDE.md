# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview
MusiqCut — 테마/분위기만 입력하면 30초 숏폼 AI 뮤직비디오를 자동 생성하는 파이프라인.

## Commands

```bash
# 서버 시작/재시작 (서비스 코드 수정 후 반드시 실행)
pm2 restart musical-pipeline

# ComfyUI 재시작 (모델 추가/변경 후)
pm2 restart comfyui

# 전체 시작
pm2 start ecosystem.config.js

# 로그 확인
pm2 logs musical-pipeline
pm2 logs comfyui

# 패키지 설치
venv/Scripts/pip.exe install <package>

# 서버 직접 실행 (디버깅용)
venv/Scripts/uvicorn.exe backend.main:app --host 0.0.0.0 --port 8000 --reload

# 자동 생성 스크립트 (2시간마다 랜덤 테마로 생성)
venv/Scripts/python.exe auto_generate.py
```

## Architecture

**FastAPI backend** (port 8000) + **static frontend** + **ComfyUI** (port 8189) for GPU inference.

### Pipeline Flow (5 Steps, Sequential)
1. **Script Generation** — Gemini 2.5 Flash → 제목, 가사, 음악 프롬프트, 아트 스타일, 캐릭터, 해시태그 (`gemini_script_service.py`)
2. **Music Generation** — Suno AI → MP3 → faster-whisper 전사(원본 mp3 직접, demucs 미사용) + whisperx forced alignment(wav2vec2) 단어별 정밀 타이밍 → Gemini Flash 가사 보정 (전체 단어 단위, SequenceMatcher 타이밍 보존) (`suno_service.py`, `lyrics_sync_service.py`)
3. **Image Generation** — Imagen 4 via Gemini API → 장면별 576x1024 PNG (`gemini_image_service.py`)
4. **Video Clip Generation** — 장면별 자동 분기:
   - 보컬+vocalist → **Wan 2.2 S2V** 립싱크 (`wan_s2v_service.py`)
   - 비보컬/비주체 → **Wan 2.2 I2V** (`wan_video_service.py`)
   - 모두 ComfyUI API (localhost:8189)로 워크플로우 전송
   - 실행 중 클립 재생성 요청 시 abort 없이 파일 삭제 → 루프에서 자동 재생성 (누락 클립 체크)
5. **Compositing** — FFmpeg로 클립 결합 + 노래방 스타일 2줄 교대 자막(ASS) + 오디오 합성 (`ffmpeg_service.py`)

### Key Architectural Patterns
- **Pipeline orchestration**: `pipeline_service.py`가 전체 5단계를 순차 실행, `resume_from` 파라미터로 특정 단계부터 재시작 가능
- **Concurrency lock**: 파이프라인은 한 번에 하나만 실행 (`_pipeline_lock` in `routers/pipeline.py`)
- **Progress reporting**: `ProgressEmitter` → WebSocket으로 실시간 진행상황 전송
- **VRAM management**: GPU 작업 간 `_free_comfyui_vram()` 호출하여 VRAM 해제 (I2V → S2V 전환 시)
- **ComfyUI queue management**: Step 4 시작 시 `_clear_comfyui_queue()`로 이전 워크플로우 제거 (서버 재시작 시 고아 작업 방지)
- **S2V webp 재활용**: 서버 재시작으로 mp4 변환 전에 죽은 경우, 기존 ComfyUI 출력 webp를 변환만 수행
- **Auto-generation scheduler**: 설정 간격마다 트렌드 기반 자동 생성, 서버 재시작 시 중단 작품 자동 resume, 비정상 종료 시 자동 복구, 중복 테마 30개 체크 (mood 포함), sleep 60초 분할
- **Upload system**: YouTube/Instagram/TikTok 연동, source='auto' 작품만 자동 업로드, 해시태그 자동 포함, 플랫폼별 제목 포맷 (YouTube: 하이픈, Instagram/TikTok: 줄바꿈)
- **Database**: SQLite (`pipeline.db`) via aiosqlite, 스키마는 `database.py`에서 앱 시작 시 자동 생성
- **Config**: pydantic-settings로 `.env` 로드 (`backend/config.py`)

### Lyrics & Subtitle System
- **Whisper 전사**: 원본 mp3 직접 사용 (demucs 미사용), condition_on_previous_text fallback (False→True)
- **환각 방지 2중 방어**: ① 마지막 세그먼트 원문 겹침 0% 제거 ② 끝부분 0.3초 미만 연속 단어 트림 (Gemini 오보정 제거)
- **Gemini 보정**: 전체 단어 단위 (줄 무관), SequenceMatcher 타이밍 보존, 보컬라이즈 보존, delete 시 원문에 없는 단어만 삭제 허용, 맨 끝 insert 허용 (트림이 환각 방어)
- **Step 4 구 클립 방지**: 이미지보다 오래된 클립 자동 삭제 (이미지 재생성 후 잔존 방지)
- **재생성 WebSocket 알림**: Step 3/4 재생성 시 emitter로 실시간 슬롯 상태 전송
- **트리밍 → 보정 순서**: scene_count 맞추기 후 Gemini 보정 (잘린 가사 복원 방지)
- **보컬 감지 기준**: 인식 단어 수 < 원문 30% 미만이면 실패 → Step 1 재시도
- **자막 ASS**: 카페24 단정해(40px), 2줄 교대, 3단어/줄, 무음 갭 fade-out
- **첫 프레임 가사 숨김**: 썸네일용 (제목만 표시)
- whisperx forced alignment 실패 시 faster-whisper fallback
- 0.1초 미만 단어 필터링 (노이즈 오인식 방지)

### Frontend
순수 HTML/JS/CSS (프레임워크 없음). `frontend/` 디렉토리를 FastAPI StaticFiles로 서빙.
- `pipeline.js` — 파이프라인 실행/모니터링 (WebSocket)
- `dashboard.js` — 프로젝트 목록
- `feedback.js` — 피드백 UI
- `settings.js` — API 키 설정 + 자동 생성 스케줄러 상태 (생성 중/다음 생성/이력)
- **스타일 가이드**: `frontend/STYLE_GUIDE.md` — 프론트엔드 작업 시 반드시 참조 (간격, 폰트, 보더, 컴포넌트 패턴)

## Important Rules
- art_style은 애니메이션/일러스트 스타일만 (실사 photorealistic/photograph 금지, 3D 애니메이션은 OK)
- image_prompt에 "no text, no subtitles" 필수 포함
- S2V 오디오: 보컬 분리 → 16kHz mono WAV 변환 필수
- 비디오 해상도: 576x1024 (9:16 세로), 24fps 최종 출력
- 자동/수동 작품 구분: `projects.source` 컬럼 ('auto' / 'manual')
- **프론트엔드 CSS 작업 시 `frontend/STYLE_GUIDE.md` 반드시 참조**, 새 패턴/규칙 추가 시 스타일 가이드도 함께 보강

## Infrastructure
- **도메인**: musiqcut.com (Cloudflare Tunnel + Access Google 로그인)
- GPU: RTX 4070 Ti SUPER (16GB VRAM)
- PM2 프로세스: `musical-pipeline` (8000), `comfyui` (8189), `cloudflared` (tunnel)
- Python venv: `venv/Scripts/python.exe`
- ComfyUI + 모델: `vendor/ComfyUI/` (gitignored)
- 프로젝트 출력: `storage/projects/` (gitignored)
- OAuth 콜백: `https://musiqcut.com/api/upload/{platform}/callback`
- 공개 페이지: `/privacy.html`, `/terms.html` (Cloudflare Access Bypass)

## Frontend 주의사항
- JS/CSS 수정 시 `index.html`의 `?v=` 캐시버스팅 반드시 업데이트 (Cloudflare CDN 캐시)
