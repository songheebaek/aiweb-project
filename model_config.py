"""
11주차 모델 설정 — OpenAI API
=============================
- LLM_MODEL:   스트레스 텍스트를 분석해서 몬스터 카드 JSON을 만드는 텍스트 LLM
- IMAGE_MODEL: 카드에 들어갈 몬스터 캐릭터 이미지를 생성하는 text-to-image 모델

토큰은 .env 파일의 OPENAI_API_KEY 환경변수에서 읽는다.
HF Space에 배포할 때는 Space의 Settings > Secrets 에서 OPENAI_API_KEY 를 등록한다.
"""

from __future__ import annotations

import os

# -----------------------------------------------------------------------------
# 모델 선택
# -----------------------------------------------------------------------------
# 스트레스 분석용 텍스트 LLM (한국어 잘하고 JSON mode 지원, 저렴)
LLM_MODEL = "gpt-4o-mini"

# 몬스터 캐릭터 이미지 생성용 (DALL·E 3: 인증 불필요, 카툰 스타일 안정적, $0.04/장)
# 조직 verification 받은 계정이면 "gpt-image-1"로 바꿔도 됨 (더 신선한 결과)
IMAGE_MODEL = "dall-e-3"


def get_token() -> str:
    """환경변수에서 OpenAI API 키를 읽는다 (LLM + 이미지 공통)."""
    token = os.getenv("OPENAI_API_KEY")
    if not token:
        raise SystemExit(
            "OPENAI_API_KEY 환경변수가 비어 있습니다.\n"
            "  1) https://platform.openai.com/api-keys 에서 키 발급\n"
            "  2) 로컬: .env 에 OPENAI_API_KEY=sk-... 추가\n"
            "  3) HF Space: Settings > Secrets 에 OPENAI_API_KEY 등록"
        )
    return token
