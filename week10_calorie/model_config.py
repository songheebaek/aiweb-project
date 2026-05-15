"""
6주차 모델 설정 — HuggingFace Inference API
===========================================
- VISION_MODEL: 음식 이미지 분류 모델
- LLM_MODEL:    분류 결과를 받아 칼로리/영양소를 추정하는 텍스트 LLM

토큰은 .env 파일의 HUGGINGFACEHUB_API_TOKEN 또는 HF_TOKEN 환경변수에서 읽는다.
HF Space에 배포할 때는 Space의 Settings > Secrets 에서 HF_TOKEN 을 등록한다.
"""

from __future__ import annotations

import os

from huggingface_hub import InferenceClient

# -----------------------------------------------------------------------------
# 모델 선택
# -----------------------------------------------------------------------------
# 음식 이미지 분류 (Vision Transformer, food-101 파인튜닝)
VISION_MODEL = "nateraw/food"

# 칼로리/영양소 추정용 텍스트 LLM (무료 티어 호환성 좋은 instruct 모델)
LLM_MODEL = "meta-llama/Meta-Llama-3-8B-Instruct"


def get_token() -> str:
    """환경변수에서 HF 토큰을 읽는다 (이미지 분류 + LangChain LLM 공통)."""
    token = os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACEHUB_API_TOKEN")
    if not token:
        raise SystemExit(
            "HF_TOKEN(또는 HUGGINGFACEHUB_API_TOKEN) 환경변수가 비어 있습니다.\n"
            "  1) https://huggingface.co/settings/tokens 에서 Read 토큰 발급\n"
            "  2) 로컬: .env 에 HF_TOKEN=hf_xxx 추가\n"
            "  3) HF Space: Settings > Secrets 에 HF_TOKEN 등록"
        )
    return token


def get_client() -> InferenceClient:
    """이미지 분류용 InferenceClient (LangChain 추상화 영역 아님)."""
    return InferenceClient(token=get_token())
