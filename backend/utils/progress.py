import asyncio
import json
from datetime import datetime, timezone
from typing import Any
from fastapi import WebSocket

# ── 글로벌 emitter 레지스트리 (project_id → ProgressEmitter) ──
_emitters: dict[str, "ProgressEmitter"] = {}


def get_emitter(project_id: str) -> "ProgressEmitter | None":
    return _emitters.get(project_id)


def register_emitter(project_id: str, emitter: "ProgressEmitter"):
    _emitters[project_id] = emitter


def unregister_emitter(project_id: str):
    _emitters.pop(project_id, None)


class ProgressEmitter:
    def __init__(self, project_id: str):
        self.project_id = project_id
        self._clients: set[WebSocket] = set()
        self._done = False
        self._step_starts: dict[int, str] = {}
        # 스텝별 최신 이벤트 (재접속 시 전체 상태 동기화)
        self._latest_events: dict[int, dict] = {}  # step_no → event
        self._complete_event: dict | None = None

    async def _broadcast(self, event: dict):
        """모든 연결된 WebSocket 클라이언트에 이벤트 전송."""
        # 스텝별 최신 상태 저장 (재접속 시 동기화용)
        if event.get("type") == "step" and "step" in event:
            self._latest_events[event["step"]] = event
        elif event.get("type") in ("complete", "error"):
            self._complete_event = event

        dead = set()
        msg = json.dumps(event, ensure_ascii=False)
        for ws in list(self._clients):  # 복사본 순회 (동시 수정 방지)
            try:
                await ws.send_text(msg)
            except Exception:
                dead.add(ws)
        self._clients -= dead

    async def register(self, ws: WebSocket):
        """WebSocket 클라이언트 등록 + 전체 상태 동기화."""
        self._clients.add(ws)
        # 재접속 시 각 스텝의 최신 상태 전송
        for step_no in sorted(self._latest_events.keys()):
            try:
                await ws.send_text(json.dumps(self._latest_events[step_no], ensure_ascii=False))
            except Exception:
                self._clients.discard(ws)
                return
        if self._complete_event:
            try:
                await ws.send_text(json.dumps(self._complete_event, ensure_ascii=False))
            except Exception:
                self._clients.discard(ws)

    def unregister(self, ws: WebSocket):
        self._clients.discard(ws)

    @property
    def done(self):
        return self._done

    async def update(self, step: int, status: str, message: str = "",
                     data: Any = None):
        now = datetime.now(timezone.utc).isoformat()
        if status == "running" and step not in self._step_starts:
            self._step_starts[step] = now
        event = {
            "type": "step",
            "step": step,
            "status": status,
            "message": message,
            "data": data or {},
            "started_at": self._step_starts.get(step),
            "finished_at": now if status in ("done", "failed") else None,
        }
        if status in ("done", "failed"):
            self._step_starts.pop(step, None)
        await self._broadcast(event)

    def step_progress(self, step: int, label: str = "처리"):
        """특정 스텝 전용 progress 콜백 생성."""
        async def _cb(current: int, total: int):
            await self.update(
                step=step,
                status="running",
                message=f"{label} 중... {current}/{total}",
                data={"current": current, "total": total}
            )
        return _cb

    async def image_progress(self, current: int, total: int):
        await self.update(
            step=3,
            status="running",
            message=f"이미지 생성 중... {current}/{total}",
            data={"current": current, "total": total}
        )

    async def complete(self, video_path: str):
        event = {
            "type": "complete",
            "video_url": f"/storage/{video_path.lstrip('./')}",
            "message": "영상 생성 완료!"
        }
        await self._broadcast(event)
        self._done = True
        unregister_emitter(self.project_id)

    async def error(self, step: int, message: str):
        event = {
            "type": "error",
            "step": step,
            "message": message
        }
        await self._broadcast(event)
        self._done = True
        unregister_emitter(self.project_id)
