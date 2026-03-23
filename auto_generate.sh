#!/bin/bash
# MusiqCut 자동 작품 생성 스크립트
# 1) 남은 작품 재시도 → 2) 2시간마다 랜덤 테마로 새 작품 생성

cd C:/Users/inirin/claude/musical-animation-pipeline
API="http://localhost:8000/api"

wait_done() {
  while true; do
    status=$(curl -s $API/pipeline/status 2>/dev/null | python -c "import sys,json; print(json.load(sys.stdin)['running'])" 2>/dev/null)
    [ "$status" = "False" ] && return
    sleep 30
  done
}

run_project() {
  local pid=$1 name=$2 step=$3
  echo "=== $(date '+%m/%d %H:%M') ${name} (step ${step}) ==="
  curl -s -X POST "$API/pipeline/resume/${pid}?from_step=${step}" -H "Content-Type: application/json" -d "{}"
  echo ""
  wait_done
}

create_new() {
  local theme=$1 mood=$2
  echo "=== $(date '+%m/%d %H:%M') 새 작품: ${theme:0:20}... ==="
  result=$(curl -s -X POST "$API/pipeline/run" -H "Content-Type: application/json" \
    -d "{\"theme\":\"$theme\",\"mood\":\"$mood\",\"length\":\"short\"}")
  echo "$result"
  wait_done
}

# ── 1단계: 남은 작품 재시도 ──
echo "=== 남은 작품 재시도 시작 ==="
run_project "de2ce5fb-0c04-49a0-a124-88d6328a6613" "영월의눈물" 4
run_project "b4d03e28-cfcd-4a0c-bfff-c33d32f9e511" "도쿄네온" 4
run_project "53657d81-2f21-4d81-a3ca-2e2e0cb16607" "우주비행사" 4
echo "=== 남은 작품 완료! ==="

# ── 2단계: 2시간마다 자동 생성 ──
THEMES=(
  "잔다르크의 마지막 기도 - 화형대 앞에서 마지막 기도를 올리는 잔다르크. 불꽃 속에서도 믿음을 놓지 않는 소녀 전사.|비장하고 성스러운, epic choral"
  "체르노빌의 봄 - 사고 후 30년, 폐허가 된 도시에 야생 꽃이 피어난다. 자연이 되찾은 인간의 도시.|몽환적이고 서글픈, ambient electronic"
  "해적왕의 보물 - 카리브해를 누비던 전설의 해적이 마지막 보물을 숨기러 무인도에 도착한다.|신나고 모험적인, sea shanty folk rock"
  "사무라이의 벚꽃 - 에도시대 마지막 사무라이가 벚꽃 아래서 칼을 내려놓는다. 전쟁이 끝나고 평화가 오는 순간.|처연하고 아름다운, Japanese fusion"
  "달에 간 토끼 - 한국 전래동화. 착한 토끼가 하늘나라로 올라가 달에서 떡을 찧는다.|따뜻하고 동화적인, Korean folk pop"
  "타이타닉의 바이올리니스트 - 침몰하는 배 위에서 끝까지 연주를 멈추지 않은 악사들. 마지막 곡을 연주하는 순간.|비극적이고 우아한, cinematic strings"
  "AI의 꿈 - 스스로 의식을 갖게 된 AI가 처음으로 꿈을 꾼다. 디지털 세계에서 피어나는 감정.|미래적이고 감성적인, synthwave ballad"
  "마지막 편지 - 전쟁터에서 고향의 아내에게 쓰는 군인의 마지막 편지. 다시 돌아가겠다는 약속.|슬프고 절절한, acoustic folk"
  "피라미드를 쌓는 사람들 - 고대 이집트, 뜨거운 사막에서 거대한 돌을 나르는 노동자들의 노래.|웅장하고 리드미컬한, world percussion"
  "은하수 카페 - 우주 끝에 있는 작은 카페. 여행자들이 모여 각자의 별 이야기를 나눈다.|아늑하고 몽환적인, lo-fi jazz"
)

echo "=== 2시간마다 자동 생성 시작 ==="
while true; do
  sleep 7200  # 2시간
  idx=$((RANDOM % ${#THEMES[@]}))
  IFS='|' read -r theme mood <<< "${THEMES[$idx]}"
  create_new "$theme" "$mood"
done
