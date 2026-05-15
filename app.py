"""
11주차 실습: 나의 스트레스 몬스터 (LangChain LCEL + OpenAI)
==========================================================
사용자가 오늘의 스트레스를 한 문장 이상 입력하면
  1) ChatOpenAI(gpt-4o-mini) LCEL 체인이 JSON 모드로 스트레스 유형/몬스터 이름/특징/이미지 프롬프트 생성
  2) OpenAI Images API(DALL·E 3)가 몬스터 캐릭터 이미지를 생성
  3) Gradio UI로 "오늘의 스트레스 몬스터 카드"를 보여준다.

10주차 칼로리카운터의 LCEL 패턴(prompt | llm | parser)을 그대로 재활용한다.
"""

from __future__ import annotations

import base64
import os
import re
from datetime import datetime, timedelta
from io import BytesIO
from typing import Any

import gradio as gr
import openai
from gradio_client import utils as _gc_utils  # noqa: E402

# --- workaround: gradio_client의 JSON Schema walker가 bool 스키마를 만나면
# 터지는 버그(#10178) 우회.
_orig_get_type = _gc_utils.get_type
def _safe_get_type(schema):
    if isinstance(schema, bool):
        return "Any"
    return _orig_get_type(schema)
_gc_utils.get_type = _safe_get_type

_orig_j2p = _gc_utils._json_schema_to_python_type
def _safe_j2p(schema, defs=None):
    if isinstance(schema, bool):
        return "Any"
    return _orig_j2p(schema, defs)
_gc_utils._json_schema_to_python_type = _safe_j2p

from dotenv import load_dotenv
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from openai import OpenAI
from PIL import Image

from model_config import IMAGE_MODEL, LLM_MODEL, get_token

load_dotenv()

SYSTEM_PROMPT = (
    "너는 사용자의 오늘 스트레스 문장을 읽고 귀엽고 웃긴 '스트레스 몬스터' 카드를 만드는 분석가다.\n"
    "사용자를 비판하지 말고, 스트레스를 외재화해서 친근한 캐릭터로 만들어라.\n"
    "위트있고 따뜻한 톤. 너무 진지하거나 의학적이지 않게.\n"
    "반드시 아래 JSON 스키마만 출력하고 다른 텍스트/마크다운/코드블록은 절대 금지.\n"
    '{{"stress_type": str (예: 업무/인간관계/학업/건강/재정/미래 불안 등), '
    '"stress_index": int (1~10), '
    '"monster_name": str (한국어, 귀엽고 웃긴 이름. 예: 마감지옥 슬라임, 눈치왕 두꺼비), '
    '"monster_species": str (예: 슬라임, 도깨비, 거미, 곰팡이), '
    '"traits": [str, str, str], '
    '"weakness": str (몬스터를 약화시키는 방법 = 사용자에게 도움 되는 짧은 조언), '
    '"description": str (이 몬스터에 대한 2~3문장 위트있는 설명), '
    '"image_prompt": str (반드시 영어, SFW. 몬스터 외형/색감/표정을 묘사. '
    '"chibi style, pastel colors, white background, full body, kawaii, cute cartoon monster" 키워드 포함)'
    "}}"
)


# -----------------------------------------------------------------------------
# 클라이언트 / 체인 lazy init
# -----------------------------------------------------------------------------
_image_client: OpenAI | None = None
_chain = None


def _image_lazy() -> OpenAI:
    global _image_client
    if _image_client is None:
        _image_client = OpenAI(api_key=get_token())
    return _image_client


def _chain_lazy():
    """LCEL 체인: prompt | ChatOpenAI(JSON mode) | JsonOutputParser"""
    global _chain
    if _chain is None:
        llm = ChatOpenAI(
            model=LLM_MODEL,
            temperature=0.8,
            api_key=get_token(),
            model_kwargs={"response_format": {"type": "json_object"}},
        )
        prompt = ChatPromptTemplate.from_messages(
            [
                ("system", SYSTEM_PROMPT),
                ("human", "오늘의 스트레스:\n{stress_text}"),
            ]
        )
        _chain = prompt | llm | JsonOutputParser()
    return _chain


# -----------------------------------------------------------------------------
# Quota / Rate-limit 메시지 포맷터
# -----------------------------------------------------------------------------
_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h)")


def _parse_openai_duration(s: str | None) -> float | None:
    """OpenAI 헤더의 '6m0s', '20ms' 같은 duration 문자열 → 초."""
    if not s:
        return None
    total = 0.0
    for value, unit in _DURATION_RE.findall(s):
        v = float(value)
        total += {"ms": v / 1000, "s": v, "m": v * 60, "h": v * 3600}[unit]
    return total or None


def _next_month_first_local() -> datetime:
    now = datetime.now().astimezone()
    if now.month == 12:
        return now.replace(year=now.year + 1, month=1, day=1,
                           hour=0, minute=0, second=0, microsecond=0)
    return now.replace(month=now.month + 1, day=1,
                       hour=0, minute=0, second=0, microsecond=0)


def format_quota_message(exc: openai.RateLimitError) -> tuple[str, str]:
    """RateLimitError → (토스트 한 줄, 마크다운 본문). 한국어."""
    # 에러 코드 추출
    code = ""
    try:
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            code = (body.get("error") or {}).get("code") or ""
    except Exception:
        pass

    # 1) 결제 한도 소진 — 자동 reset 없음, 다음 결제 주기 추정
    if code == "insufficient_quota":
        nxt = _next_month_first_local()
        eta = nxt.strftime("%Y년 %-m월 %-d일 %H:%M") if os.name != "nt" \
            else nxt.strftime("%Y년 %#m월 %#d일 %H:%M")
        toast = f"🪙 OpenAI API 한도 소진 — {eta}쯤 다시 만나요!"
        md = (
            "## 🪙 OpenAI API 한도가 소진됐어\n\n"
            f"이번 결제 주기의 API 한도가 끝났어. (`insufficient_quota`)\n\n"
            f"**다음 reset 예상**: `{eta}` (월 결제 주기 기준)\n\n"
            "👉 그 때 다시 만나자! 🌙\n\n"
            "결제/사용량 확인: https://platform.openai.com/account/billing/overview"
        )
        return toast, md

    # 2) RPM/TPM rate limit — retry-after 헤더 파싱
    retry_after_s: float | None = None
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is not None:
        for h in ("retry-after", "x-ratelimit-reset-requests",
                  "x-ratelimit-reset-tokens"):
            raw = headers.get(h)
            if not raw:
                continue
            try:
                retry_after_s = float(raw)
            except (TypeError, ValueError):
                retry_after_s = _parse_openai_duration(raw)
            if retry_after_s:
                break

    if retry_after_s and retry_after_s > 0:
        eta_dt = datetime.now().astimezone() + timedelta(seconds=retry_after_s)
        secs = int(retry_after_s)
        eta_str = eta_dt.strftime("%H:%M:%S")
        toast = f"⏳ 잠깐 쉬는 중 — {secs}초 뒤({eta_str})에 다시 만나요!"
        md = (
            "## ⏳ 잠깐 쉬어가는 중\n\n"
            "호출이 너무 빨라서 OpenAI가 잠시 쉬라고 하네.\n\n"
            f"**{secs}초 뒤** (`{eta_str}`)에 다시 시도해줘! 🌙"
        )
        return toast, md

    # 3) 기타 (헤더 못 읽었을 때 fallback)
    toast = "⏳ Rate limit — 1분쯤 뒤에 다시 만나요!"
    md = (
        "## ⏳ 잠깐 쉬어가는 중\n\n"
        "OpenAI가 잠시 숨고르는 중이야. **1분 후** 다시 시도해줘!"
    )
    return toast, md


# -----------------------------------------------------------------------------
# Step 1: 스트레스 분석 (LCEL 체인)
# -----------------------------------------------------------------------------
def analyze_stress(stress_text: str) -> dict[str, Any]:
    return _chain_lazy().invoke({"stress_text": stress_text})


# -----------------------------------------------------------------------------
# Step 2: 몬스터 이미지 생성 (OpenAI Images API)
# -----------------------------------------------------------------------------
def generate_monster_image(image_prompt: str) -> Image.Image:
    client = _image_lazy()
    kwargs: dict[str, Any] = {
        "model": IMAGE_MODEL,
        "prompt": image_prompt,
        "size": "1024x1024",
        "n": 1,
    }
    # dall-e-3은 response_format으로 b64_json 선택 가능. gpt-image-1은 항상 b64_json.
    if IMAGE_MODEL == "dall-e-3":
        kwargs["response_format"] = "b64_json"
        kwargs["style"] = "vivid"
    resp = client.images.generate(**kwargs)
    b64 = resp.data[0].b64_json
    return Image.open(BytesIO(base64.b64decode(b64)))


# -----------------------------------------------------------------------------
# Step 3: 카드 마크다운 렌더링
# -----------------------------------------------------------------------------
def card_to_markdown(card: dict[str, Any]) -> str:
    name = card.get("monster_name", "이름 없는 몬스터")
    species = card.get("monster_species", "")
    stress_type = card.get("stress_type", "")
    try:
        idx = int(card.get("stress_index", 0))
    except (TypeError, ValueError):
        idx = 0
    idx = max(0, min(idx, 10))
    bar = "█" * idx + "░" * (10 - idx)

    lines = [
        f"## 🪪 {name}",
        f"**종족**: {species}  ·  **유형**: {stress_type}",
        f"**스트레스 지수** `{idx}/10`  {bar}",
        "",
        "**특징**",
    ]
    for t in card.get("traits", []):
        lines.append(f"- {t}")
    weakness = card.get("weakness")
    if weakness:
        lines.append("")
        lines.append(f"**약점 (= 너의 무기)**: {weakness}")
    description = card.get("description")
    if description:
        lines.append("")
        lines.append(f"> {description}")
    return "\n".join(lines)


# -----------------------------------------------------------------------------
# Step 4: Gradio 콜백
# -----------------------------------------------------------------------------
def summon_monster(stress_text: str):
    if not stress_text or len(stress_text.strip()) < 5:
        return None, "⚠️ 스트레스를 한 문장 이상 적어주세요.", {}

    # Step 1: 스트레스 분석
    try:
        card = analyze_stress(stress_text)
    except openai.RateLimitError as e:
        toast, md = format_quota_message(e)
        gr.Warning(toast)
        return None, md, {"error": str(e)[:300]}
    except Exception as e:
        return (
            None,
            f"## ❌ 분석 실패\n\n`{type(e).__name__}`: {str(e)[:200]}",
            {"error": str(e)[:300]},
        )

    # Step 2: 몬스터 이미지 생성
    fallback_prompt = (
        "a cute chibi stress monster, kawaii, pastel colors, "
        "white background, full body, cute cartoon"
    )
    try:
        image = generate_monster_image(card.get("image_prompt") or fallback_prompt)
    except openai.RateLimitError as e:
        toast, md_quota = format_quota_message(e)
        gr.Warning(toast)
        # 분석 카드 + 이미지 한도 메시지 같이 보여줌
        md = card_to_markdown(card) + "\n\n---\n\n" + md_quota
        return None, md, card
    except Exception as e:
        card["image_error"] = f"{type(e).__name__}: {str(e)[:120]}"
        return None, card_to_markdown(card), card

    return image, card_to_markdown(card), card


# -----------------------------------------------------------------------------
# Step 5: UI
# -----------------------------------------------------------------------------
def build_ui() -> gr.Blocks:
    with gr.Blocks(title="나의 스트레스 몬스터", theme=gr.themes.Soft()) as demo:
        gr.Markdown(
            "# 👿 나의 스트레스 몬스터\n"
            "오늘 너를 괴롭히는 스트레스를 한 문장 이상 적어줘.  \n"
            "AI가 그 녀석의 정체를 밝히고 캐릭터 카드를 만들어줄게."
        )
        with gr.Row():
            with gr.Column(scale=1):
                stress_input = gr.Textbox(
                    label="오늘의 스트레스",
                    lines=6,
                    placeholder=(
                        "예) 내일까지 발표 자료 만들어야 하는데 한 글자도 못 썼어. "
                        "머리는 멍하고 자꾸 유튜브만 봐..."
                    ),
                )
                submit = gr.Button("🔮 몬스터 생성하기", variant="primary")
                gr.Examples(
                    examples=[
                        "팀장님이 자꾸 일을 막판에 던져. 퇴근 30분 전마다 새로운 일이 떨어져서 도저히 정시퇴근을 못해.",
                        "시험 D-3인데 책도 안 폈어. 자꾸 SNS만 보다가 새벽 3시야.",
                        "친구한테 답장이 안 와. 내가 뭐 잘못한 건가 계속 카톡 다시 읽어보게 돼.",
                    ],
                    inputs=stress_input,
                )
            with gr.Column(scale=1):
                image_out = gr.Image(label="스트레스 몬스터", type="pil")
                card_md = gr.Markdown()
                with gr.Accordion("🛠️ 분석 원본 JSON", open=False):
                    debug = gr.JSON(label="raw card")

        submit.click(
            fn=summon_monster,
            inputs=stress_input,
            outputs=[image_out, card_md, debug],
        )
    return demo


# 모듈 레벨 demo (Space/HF 런타임 호환)
demo = build_ui()

if __name__ == "__main__":
    is_space = bool(os.getenv("SPACE_ID"))
    demo.launch(
        server_name="0.0.0.0" if is_space else "127.0.0.1",
        server_port=int(os.getenv("PORT", 7860)),
        show_api=False,
        ssr_mode=False,
    )
