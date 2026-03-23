from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class ProjectCreate(BaseModel):
    theme: str
    mood: str


class Project(BaseModel):
    id: str
    title: Optional[str] = None
    theme: str
    mood: str
    status: str  # pending / running / done / failed
    created_at: datetime
    updated_at: datetime
    video_path: Optional[str] = None
    error_msg: Optional[str] = None


class PipelineStep(BaseModel):
    id: int
    project_id: str
    step_no: int
    step_name: str
    status: str
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    output_data: Optional[str] = None
    error_msg: Optional[str] = None


class ScriptScene(BaseModel):
    scene_no: int
    description: str
    image_prompt: str
    vocal_lines: list[str] = []
    shot_type: str = "medium"  # "closeup" | "medium" | "wide"
    is_vocalist: bool = False  # 이 장면의 주체가 노래하는 캐릭터인지
    start_sec: float = 0.0     # 음악 내 시작 시점 (초)
    end_sec: float = 0.0       # 음악 내 종료 시점 (초)
    duration: float = 5.0      # 장면 길이 (초)


class GeneratedScript(BaseModel):
    title: str
    lyrics: str
    music_prompt: str
    scenes: list[ScriptScene]


class PipelineRunRequest(BaseModel):
    theme: str
    mood: str
    length: str = "short"  # 숏폼 전용 (최대 60초)
