"""시스템 리소스 모니터링 API."""
import asyncio
import subprocess
from fastapi import APIRouter

import psutil

router = APIRouter()


def _gpu_stats() -> dict | None:
    """nvidia-smi로 GPU 정보 조회."""
    try:
        r = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,name",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return None
        parts = [p.strip() for p in r.stdout.strip().split(",")]
        return {
            "name": parts[4],
            "util": int(parts[0]),
            "mem_used": int(parts[1]),
            "mem_total": int(parts[2]),
            "temp": int(parts[3]),
        }
    except Exception:
        return None


@router.get("/stats")
async def system_stats():
    """CPU, RAM, GPU 실시간 사용량."""
    gpu_future = asyncio.to_thread(_gpu_stats)

    cpu_percent = psutil.cpu_percent(interval=0.3)
    mem = psutil.virtual_memory()

    gpu = await gpu_future

    return {
        "cpu": {"percent": cpu_percent, "cores": psutil.cpu_count()},
        "ram": {
            "percent": mem.percent,
            "used": round(mem.used / 1024**3, 1),
            "total": round(mem.total / 1024**3, 1),
        },
        "gpu": gpu,
    }
