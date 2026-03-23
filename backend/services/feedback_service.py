"""피드백 분석 → 프롬프트 자동 개선 (안전장치 포함)"""
import json
import sys
from datetime import datetime
from pathlib import Path
import aiosqlite
from backend.database import DB_PATH
from backend.utils.gemini_client import gemini_generate

OVERRIDE_PATH = Path("storage/prompt_overrides.json")
OVERRIDE_HISTORY_PATH = Path("storage/prompt_overrides_history.json")

# 안전장치 상수
MIN_FEEDBACKS_TO_PROCESS = 3   # 최소 피드백 수 (편향 방지)
MAX_RULES_LENGTH = 500         # 규칙 최대 길이 (프롬프트 비대화 방지)

STEP_TARGETS = {
    0: "전체 작품 품질",
    1: "스토리/컨셉 생성",
    2: "음악 생성 (보컬/장르/길이)",
    3: "이미지 생성 (구도/스타일/캐릭터)",
    4: "영상 클립 생성 (모션/립싱크/자연스러움)",
    5: "최종 합성",
}


def load_overrides() -> dict:
    if OVERRIDE_PATH.exists():
        return json.loads(OVERRIDE_PATH.read_text(encoding="utf-8"))
    return {}


def save_overrides(overrides: dict):
    # 변경 전 히스토리 저장 (롤백용)
    _save_history(overrides)
    overrides["last_updated"] = datetime.utcnow().isoformat()
    OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
    OVERRIDE_PATH.write_text(
        json.dumps(overrides, ensure_ascii=False, indent=2), encoding="utf-8")


def _save_history(current: dict):
    """변경 전 상태를 히스토리에 추가 (롤백용)."""
    history = []
    if OVERRIDE_HISTORY_PATH.exists():
        try:
            history = json.loads(OVERRIDE_HISTORY_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    history.append({
        "timestamp": datetime.utcnow().isoformat(),
        "overrides": current
    })
    # 최근 20개만 유지
    history = history[-20:]
    OVERRIDE_HISTORY_PATH.write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


def rollback_overrides() -> bool:
    """마지막 변경 전 상태로 롤백."""
    if not OVERRIDE_HISTORY_PATH.exists():
        return False
    history = json.loads(OVERRIDE_HISTORY_PATH.read_text(encoding="utf-8"))
    if not history:
        return False
    last = history.pop()
    OVERRIDE_HISTORY_PATH.write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    OVERRIDE_PATH.write_text(
        json.dumps(last["overrides"], ensure_ascii=False, indent=2), encoding="utf-8")
    return True


async def analyze_and_improve(feedbacks: list[dict], projects_info: list[dict]) -> list[dict]:
    """피드백 분석 → 프롬프트 개선안 생성."""
    # 스텝별 그룹핑
    by_step = {}
    for f in feedbacks:
        step = f.get("step_no") or 0
        by_step.setdefault(step, []).append(f)

    # 작품 정보 텍스트
    projects_text = "\n".join(
        f"- {p.get('title','?')}: 테마={p.get('theme','?')}, 분위기={p.get('mood','?')}"
        for p in projects_info[:5])

    overrides = load_overrides()
    improvements = []

    for step, step_feedbacks in by_step.items():
        # 안전장치 1: 최소 피드백 수 미달 시 스킵
        if len(step_feedbacks) < MIN_FEEDBACKS_TO_PROCESS:
            print(f"[Feedback] STEP {step}: 피드백 {len(step_feedbacks)}개 < "
                  f"최소 {MIN_FEEDBACKS_TO_PROCESS}개, 스킵", file=sys.stderr)
            continue

        step_name = STEP_TARGETS.get(step, f"STEP {step}")
        key = f"step_{step}_rules"
        current_rules = overrides.get(key, "")

        likes = sum(1 for f in step_feedbacks if f["feedback_type"] == "like")
        dislikes = sum(1 for f in step_feedbacks if f["feedback_type"] == "dislike")
        comments = [f["content"] for f in step_feedbacks
                    if f["feedback_type"] == "comment" and f.get("content")]

        fb_text = f"👍 {likes}개 / 👎 {dislikes}개\n"
        if comments:
            fb_text += "코멘트:\n" + "\n".join(f"- {c}" for c in comments[:20])

        prompt = f"""AI 뮤직비디오 파이프라인의 프롬프트 최적화.

[대상] {step_name}
[작품 정보] {projects_text}
[피드백] {fb_text}
[현재 규칙] {current_rules or '없음'}

규칙:
1. 피드백에서 반복되는 패턴만 반영 (1회성 의견은 무시)
2. 기존 규칙과 상충하면 최신 피드백 우선, 기존 규칙 교체
3. 규칙은 간결하게 (총 {MAX_RULES_LENGTH}자 이내)
4. 다양성을 해치는 규칙은 피하기
5. 확실하지 않으면 규칙 추가하지 말 것

JSON으로만 응답:
{{"summary": "변경 요약 (한국어)", "rules": "최종 규칙 전문 (기존+신규 통합, {MAX_RULES_LENGTH}자 이내)", "changed": true/false}}"""

        try:
            resp = await gemini_generate(
                model="gemini-2.5-flash", contents=prompt)
            text = resp.text.strip().replace("```json", "").replace("```", "").strip()
            result = json.loads(text)

            if not result.get("changed", False):
                print(f"[Feedback] STEP {step}: 변경 불필요 판단", file=sys.stderr)
                continue

            # 안전장치 2: 규칙 길이 제한
            new_rules = result.get("rules", "")[:MAX_RULES_LENGTH]

            overrides[key] = new_rules
            save_overrides(overrides)

            improvement = {
                "step_target": step,
                "before_summary": current_rules[:200] if current_rules else "없음",
                "after_summary": result.get("summary", ""),
                "changes_applied": json.dumps(result, ensure_ascii=False),
            }
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "INSERT INTO prompt_improvements "
                    "(step_target, feedback_ids, before_summary, after_summary, "
                    "changes_applied, applied_at) VALUES (?,?,?,?,?,?)",
                    (step,
                     json.dumps([f["id"] for f in step_feedbacks]),
                     improvement["before_summary"],
                     improvement["after_summary"],
                     improvement["changes_applied"],
                     datetime.utcnow().isoformat()))
                await db.commit()

            improvements.append(improvement)
            print(f"[Feedback] STEP {step} 개선: {result.get('summary', '')}",
                  file=sys.stderr)

        except Exception as e:
            print(f"[Feedback] STEP {step} 분석 실패: {e}", file=sys.stderr)

    return improvements
