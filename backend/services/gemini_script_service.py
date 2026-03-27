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
    lines = random.choice(["2~3줄", "3줄"])
    return {
        "lyrics_lines": lines,
        "duration": "15초 내외 (최대 30초, 짧을수록 좋음)",
        "structure": "가사 2~3줄 작성. 반복/간주 넣지 말 것. 인트로 없이 바로 시작, 짧게 끝내기. 가사 끝에 [End] 태그 필수.",
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
(분위기에 [트렌드 힌트: ...]가 포함되어 있다면, 그 실존 인물/브랜드/작품을 Google 검색으로 파악하세요.
 - 캐릭터 description_en: 해당 인물의 **실제 외형 특징**을 애니메이션 캐릭터로 변환하여 묘사 (체형, 헤어, 의상 등 — 반드시 "animated character" 명시, 실사/사진풍 절대 금지)
 - vocal_style: 해당 인물의 **실제 목소리/가창력**을 구체적으로 반영
 - art_style: 해당 작품/브랜드의 **실제 비주얼 톤**을 반영
 - music_prompt: 해당 인물/작품과 연관된 **실제 음악 장르/스타일**을 반영
 - 단, title과 lyrics에만 고유명사를 쓰지 마세요. 위 필드들에서는 고유명사를 적극 활용하세요)

작사 가이드:
- 가사는 반드시 {lyrics_lines_desc}로 작성 (이 줄 수가 곡 길이를 결정합니다!)
- {structure_desc}
- 첫 줄부터 임팩트 있는 가사로 시선을 사로잡을 것
- 매 줄이 독립적으로 강렬하고 기억에 남도록 작성
- 반복되는 후렴구 훅(hook)을 포함하면 중독성 UP
- **가사만 들어도 테마의 소재/상황이 구체적으로 그려져야 함** — 추상적 은유만 나열하지 말고, 소재의 핵심 장면/행동/감정을 직접적으로 표현 (예: 신용카드 연체 → "5일 늦은 카드값에 무너진 내 점수" 처럼 구체적으로)
- 가사는 Suno AI가 자유롭게 해석하므로, 스토리/감정 전달에 집중

장르와 톤:
- 뮤지컬 애니메이션의 장르를 테마에 맞게 자유롭게 선택하세요
- 뮤지컬 장르: 코미디, 로맨스, 비극, 풍자극, 환상극, 스릴러, 성장드라마, 서사극, 문학 각색, 희곡, 레뷔, 록 오페라 등
- 애니메이션 장르: 액션, 판타지, 로맨스, 코미디, 호러, 드라마, SF, 미스터리, 슬라이스 오브 라이프, 모험, 다크 판타지, 뮤지컬, 풍자, 실험 애니메이션 등
- 장르에 맞는 서사 구조와 감정 톤을 가사에 반영하세요

아트 스타일:
- 테마, 장르, 분위기에 가장 어울리는 **애니메이션 아트 스타일**을 자유롭게 선택하세요
- **[트렌드 힌트]가 있으면**: 해당 작품/브랜드의 **실제 비주얼/포스터/화면 톤**을 검색하여 art_style에 구체적으로 반영 (예: 왕과 사는 남자 → 한국 사극풍 동양화 터치, 라라랜드 → 따뜻한 네온 톤 뮤지컬 애니메이션)
- 3D 애니메이션: Pixar/Disney 스타일, DreamWorks 스타일, 카툰 렌더링(toon shading), 로우폴리, 클레이/점토 질감 3D, 미니어처 디오라마, Arcane 스타일, 스파이더버스 셀셰이딩 3D 등
- 2D 애니메이션: 셀 애니메이션, 스튜디오 지브리, 신카이 마코토, 플라이셔, 카르툰 살룬, 컷아웃, 실루엣, 로토스코핑 등
- 화풍별: 수채화, 유화, 라인 아트, 파스텔, 잉크워시, 우키요에, 아르누보, 아르데코, 팝아트, 픽셀아트, 점묘법 등
- 분위기별: 사이버펑크 네온, 누아르, 고딕, 빈티지 레트로, 미니멀 플랫, 그런지, 드림코어, 베이퍼웨이브, 스팀펑크 등
- 3D와 2D를 균형 있게 선택하세요 — 테마에 따라 3D가 더 어울리면 적극적으로 3D를 선택
- 위 예시에 얽매이지 말고 테마에 가장 맞는 독창적인 스타일을 선택하세요
- **절대 금지: 실사(photorealistic), 사진(photograph), 하이퍼리얼리즘, semi-realistic** — 이 프로젝트는 2D/3D 애니메이션 뮤직비디오 전용. 트렌드 힌트에 실존 인물이 있어도 반드시 애니메이션/일러스트 스타일로
- 선택한 스타일을 art_style 필드에 영문으로 구체적으로 명시하세요 (이 스타일이 모든 장면에 일관 적용됩니다)

해시태그:
- 이 작품을 YouTube/TikTok/Instagram 숏폼 채널에 업로드할 때 사용할 해시태그를 10~15개 생성하세요
- 한국어 + 영어 혼합 (한국어 70%, 영어 30%)
- 카테고리: 테마/소재 키워드, 장르, 분위기, 아트스타일, 트렌드 태그
- 예: #AI뮤직비디오 #애니메이션 #숏폼 #MusicVideo 등 범용 태그도 3~4개 포함
- 각 해시태그는 #으로 시작

영상 연출 참고 (가사에 반영):
- 이 가사는 위 아트 스타일의 뮤지컬 애니메이션이 됩니다
- 감정 표현이 풍부한 캐릭터 연기가 가능한 가사가 좋습니다
- 클로즈업(얼굴 강조)과 와이드샷(전신/배경)이 번갈아 나오는 뮤지컬 연출을 상상하세요

캐릭터 설계:
- 이 작품에 등장하는 **주인공과 주요 캐릭터**(최대 3명)의 외형을 상세히 정의하세요
- **[트렌드 힌트]가 있으면**: 해당 실존 인물의 **실제 체형/헤어/의상 특징**을 검색하여 description_en에 반영. 단, **반드시 2D/3D 애니메이션 캐릭터로 표현** — 실사/사진풍 묘사 절대 금지. "animated character"를 description_en 맨 앞에 명시
- 모든 장면에서 동일한 캐릭터가 일관되게 등장해야 하므로, 구체적인 외형 묘사가 핵심입니다
- 각 캐릭터: 이름/별칭, 성별, 나이대, 머리 스타일/색, 눈 색, 피부톤, 체형, 의상, 특징적 액세서리
- **헤어스타일은 AI 이미지 일관성의 핵심** — 머리카락이 있는 캐릭터는 반드시 구체적으로:
  - 길이: short/medium/long/very long + 정확한 위치 (chin-length, shoulder-length, waist-length 등)
  - 형태: straight, wavy, curly, braided, ponytail, twin tails, bun, bob cut, pixie cut, undercut 등
  - 색상: 단색이면 정확한 색 (jet black, platinum blonde, chestnut brown), 그라데이션이면 양쪽 색 명시
  - 앞머리: bangs 유무 + 형태 (blunt bangs, side-swept bangs, curtain bangs, no bangs 등)
  - 장식: 헤어핀, 리본, 머리띠, 꽃 등 있으면 반드시 명시
- **체형(build)도 AI 이미지 일관성의 핵심** — 반드시 구체적으로:
  - 체격: slim, slender, petite, athletic, muscular, stocky, heavyset, average build, curvy, lanky, broad-shouldered 등
  - 키: short, average height, tall, very tall 등 상대적 키
  - 체형 특징: narrow waist, wide hips, long legs, long neck, round face, angular jawline, small frame, large frame 등
  - 어깨: narrow shoulders, broad shoulders, rounded shoulders 등
  - **같은 캐릭터가 클로즈업/미디엄/와이드 모두에서 동일 체형으로 보여야 함** — 와이드샷에서 체형이 달라지지 않도록 명확히 정의
- description_en에 위 헤어 + 체형 정보를 **매번 빠짐없이** 포함하세요
- description_en은 선택한 art_style에 맞춰 작성
- **첫 번째 캐릭터 = 주인공 = 보컬리스트**: 이 캐릭터의 성별/나이대가 vocal_style과 반드시 일치해야 함

보컬 스타일:
- 주인공 캐릭터의 성별, 나이대, 성격에 어울리는 보컬을 구체적으로 정의하세요
- **[트렌드 힌트]가 있으면**: 해당 인물의 **실제 목소리 톤/음역대/가창 스타일**을 검색하여 vocal_style과 music_prompt에 구체적으로 반영 (예: "유지태처럼 낮고 묵직한 남성 바리톤")
- vocal_style 필드에 한국어로 간결하게 명시 (예: "20대 여성, 맑고 감성적인 목소리", "40대 남성, 깊고 허스키한 바리톤")
- music_prompt에도 영문으로 보컬 특성을 포함 — 반드시 vocal_style과 일관되게

출력 형식:
{{
  "title": "작품 제목 (한국어)",
  "lyrics": "전체 가사 (줄바꿈 포함, 한국어, 반드시 {lyrics_lines_desc})",
  "music_prompt": "Suno AI용 영문 음악 스타일 프롬프트 (최대 200자 이내!). 보컬 포함 필수. 보컬 성별/음색 + 장르 + 감정을 간결하게. {suno_hint}",
  "vocal_style": "보컬 스타일 (한국어, 예: '20대 여성, 맑고 감성적인 목소리' — 반드시 주인공 캐릭터의 성별/나이와 일치)",
  "art_style": "선택한 애니메이션 아트 스타일 (영문, 구체적으로 — photorealistic/photograph 금지)",
  "characters": [
    {{
      "name": "캐릭터 이름/별칭 (한국어)",
      "description_en": "3D animated character, female, early 20s, shoulder-length wavy chestnut brown hair with side-swept bangs, bright green eyes, fair skin, slim slender build, average height, narrow shoulders, long neck, wearing a yellow sundress with white sneakers, small star-shaped earring on left ear"
    }}
  ],
  "hashtags": ["#해시태그1", "#해시태그2", "...최대 15개"]
}}

중요: scenes 필드는 포함하지 마세요. 장면 구성은 음악 생성 후 별도로 진행됩니다."""


# ── STEP 3 전반부: 장면 구성 (음악 길이 기반) ──────────────
SCENE_PROMPT = """당신은 뮤지컬 애니메이션 장면 구성 전문가입니다.
아래 스토리를 기반으로 {scene_count}개의 장면을 구성하세요.
각 장면은 ~5초 영상 클립(576×1024 세로 9:16)이며, AI 이미지 생성 + AI 영상 변환에 사용됩니다.
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
- 전체 장면의 30~40%는 **와이드샷** (전신/배경 중심, 등장인물이 나올 수도 있고 풍경만일 수도 있음)
- **인물 장면과 비인물 장면을 번갈아 배치** — 인물 클로즈업 다음엔 풍경 와이드샷으로 호흡 조절
- **와이드샷에 주요 인물이 등장할 경우**: 반드시 캐릭터 프로필의 description_en 전체를 image_prompt에 포함
- **장면에 필요한 엑스트라(판사, 의사, 점원, 행인 등)**: 등장 가능하지만 **뒷모습, 실루엣, 흐릿한 배경 처리**로 묘사 (예: "a blurred silhouette of a judge seen from behind"). 엑스트라의 얼굴/외형을 구체적으로 묘사하지 말 것 — AI가 주인공과 혼동합니다
- **와이드샷에 인물이 없는 장면**: 반드시 사람이 존재할 수 없는 구도여야 함. 건물 외관, 하늘, 자연 풍경, 소품 극접사, 빈 거리 등. **실내(법정, 교실, 카페 등)는 인물 없이 그려도 AI가 사람을 추가하므로 피할 것** — 외부 전경이나 소품/상징물 클로즈업 위주로 구성
- 가장 감정이 강한 가사 줄은 클로즈업으로 배치
- **보컬 구간의 클로즈업/미디엄샷은 반드시 노래하는 주인공만** 배치
- **is_vocalist=false인 인물 장면은 얼굴 정면을 피할 것** — 뒷모습, 실루엣, 손/발 클로즈업, 멀리서 바라보는 구도 등 (AI 모델이 정면 얼굴을 말하는 것처럼 움직이는 문제 방지)
- 연속으로 같은 샷 타입이 3번 이상 나오면 안 됨 — 다채로운 구도 변화 필수

이미지 스타일:
- 아트 스타일: {art_style} (모든 장면에 일관 적용)
- 해상도: 576×1024 (9:16 세로, 모바일 숏폼 최적)
- 캐릭터는 반드시 성인 또는 청소년(10대 후반~20대)으로 설정 (어린이/유아 금지)

각 장면은 가사의 흐름에 맞춰 감정적 전개를 보여줘야 합니다.

출력 형식:
{{
  "scenes": [
    {{
      "scene_no": 1,
      "shot_type": "closeup 또는 medium 또는 wide (반드시 영문 소문자)",
      "is_vocalist": true/false (보컬 구간에서 주인공(첫 번째 캐릭터)이 화면에 등장하는 클로즈업/미디엄이면 무조건 true — 가사 내용과 무관하게 주인공이 보이면 true. 조연만 등장하거나 와이드샷이면 false),
      "description": "장면 설명 (한국어, 샷 타입/캐릭터 동작/감정 포함, 예: '[클로즈업] 주인공이 눈을 감고 미소짓는다')",
      "image_prompt": "vertical portrait composition, [shot_type에 맞는 구도] {art_style}, [상세 영문 프롬프트, 배경/조명/캐릭터 포즈/표정 포함]"
    }}
  ]
}}

image_prompt 작성 규칙:
- **핵심: 캐릭터가 등장하는 장면은 반드시 위 캐릭터 프로필의 description_en을 image_prompt에 그대로 포함**
- 매 장면마다 캐릭터의 체형(build/height/shoulders), 머리 스타일/색, 의상, 특징적 액세서리 등을 빠짐없이 반복 명시
- **특히 와이드샷/미디엄샷에서 체형 묘사 누락 금지** — 전신이 보이는 샷일수록 체형 키워드(slim, athletic, broad-shouldered 등)가 반드시 포함되어야 일관성 유지
- **한 장면에 등장하는 인물은 최대 2~3명까지** — 군중씬이나 4명 이상은 금지 (AI 이미지 생성 한계로 인물이 복제되어 보임). 2~3명일 때는 각 인물의 의상/체형/위치를 명확히 구분
- 클로즈업: **매번 다른 앵글/구도로** — 정면 초상화만 반복 금지! 다양한 예시: "side profile looking away", "over-the-shoulder from behind", "tilted angle looking up at the sky", "three-quarter view with wind in hair", "extreme close-up on eyes only", "low angle looking down at camera"
- 미디엄샷: "medium shot, upper body visible, dynamic hand gestures, head tilt, breathing motion, emotional body language"
- 와이드샷 (인물 없음): "wide cinematic shot, no people, no characters, no figures, empty scene, detailed environment, atmospheric lighting" — **실내 공간(법정, 카페, 교실 등)은 AI가 인물을 생성하므로 금지. 외부 풍경/건물 외관/하늘/소품 극접사만**
- 와이드샷 (인물 있음): "wide cinematic shot, full body, [주인공 description_en 전체], detailed environment, atmospheric lighting" — 엑스트라는 "blurred figure from behind" 등 최소 묘사만
- 모든 프롬프트에 조명, 분위기, 색감, 캐릭터의 감정 상태를 구체적으로 포함
- **프로필에 없는 엑스트라 인물은 "blurred silhouette from behind" 등으로 최소화** — 얼굴/외형을 구체적으로 묘사하면 AI가 주인공과 동일한 외형으로 그려버립니다. 엑스트라는 항상 흐릿하게/뒷모습으로
- **모든 image_prompt에 반드시 포함**: "no text, no subtitles, no captions, no letters, no watermark, no title, not photorealistic, not a photograph" — 이미지 안에 글자 금지 + 실사/사진풍 금지
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
    # 트렌드 힌트가 있으면 Google Search grounding으로 실존 인물/작품 검색
    if "[트렌드 힌트:" in mood:
        from backend.utils.gemini_client import get_api_keys
        from google import genai
        from google.genai import types
        keys = get_api_keys()
        client = genai.Client(api_key=keys[0])
        response = await asyncio.to_thread(
            client.models.generate_content,
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
            )
        )
    else:
        response = await gemini_generate(
            model="gemini-2.5-flash",
            contents=prompt
        )
    data = _parse_json(response.text)
    return {
        "title": data["title"],
        "lyrics": data["lyrics"],
        "music_prompt": data["music_prompt"],
        "vocal_style": data.get("vocal_style", ""),
        "art_style": data.get("art_style", "Pixar-style 3D animation"),
        "characters": data.get("characters", []),
        "hashtags": data.get("hashtags", []),
    }


HASHTAG_PROMPT = """아래 작품 정보를 보고 YouTube/TikTok/Instagram 숏폼 업로드용 해시태그를 10~15개 생성하세요.
반드시 JSON 형식으로만 응답하세요.

작품 정보:
- 제목: {title}
- 테마: {theme}
- 분위기: {mood}
- 가사: {lyrics}
- 아트 스타일: {art_style}

해시태그 규칙:
- 한국어 + 영어 혼합 (한국어 70%, 영어 30%)
- 카테고리: 테마/소재 키워드, 장르, 분위기, 아트스타일, 트렌드 태그
- #AI뮤직비디오 #애니메이션 #숏폼 #MusicVideo 등 범용 태그 3~4개 포함
- 각 해시태그는 #으로 시작

출력 형식:
{{"hashtags": ["#태그1", "#태그2", "..."]}}"""


async def generate_hashtags(title: str, theme: str, mood: str,
                            lyrics: str, art_style: str) -> list[str]:
    """기존 작품에 대해 해시태그만 별도 생성."""
    prompt = HASHTAG_PROMPT.format(
        title=title, theme=theme, mood=mood,
        lyrics=lyrics[:500], art_style=art_style,
    )
    response = await gemini_generate(
        model="gemini-2.5-flash",
        contents=prompt
    )
    data = _parse_json(response.text)
    return data.get("hashtags", [])


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

    response = await gemini_generate(
        model="gemini-2.5-flash",
        contents=prompt
    )
    data = _parse_json(response.text)
    return [ScriptScene(**s) for s in data["scenes"]]
