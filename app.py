import base64
import json
import os
from io import BytesIO

import streamlit as st
from openai import OpenAI
from PIL import Image

st.set_page_config(
    page_title="나의 스트레스 몬스터",
    page_icon="👿",
    layout="centered",
)

ANALYZER_MODEL = "gpt-4o-mini"
IMAGE_MODEL = "gpt-image-1"

SYSTEM_PROMPT = """너는 사용자의 스트레스 문장을 읽고 귀엽고 웃긴 '스트레스 몬스터' 카드를 만들어주는 분석가야.
사용자의 스트레스 내용을 분석해서 다음을 JSON으로 반환해. 다른 텍스트는 절대 출력하지 마.

{
  "stress_type": "스트레스 유형을 한국어로 짧게 (예: 업무 스트레스, 인간관계 스트레스, 학업 스트레스, 건강 스트레스, 재정 스트레스, 미래 불안, 자기혐오 등)",
  "stress_index": 1~10 사이의 정수,
  "monster_name": "귀엽고 웃긴 한국어 몬스터 이름 (예: '마감지옥 슬라임', '눈치왕 두꺼비')",
  "monster_species": "몬스터의 종족/모티프를 짧게 (예: 슬라임, 도깨비, 거미, 곰팡이)",
  "traits": ["몬스터의 귀엽고 웃긴 특징 3개를 각각 짧은 문장으로"],
  "weakness": "몬스터를 약화시키는 방법을 한 문장으로 (사용자에게 도움 되는 조언 형태)",
  "description": "이 몬스터에 대한 2~3문장의 친근하고 위트있는 설명",
  "image_prompt": "A cute, funny cartoon stress monster character for a card illustration. 영어로 몬스터의 외형/색감/표정을 구체적으로 묘사. 'chibi style, pastel colors, white background, full body, kawaii' 같은 스타일 키워드 포함."
}

규칙:
- 사용자를 비판하지 말고, 스트레스를 외재화해서 귀여운 캐릭터로 만들어줘.
- 위트있고 따뜻한 톤. 너무 진지하거나 의학적이지 않게.
- image_prompt는 반드시 영어로, 안전한(SFW) 묘사로.
"""


def get_client() -> OpenAI:
    api_key = st.session_state.get("api_key") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        st.error("OpenAI API 키가 필요합니다. 사이드바에서 입력해주세요.")
        st.stop()
    return OpenAI(api_key=api_key)


def analyze_stress(client: OpenAI, text: str) -> dict:
    resp = client.chat.completions.create(
        model=ANALYZER_MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        temperature=0.9,
    )
    return json.loads(resp.choices[0].message.content)


def generate_monster_image(client: OpenAI, prompt: str) -> Image.Image:
    resp = client.images.generate(
        model=IMAGE_MODEL,
        prompt=prompt,
        size="1024x1024",
        n=1,
    )
    b64 = resp.data[0].b64_json
    return Image.open(BytesIO(base64.b64decode(b64)))


def render_card(card: dict, image: Image.Image) -> None:
    st.markdown("---")
    st.markdown(f"## 🪪 오늘의 스트레스 몬스터")
    st.image(image, use_container_width=True)

    name = card.get("monster_name", "이름 없는 몬스터")
    species = card.get("monster_species", "")
    stress_type = card.get("stress_type", "")
    stress_index = card.get("stress_index", 0)

    st.markdown(f"### {name}")
    st.caption(f"종족: {species}  ·  유형: {stress_type}")

    st.progress(min(max(int(stress_index), 0), 10) / 10, text=f"스트레스 지수 {stress_index} / 10")

    st.markdown("**특징**")
    for trait in card.get("traits", []):
        st.markdown(f"- {trait}")

    weakness = card.get("weakness")
    if weakness:
        st.markdown("**약점 (= 너의 무기)**")
        st.info(weakness)

    description = card.get("description")
    if description:
        st.markdown("**설명**")
        st.write(description)


with st.sidebar:
    st.markdown("### ⚙️ 설정")
    st.text_input(
        "OpenAI API Key",
        type="password",
        key="api_key",
        help="환경변수 OPENAI_API_KEY가 있으면 비워둬도 됩니다.",
    )
    st.markdown("---")
    st.caption("Made with Streamlit · GPT-4o-mini + gpt-image-1")


st.title("👹 나의 스트레스 몬스터")
st.write("오늘 너를 괴롭히는 스트레스를 한 문장 이상 적어줘. AI가 그 녀석의 정체를 밝혀줄게.")

with st.form("stress_form"):
    user_text = st.text_area(
        "오늘의 스트레스",
        placeholder="예) 내일까지 발표 자료 만들어야 하는데 한 글자도 못 썼어. 머리는 멍하고 자꾸 유튜브만 봐...",
        height=140,
    )
    submitted = st.form_submit_button("🔮 몬스터 소환하기", use_container_width=True)

if submitted:
    if not user_text or len(user_text.strip()) < 5:
        st.warning("스트레스를 조금 더 자세히 적어줘. (최소 한 문장)")
        st.stop()

    client = get_client()

    with st.status("스트레스를 분석하고 있어...", expanded=True) as status:
        st.write("🧠 스트레스 유형 분석 중...")
        try:
            card = analyze_stress(client, user_text)
        except Exception as e:
            status.update(label="분석 실패", state="error")
            st.error(f"분석 중 오류: {e}")
            st.stop()

        st.write(f"🎨 몬스터 그리는 중... ({card.get('monster_name', '?')})")
        try:
            image = generate_monster_image(client, card["image_prompt"])
        except Exception as e:
            status.update(label="이미지 생성 실패", state="error")
            st.error(f"이미지 생성 중 오류: {e}")
            st.stop()

        status.update(label="완성!", state="complete")

    render_card(card, image)

    with st.expander("🛠️ 디버그 (분석 원본 JSON)"):
        st.json(card)
