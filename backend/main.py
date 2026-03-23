from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from backend.database import init_db
from backend.routers import projects, pipeline, test, keys, system, feedback


class HtmlNoCacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if "text/html" in response.headers.get("content-type", ""):
            response.headers["cache-control"] = "no-cache, no-store, must-revalidate"
            response.headers["pragma"] = "no-cache"
            response.headers["expires"] = "0"
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    # DB 초기화
    await init_db()
    # storage 폴더 보장
    Path("storage/projects").mkdir(parents=True, exist_ok=True)
    Path("storage/temp").mkdir(parents=True, exist_ok=True)
    # 스케줄러 복원
    try:
        from backend.services.scheduler_service import _get_schedule_config, start_scheduler
        for stype in ("generation", "feedback"):
            config = await _get_schedule_config(stype)
            if config.get("enabled"):
                start_scheduler(stype)
    except Exception:
        pass
    yield


app = FastAPI(
    title="MusiqCut",
    description="AI 뮤직비디오 자동 생성 파이프라인",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(HtmlNoCacheMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# API 라우터
app.include_router(projects.router, prefix="/api/projects", tags=["projects"])
app.include_router(pipeline.router, prefix="/api/pipeline", tags=["pipeline"])
app.include_router(test.router, prefix="/api/test", tags=["test"])
app.include_router(keys.router, prefix="/api/keys", tags=["keys"])
app.include_router(system.router, prefix="/api/system", tags=["system"])
app.include_router(feedback.router, prefix="/api/feedback", tags=["feedback"])

# 정적 파일 서빙
app.mount("/storage", StaticFiles(directory="storage"), name="storage")
app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
