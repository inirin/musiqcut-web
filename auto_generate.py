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

API = "http://localhost:8000/api"

THEMES = [
    ("잔다르크의 마지막 기도 - 화형대 앞에서 마지막 기도를 올리는 잔다르크. 불꽃 속에서도 믿음을 놓지 않는 소녀 전사.",
     "비장하고 성스러운, epic choral"),
    ("체르노빌의 봄 - 사고 후 30년, 폐허가 된 도시에 야생 꽃이 피어난다. 자연이 되찾은 인간의 도시.",
     "몽환적이고 서글픈, ambient electronic"),
    ("해적왕의 보물 - 카리브해를 누비던 전설의 해적이 마지막 보물을 숨기러 무인도에 도착한다.",
     "신나고 모험적인, sea shanty folk rock"),
    ("사무라이의 벚꽃 - 에도시대 마지막 사무라이가 벚꽃 아래서 칼을 내려놓는다. 전쟁이 끝나고 평화가 오는 순간.",
     "처연하고 아름다운, Japanese fusion"),
    ("달에 간 토끼 - 한국 전래동화. 착한 토끼가 하늘나라로 올라가 달에서 떡을 찧는다.",
     "따뜻하고 동화적인, Korean folk pop"),
    ("타이타닉의 바이올리니스트 - 침몰하는 배 위에서 끝까지 연주를 멈추지 않은 악사들. 마지막 곡을 연주하는 순간.",
     "비극적이고 우아한, cinematic strings"),
    ("AI의 꿈 - 스스로 의식을 갖게 된 AI가 처음으로 꿈을 꾼다. 디지털 세계에서 피어나는 감정.",
     "미래적이고 감성적인, synthwave ballad"),
    ("마지막 편지 - 전쟁터에서 고향의 아내에게 쓰는 군인의 마지막 편지. 다시 돌아가겠다는 약속.",
     "슬프고 절절한, acoustic folk"),
    ("피라미드를 쌓는 사람들 - 고대 이집트, 뜨거운 사막에서 거대한 돌을 나르는 노동자들의 노래.",
     "웅장하고 리드미컬한, world percussion"),
    ("은하수 카페 - 우주 끝에 있는 작은 카페. 여행자들이 모여 각자의 별 이야기를 나눈다.",
     "아늑하고 몽환적인, lo-fi jazz"),
    ("빗속의 재즈 클럽 - 비 오는 밤, 골목 끝 지하 재즈 클럽에서 색소폰 연주가 울려퍼진다.",
     "무디하고 감성적인, jazz noir"),
    ("오로라 아래의 늑대 - 알래스카 설원에서 오로라를 바라보며 울부짖는 외로운 늑대.",
     "신비롭고 웅장한, orchestral ambient"),
    ("네온사인 도시의 고양이 - 화려한 네온이 빛나는 밤거리를 걸어다니는 떠돌이 고양이의 하루.",
     "세련되고 도시적인, city pop"),
    ("사막의 별 헤는 밤 - 사하라 사막 한가운데서 별을 세는 소녀. 모래바람이 불어도 별은 반짝인다.",
     "고요하고 경이로운, desert folk"),
    ("마녀의 약국 - 숲속에 숨겨진 마녀의 약국. 신비한 물약을 만들어 마을 사람들을 돕는다.",
     "신비롭고 장난스러운, fantasy pop"),
]

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

    # 셔플하여 중복 방지
    themes = list(THEMES)
    random.shuffle(themes)
    idx = 0

    while True:
        time.sleep(INTERVAL_SEC)
        theme, mood = themes[idx % len(themes)]
        idx += 1
        if idx >= len(themes):
            random.shuffle(themes)
            idx = 0
        create_new(theme, mood)


if __name__ == "__main__":
    main()
