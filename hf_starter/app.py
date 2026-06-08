"""
6주차 실습: HuggingFace Space 칼로리 카운터 (LangChain LCEL 버전)
================================================================
음식 사진을 업로드하면
  1) HF Inference API의 이미지 분류 모델로 음식을 인식하고
  2) 그 결과를 LangChain ChatHuggingFace LLM에 넘겨 칼로리/영양소를 추정한 뒤
  3) Gradio UI로 보여준다.

핵심 변경: estimate_calories는 LCEL 체인(prompt | llm | parser)으로 구성한다.
이 파일을 그대로 HuggingFace Space(Gradio SDK)에 올리면 배포된다.

로컬 실행:
    uv run python 00_for_me/src/week06/app.py
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any

import gradio as gr
from gradio_client import utils as _gc_utils  # noqa: E402

# --- workaround: gradio_client의 JSON Schema walker가 bool 스키마를 만나면
# 터지는 버그(#10178) 우회. Label/JSON 컴포넌트가 생성하는
# additionalProperties: true 스키마에서 발생한다.
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
from huggingface_hub import InferenceClient
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_huggingface import ChatHuggingFace, HuggingFaceEndpoint
from PIL import Image

from model_config import LLM_MODEL, VISION_MODEL, get_token

load_dotenv()

TOP_K = 3
SYSTEM_PROMPT = (
    "너는 영양사 AI다. 사용자는 음식 이미지 분류기의 top-k 결과(label, score)를 줄 것이다.\n"
    "가장 가능성 높은 음식 1인분 기준으로 칼로리(kcal)와 주요 영양소(탄수화물/단백질/지방, g)를 추정하라.\n"
    "추정이 불확실하면 그 사실을 'note' 필드에 명시하라.\n"
    "반드시 아래 JSON 스키마만 출력하고 다른 텍스트/마크다운/코드블록 금지.\n"
    '{{"food": str, "confidence": float, "calories_kcal": int, '
    '"carbs_g": int, "protein_g": int, "fat_g": int, "note": str}}'
)


# -----------------------------------------------------------------------------
# 클라이언트 / 체인 lazy init
# -----------------------------------------------------------------------------
_vision_client: InferenceClient | None = None
_chain = None


def _vision_lazy() -> InferenceClient:
    global _vision_client
    if _vision_client is None:
        _vision_client = InferenceClient(token=get_token())
    return _vision_client


def _chain_lazy():
    """LCEL 체인: prompt | ChatHuggingFace | JsonOutputParser"""
    global _chain
    if _chain is None:
        endpoint = HuggingFaceEndpoint(
            repo_id=LLM_MODEL,
            task="text-generation",
            max_new_tokens=300,
            temperature=0.2,
            huggingfacehub_api_token=get_token(),
        )
        llm = ChatHuggingFace(llm=endpoint)
        prompt = ChatPromptTemplate.from_messages(
            [
                ("system", SYSTEM_PROMPT),
                ("human", "다음은 이미지 분류기의 top-k 결과다:\n{labels_json}"),
            ]
        )
        _chain = prompt | llm | JsonOutputParser()
    return _chain


# -----------------------------------------------------------------------------
# Step 1: 이미지 분류 (LangChain 추상화 없음 — InferenceClient 직접 사용)
# -----------------------------------------------------------------------------
def classify_food(image: Image.Image) -> list[dict[str, Any]]:
    client = _vision_lazy()
    # hf-inference 라우터가 Content-Type 헤더를 요구하므로
    # 확장자가 있는 임시 파일 경로로 전달한다 (mimetype 자동 감지).
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        image.convert("RGB").save(tmp, format="JPEG")
        tmp_path = tmp.name
    try:
        raw = client.image_classification(tmp_path, model=VISION_MODEL)
    finally:
        os.unlink(tmp_path)
    results: list[dict[str, Any]] = []
    for item in raw[:TOP_K]:
        if isinstance(item, dict):
            results.append({"label": item["label"], "score": float(item["score"])})
        else:
            results.append({"label": item.label, "score": float(item.score)})
    return results


# -----------------------------------------------------------------------------
# Step 2: 칼로리/영양소 추정 (LCEL 체인)
# -----------------------------------------------------------------------------
def estimate_calories(labels: list[dict[str, Any]]) -> dict[str, Any]:
    chain = _chain_lazy()
    labels_json = json.dumps(labels, ensure_ascii=False)
    try:
        return chain.invoke({"labels_json": labels_json})
    except Exception as e:
        return {
            "food": labels[0]["label"] if labels else "unknown",
            "confidence": labels[0]["score"] if labels else 0.0,
            "calories_kcal": 0,
            "carbs_g": 0,
            "protein_g": 0,
            "fat_g": 0,
            "note": f"체인 실행 실패: {type(e).__name__}: {str(e)[:120]}",
        }


# -----------------------------------------------------------------------------
# Step 3: Gradio 콜백
# -----------------------------------------------------------------------------
def analyze(image):
    if image is None:
        return {}, {"error": "이미지를 먼저 업로드해 주세요."}
    labels = classify_food(image)
    label_view = {item["label"]: item["score"] for item in labels}
    nutrition = estimate_calories(labels)
    return label_view, nutrition


# -----------------------------------------------------------------------------
# Step 4: UI
# -----------------------------------------------------------------------------
def build_ui() -> gr.Interface:
    return gr.Interface(
        fn=analyze,
        inputs=gr.Image(type="pil", label="음식 사진 업로드"),
        outputs=[
            gr.Label(num_top_classes=TOP_K, label="음식 분류 결과"),
            gr.JSON(label="칼로리 & 영양소 추정"),
        ],
        title="🍱 Calorie Counter (LangChain LCEL)",
        description=(
            "음식 사진을 업로드하면 HF Inference API로 음식을 인식하고, "
            "LangChain LCEL 체인이 1인분 기준 칼로리/영양소를 추정합니다. "
            "결과는 참고용입니다."
        ),
        flagging_mode="never",
    )


# 모듈 레벨 demo (Space/HF 런타임 호환)
demo = build_ui()

if __name__ == "__main__":
    # HF Space에서는 SPACE_ID 환경변수가 설정돼 있어 0.0.0.0 바인딩이 필요하다.
    # 로컬에서는 127.0.0.1.
    is_space = bool(os.getenv("SPACE_ID"))
    demo.launch(
        server_name="0.0.0.0" if is_space else "127.0.0.1",
        server_port=int(os.getenv("PORT", 7860)),
        show_api=False,
        ssr_mode=False,
    )
