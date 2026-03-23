"""스토리/컨셉 생성 + 장면 구성 — Gemini 2.5 Flash"""
import asyncio
import json
import re
from backend.config import settings
from backend.models.project import GeneratedScript, ScriptScene
from backend.utils.gemini_client import gemini_generate

# ── 숏폼 설정 (최대 60초) ────────────────────────────
import random

def _get_short_guide():
    """숏폼 하이라이트 — 15~30초."""
    lines = random.choice(["2~4줄", "3~4줄"])
    return {
        "lyrics_lines": lines,
        "duration": "15초 내외 (최대 30초, 짧을수록 좋음)",
        "structure": "가사 2~4줄 작성. 반복/간주 넣지 말 것. 인트로 없이 바로 시작, 짧게 끝내기. 가사 끝에 [End] 태그 필수.",
        "suno_hint": "very short 15 second jingle, immediate vocal, no repeats, no instrumental break, ends abruptly after vocals",
        "lyrics_suffix": "\n[End]",
    }

LENGTH_GUIDE = {
    "short": _get_short_guide,  # 함수로 매번 새로 생성
}

# ── STEP 1: 스토리/컨셉 + 작곡 지시 생성 ────────────────
STORY_PROMPT = """당신은 뮤지컬 애니메이션 콘텐츠 작가입니다.
아래 조건에 맞는 스토리 컨셉과 작곡 지시를 반드시 JSON 형식으로만 응답하세요.
다른 텍스트나 마크다운 없이 순수 JSON만 출력하세요.

조건:
- 테마: {theme}
- 분위기: {mood}
- 곡 분량: {duration_desc} ({lyrics_lines_desc} 가사)
(분위기가 'auto'이면 테마에서 자연스럽게 도출되는 분위기를 자유롭게 선택하세요)

작사 가이드:
- 가사는 반드시 {lyrics_lines_desc}로 작성 (이 줄 수가 곡 길이를 결정합니다!)
- {structure_desc}
- 첫 줄부터 임팩트 있는 가사로 시선을 사로잡을 것
- 매 줄이 독립적으로 강렬하고 기억에 남도록 작성
- 반복되는 후렴구 훅(hook)을 포함하면 중독성 UP
- 가사는 Suno AI가 자유롭게 해석하므로, 스토리/감정 전달에 집중

아트 스타일:
- 테마와 분위기에 가장 어울리는 **애니메이션 아트 스타일**을 자유롭게 선택하세요
- 예시: Pixar/Disney 3D, 스튜디오 지브리 수채화, 셀 애니메이션, 사이버펑크 네온, 판타지 유화, 미니멀 플랫 디자인, 빈티지 레트로, 누아르 등
- 선택한 스타일을 art_style 필드에 영문으로 명시하세요 (이 스타일이 모든 장면에 일관 적용됩니다)

영상 연출 참고 (가사에 반영):
- 이 가사는 위 아트 스타일의 뮤지컬 애니메이션이 됩니다
- 감정 표현이 풍부한 캐릭터 연기가 가능한 가사가 좋습니다
- 클로즈업(얼굴 강조)과 와이드샷(전신/배경)이 번갈아 나오는 뮤지컬 연출을 상상하세요

캐릭터 설계:
- 이 작품에 등장하는 **주인공과 주요 캐릭터**(최대 3명)의 외형을 상세히 정의하세요
- 모든 장면에서 동일한 캐릭터가 일관되게 등장해야 하므로, 구체적인 외형 묘사가 핵심입니다
- 각 캐릭터: 이름/별칭, 성별, 나이대, 머리 스타일/색, 눈 색, 피부톤, 체형, 의상, 특징적 액세서리
- description_en은 선택한 art_style에 맞춰 작성

출력 형식:
{{
  "title": "작품 제목 (한국어)",
  "lyrics": "전체 가사 (줄바꿈 포함, 한국어, 반드시 {lyrics_lines_desc})",
  "music_prompt": "Suno AI용 영문 음악 스타일 프롬프트 (예: epic orchestral, upbeat pop). 반드시 보컬이 포함된 곡이어야 함 (instrumental 금지). 보컬은 주인공 캐릭터에 어울리는 성별/연령대/음색으로 지정. 악기 나열보다 보컬 스타일/감정을 우선 기술할 것. 단, 오페라/성악/벨칸토 등 입을 크게 벌리는 창법은 금지 — 자연스러운 대중음악 창법만 사용 (pop, ballad, R&B, folk, rock, hip-hop, jazz 등). {suno_hint}",
  "art_style": "선택한 아트 스타일 (영문, 예: 'Pixar-style 3D animation' 또는 'Studio Ghibli watercolor' 또는 'cyberpunk neon cel-shading')",
  "characters": [
    {{
      "name": "캐릭터 이름/별칭 (한국어)",
      "description_en": "3D animated character, female, early 20s, shoulder-length wavy brown hair, bright green eyes, fair skin, slim build, wearing a yellow sundress with white sneakers, small star-shaped earring on left ear"
    }}
  ]
}}

중요: scenes 필드는 포함하지 마세요. 장면 구성은 음악 생성 후 별도로 진행됩니다."""


# ── STEP 3 전반부: 장면 구성 (음악 길이 기반) ──────────────
SCENE_PROMPT = """당신은 뮤지컬 애니메이션 장면 구성 전문가입니다.
아래 스토리를 기반으로 {scene_count}개의 장면을 구성하세요.
각 장면은 ~5초 영상 클립(512×768 세로)이며, AI 이미지 생성 + AI 영상 변환에 사용됩니다.
반드시 JSON 형식으로만 응답하세요.

작품 정보:
- 제목: {title}
- 가사/스토리: {lyrics}
- 분위기: {mood}
- 장면 수: {scene_count}개 (곡 길이 {duration}초 기반)
{characters_block}

샷 구성 가이드 (뮤지컬 영상 연출):
- 전체 장면의 20~30%는 **클로즈업** (얼굴이 화면의 30%+ 차지, 감정 전달/립싱크용)
- 전체 장면의 30~40%는 **미디엄샷** (상반신, 제스처와 표정 모두 보임)
- 전체 장면의 30~40%는 **와이드샷** (풍경/배경/소품/상징물 중심, 인물 없거나 작게)
- **인물 장면과 비인물 장면을 번갈아 배치** — 인물 클로즈업 다음엔 풍경/배경/소품 와이드샷으로 호흡 조절
- 와이드샷은 인물이 아닌 배경/풍경/자연/건물/소품/상징물을 주제로 (달빛, 강, 낙엽, 빈 옥좌, 성벽 등)
- 가장 감정이 강한 가사 줄은 클로즈업으로 배치
- **보컬 구간의 클로즈업/미디엄샷은 반드시 노래하는 주인공만** 배치
- **is_vocalist=false인 인물 장면은 얼굴 정면을 피할 것** — 뒷모습, 실루엣, 손/발 클로즈업, 멀리서 바라보는 구도 등 (AI 모델이 정면 얼굴을 말하는 것처럼 움직이는 문제 방지)
- 연속으로 같은 샷 타입이 3번 이상 나오면 안 됨 — 다채로운 구도 변화 필수

이미지 스타일:
- 아트 스타일: {art_style} (모든 장면에 일관 적용)
- 해상도: 512×768 (2:3 세로, 모바일 숏폼 최적)
- 캐릭터는 반드시 성인 또는 청소년(10대 후반~20대)으로 설정 (어린이/유아 금지)

각 장면은 가사의 흐름에 맞춰 감정적 전개를 보여줘야 합니다.

출력 형식:
{{
  "scenes": [
    {{
      "scene_no": 1,
      "shot_type": "closeup 또는 medium 또는 wide (반드시 영문 소문자)",
      "is_vocalist": true/false (이 장면이 주인공(보컬리스트)의 클로즈업/미디엄인 경우에만 true. 조연/충신/배경인물이면 무조건 false. 와이드샷은 무조건 false. 오직 노래하는 주인공 1인 장면만 true),
      "description": "장면 설명 (한국어, 샷 타입/캐릭터 동작/감정 포함, 예: '[클로즈업] 주인공이 눈을 감고 미소짓는다')",
      "image_prompt": "2:3 vertical portrait, [shot_type에 맞는 구도] {art_style}, [상세 영문 프롬프트, 배경/조명/캐릭터 포즈/표정 포함]"
    }}
  ]
}}

image_prompt 작성 규칙:
- **핵심: 캐릭터가 등장하는 장면은 반드시 위 캐릭터 프로필의 description_en을 image_prompt에 그대로 포함**
- 매 장면마다 캐릭터의 머리 스타일/색, 의상, 특징적 액세서리 등을 빠짐없이 반복 명시
- **한 장면에 등장하는 인물은 최대 2~3명까지** — 군중씬이나 4명 이상은 금지 (AI 이미지 생성 한계로 인물이 복제되어 보임). 2~3명일 때는 각 인물의 의상/체형/위치를 명확히 구분
- 클로즈업: **매번 다른 앵글/구도로** — 정면 초상화만 반복 금지! 다양한 예시: "side profile looking away", "over-the-shoulder from behind", "tilted angle looking up at the sky", "three-quarter view with wind in hair", "extreme close-up on eyes only", "low angle looking down at camera"
- 미디엄샷: "medium shot, upper body visible, dynamic hand gestures, head tilt, breathing motion, emotional body language"
- 와이드샷: "wide cinematic shot, full body silhouette, detailed environment, atmospheric lighting, character in motion"
- 모든 프롬프트에 조명, 분위기, 색감, 캐릭터의 감정 상태를 구체적으로 포함
- **모든 image_prompt에 반드시 포함**: "no text, no subtitles, no captions, no letters, no watermark, no title" — 이미지 안에 어떤 글자도 절대 들어가면 안 됨
- 모든 프롬프트에 조명, 분위기, 색감, 캐릭터의 감정 상태를 구체적으로 포함하되, 매번 다채롭고 창의적으로"""


def _parse_json(text: str) -> dict:
    """Gemini 응답에서 JSON 추출."""
    text = text.strip()
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)

    def _try(s):
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return json.loads(re.sub(r'[\x00-\x1f]+', ' ', s))

    try:
        return _try(text)
    except (json.JSONDecodeError, ValueError):
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if not match:
            raise ValueError(f"Gemini 응답에서 JSON을 찾을 수 없습니다: {text[:200]}")
        return _try(match.group())


def _load_prompt_rules(step: int) -> str:
    """피드백 기반 프롬프트 오버라이드 로드."""
    from backend.services.feedback_service import load_overrides
    overrides = load_overrides()
    return overrides.get(f"step_{step}_rules", "")


async def generate_story(theme: str, mood: str, length: str = "short") -> dict:
    """STEP 1: 스토리/컨셉 + 작곡 지시 생성."""
    guide_fn = LENGTH_GUIDE.get(length, LENGTH_GUIDE["short"])
    guide = guide_fn() if callable(guide_fn) else guide_fn
    prompt = STORY_PROMPT.format(
        theme=theme, mood=mood,
        duration_desc=guide["duration"],
        lyrics_lines_desc=guide["lyrics_lines"],
        structure_desc=guide["structure"],
        suno_hint=guide["suno_hint"],
    )
    # 피드백 기반 추가 규칙
    extra = _load_prompt_rules(1)
    if extra:
        prompt += f"\n\n추가 규칙 (사용자 피드백 반영):\n{extra}"

    response = await gemini_generate(
        model="gemini-2.5-flash",
        contents=prompt
    )
    data = _parse_json(response.text)
    return {
        "title": data["title"],
        "lyrics": data["lyrics"],
        "music_prompt": data["music_prompt"],
        "art_style": data.get("art_style", "Pixar-style 3D animation"),
        "characters": data.get("characters", []),
    }


async def generate_scenes(title: str, lyrics: str, mood: str,
                           scene_count: int, duration: int,
                           scene_timing: list[dict] | None = None,
                           characters: list[dict] | None = None,
                           art_style: str = "Pixar-style 3D animation") -> list[ScriptScene]:
    """STEP 3 전반부: 음악 길이 기반 장면 구성."""

    # 캐릭터 프로필 블록 생성
    characters_block = ""
    if characters:
        characters_block = "\n등장 캐릭터 프로필 (모든 장면의 image_prompt에 해당 캐릭터의 외형을 반드시 포함):\n"
        for ch in characters:
            characters_block += f"- {ch['name']}: {ch['description_en']}\n"

    # 가사 타이밍이 있으면 장면별 가사/시간 정보를 프롬프트에 포함
    timing_info = ""
    if scene_timing:
        timing_info = "\n\n장면별 가사 타이밍 (Whisper 분석 결과 — 이 타이밍에 맞춰 장면을 구성하세요):\n"
        for i, st in enumerate(scene_timing):
            text = st.get("text", "")
            has_vocal = st.get("has_vocal", bool(text))
            label = f'가사: "{text}"' if has_vocal else "(instrumental)"
            timing_info += (
                f"- 장면 {i+1}: {st['start']}~{st['end']}초 "
                f"({st['end'] - st['start']:.1f}초) — {label}\n"
            )
        timing_info += "\n각 장면의 description과 image_prompt는 해당 시간대의 가사 내용에 정확히 맞춰야 합니다."

    prompt = SCENE_PROMPT.format(
        title=title, lyrics=lyrics, mood=mood,
        scene_count=scene_count, duration=duration,
        characters_block=characters_block,
        art_style=art_style,
    ) + timing_info
    extra = _load_prompt_rules(3)
    if extra:
        prompt += f"\n\n추가 규칙 (사용자 피드백 반영):\n{extra}"

    response = await gemini_generate(
        model="gemini-2.5-flash",
        contents=prompt
    )
    data = _parse_json(response.text)
    return [ScriptScene(**s) for s in data["scenes"]]
