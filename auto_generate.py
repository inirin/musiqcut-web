#!/usr/bin/env python
"""MusiqCut 자동 작품 생성 스크립트 (Python 버전).
1) 2시간마다 랜덤 테마로 새 작품 생성
"""
import json
import random
import sys
import time
import urllib.request
from datetime import datetime

sys.path.insert(0, __file__ and __import__('os').path.dirname(__file__) or '.')
from backend.utils.theme_pool import THEME_POOL, MOOD_POOL

API = "http://localhost:8000/api"

INTERVAL_SEC = 7200  # 2시간


def log(msg: str):
    ts = datetime.now().strftime("%m/%d %H:%M")
    line = f"=== {ts} {msg} ==="
    print(line, flush=True)


def api_post(path: str, data: dict | None = None) -> dict:
    body = json.dumps(data or {}).encode("utf-8")
    req = urllib.request.Request(
        f"{API}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "error": str(e)}


def api_get(path: str) -> dict:
    try:
        with urllib.request.urlopen(f"{API}{path}", timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {}


def wait_done(timeout: int = 3600):
    """파이프라인 완료 대기 (최대 timeout초)."""
    start = time.time()
    while time.time() - start < timeout:
        status = api_get("/pipeline/status")
        if not status.get("running", False):
            return True
        time.sleep(30)
    log("타임아웃! 파이프라인 완료 대기 초과")
    return False


def create_new(theme: str, mood: str):
    log(f"자동생성: {theme[:20]}...")
    result = api_post("/pipeline/run", {"theme": theme, "mood": mood, "length": "short"})
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if result.get("ok"):
        wait_done()
    else:
        log(f"생성 실패: {result.get('error', 'unknown')}")


def main():
    log("자동 생성 스크립트 시작 (2시간 간격)")

    themes = list(THEME_POOL)
    random.shuffle(themes)
    idx = 0

    while True:
        time.sleep(INTERVAL_SEC)
        theme = themes[idx % len(themes)]
        mood = random.choice(MOOD_POOL)
        idx += 1
        if idx >= len(themes):
            random.shuffle(themes)
            idx = 0
        create_new(theme, mood)


if __name__ == "__main__":
    main()
