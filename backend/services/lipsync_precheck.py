"""립싱크 사전 검사 — 보컬 분리 + 보컬 감지 + 얼굴/샷 분류"""
import asyncio
import subprocess
import sys
import numpy as np
from pathlib import Path


async def separate_vocals(audio_path: str, output_dir: str) -> str:
    """Demucs v4로 보컬 분리. 분리된 보컬 파일 경로 반환."""
    out = Path(output_dir)
    vocals_path = out / "htdemucs" / Path(audio_path).stem / "vocals.wav"

    # 이미 분리되어 있으면 스킵
    if vocals_path.exists() and vocals_path.stat().st_size > 1000:
        print(f"[STEP5] 보컬 분리 캐시 사용: {vocals_path}", file=sys.stderr)
        return str(vocals_path)

    print(f"[STEP5] Demucs 보컬 분리 중...", file=sys.stderr)
    cmd = [
        sys.executable, "-m", "demucs",
        "--two-stems=vocals",
        "-o", str(out),
        str(Path(audio_path).resolve()),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, stderr = await proc.communicate()

    if proc.returncode != 0 or not vocals_path.exists():
        print(f"[STEP5] Demucs 실패, 원본 오디오 사용: {stderr.decode()[-300:]}",
              file=sys.stderr)
        return audio_path  # 폴백: 원본 사용

    print(f"[STEP5] 보컬 분리 완료: {vocals_path}", file=sys.stderr)
    return str(vocals_path)


async def check_vocal_energy(audio_segment_path: str,
                             threshold: float = 0.01) -> bool:
    """오디오 세그먼트에 보컬이 있는지 RMS 에너지로 판단."""
    seg = Path(audio_segment_path)
    if not seg.exists():
        return False

    try:
        import librosa
        y, sr = librosa.load(str(seg), sr=16000, mono=True)
        if len(y) == 0:
            return False
        rms = float(np.sqrt(np.mean(y ** 2)))
        return rms > threshold
    except Exception:
        return True  # 판단 불가 시 적용


# 샷 분류 상수
SHOT_CLOSEUP = "closeup"       # 얼굴 15%+ → 립싱크 필수
SHOT_MEDIUM = "medium"         # 얼굴 5~15% → 립싱크 시도
SHOT_WIDE = "wide"             # 얼굴 <5% 또는 미감지 → 스킵


async def classify_shot(
    video_path: str,
) -> tuple[str, float, str]:
    """클립의 샷 타입을 분류.
    Returns: (shot_type, face_ratio, reason)"""
    try:
        import cv2
        from insightface.app import FaceAnalysis

        cap = cv2.VideoCapture(str(Path(video_path).resolve()))
        ret, frame = cap.read()
        cap.release()
        if not ret or frame is None:
            return SHOT_WIDE, 0.0, "프레임 읽기 실패"

        h, w = frame.shape[:2]

        app = FaceAnalysis(
            allowed_modules=["detection"],
            root="checkpoints/auxiliary",
            providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
        )
        app.prepare(ctx_id=0, det_size=(640, 640))
        faces = app.get(frame)

        if not faces:
            return SHOT_WIDE, 0.0, "얼굴 미감지"

        # 가장 큰 얼굴
        face = max(faces, key=lambda f:
                   (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))

        # 감지 신뢰도 체크
        if face.det_score < 0.3:
            return SHOT_WIDE, 0.0, f"감지 신뢰도 낮음 ({face.det_score:.2f})"

        fw = face.bbox[2] - face.bbox[0]
        fh = face.bbox[3] - face.bbox[1]
        face_ratio = (fw * fh) / (w * h)

        if face_ratio >= 0.15:
            return SHOT_CLOSEUP, face_ratio, f"클로즈업 ({face_ratio:.1%})"
        elif face_ratio >= 0.05:
            return SHOT_MEDIUM, face_ratio, f"미디엄 ({face_ratio:.1%})"
        else:
            return SHOT_WIDE, face_ratio, f"와이드 ({face_ratio:.1%})"

    except Exception as e:
        return SHOT_MEDIUM, 0.0, f"분류 실패, 기본 시도: {e}"
