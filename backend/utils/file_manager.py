from pathlib import Path
from backend.config import settings


def project_dir(project_id: str) -> Path:
    path = settings.storage_path / "projects" / project_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def music_path(project_id: str) -> Path:
    d = project_dir(project_id) / "music"
    d.mkdir(exist_ok=True)
    return d / "output.mp3"


def image_path(project_id: str, scene_no: int) -> Path:
    d = project_dir(project_id) / "images"
    d.mkdir(exist_ok=True)
    return d / f"scene_{scene_no:02d}.png"


def clip_path(project_id: str, scene_no: int) -> Path:
    d = project_dir(project_id) / "clips"
    d.mkdir(exist_ok=True)
    return d / f"clip_{scene_no:02d}.mp4"


def lipsync_clip_path(project_id: str, scene_no: int) -> Path:
    d = project_dir(project_id) / "clips"
    d.mkdir(exist_ok=True)
    return d / f"lipsync_{scene_no:02d}.mp4"


def audio_segment_path(project_id: str, scene_no: int) -> Path:
    d = project_dir(project_id) / "audio_segments"
    d.mkdir(exist_ok=True)
    return d / f"segment_{scene_no:02d}.mp3"


def video_path(project_id: str) -> Path:
    d = project_dir(project_id) / "video"
    d.mkdir(exist_ok=True)
    return d / "final.mp4"


def lyrics_path(project_id: str) -> Path:
    return project_dir(project_id) / "lyrics.json"
