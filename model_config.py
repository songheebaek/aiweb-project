"""Gemini 모델 설정 + 요약/핵심/Q&A 프롬프트 체인.

스택을 한 곳에 모아두는 패턴(week10 model_config.py)을 그대로 유지.
앱(app.py)은 UI만 담당하고, AI 호출은 전부 여기를 거친다.
"""

import json
import os
import time

from google import genai  # 신 공식 SDK (google-genai). 구 google-generativeai는 EOL.
from google.genai import types
from google.genai import errors as genai_errors

# 일시적 서버 오류 시 재시도할 HTTP 상태 (무료 티어 과부하 503, 레이트리밋 429 등)
_RETRYABLE = {429, 500, 503}

# 무료 티어 모델. 기본 gemini-2.5-flash. 다른 모델로 바꾸려면 .env의 GEMINI_MODEL 사용.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# Gemini Flash 컨텍스트는 100만 토큰 → 웬만한 장편 영상 자막도 통째로 들어감.
# 따라서 청크 분할 없이 전체 자막을 한 번에 전달.

_client = None


def get_client():
    """genai.Client 싱글톤 반환. 키 없으면 친절한 에러."""
    global _client
    if _client is not None:
        return _client

    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GOOGLE_API_KEY가 없습니다. .env 파일에 키를 넣었는지 확인하세요. "
            "(https://aistudio.google.com/apikey 에서 무료 발급)"
        )
    _client = genai.Client(api_key=api_key)
    return _client


def _generate(prompt: str, as_json: bool = False, retries: int = 3) -> str:
    """프롬프트 → Gemini 응답 텍스트. 일시적 503/429는 백오프 후 재시도."""
    client = get_client()
    config = (
        types.GenerateContentConfig(response_mime_type="application/json")
        if as_json
        else None
    )
    for attempt in range(retries):
        try:
            resp = client.models.generate_content(
                model=GEMINI_MODEL, contents=prompt, config=config
            )
            return resp.text
        except genai_errors.APIError as e:
            if getattr(e, "code", None) in _RETRYABLE and attempt < retries - 1:
                time.sleep(2 * (attempt + 1))  # 2s, 4s 백오프
                continue
            raise


def summarize_video(transcript_text: str) -> str:
    """영상 전체를 3~5문단 한국어 요약으로."""
    prompt = (
        "당신은 한국어 영상 요약 전문가입니다. 아래는 유튜브 영상의 자막입니다.\n"
        "이 영상의 핵심 내용을 한국어로 3~5문단으로 요약하세요.\n"
        "- 첫 문단은 영상이 무엇에 대한 것인지 한 문장으로 시작\n"
        "- 사실에 근거하고, 자막에 없는 내용은 지어내지 말 것\n"
        "- 마크다운으로 읽기 좋게 정리\n\n"
        f"=== 자막 ===\n{transcript_text}"
    )
    return _generate(prompt)


def extract_highlights(transcript_text: str) -> list:
    """타임스탬프별 핵심 포인트를 [{"start": 초(int), "point": "한 줄"}] 리스트로.

    UI에서 '초록 시각 + 텍스트' 행으로 렌더링하기 위해 마크다운 대신 구조화 JSON으로 받음.
    """
    prompt = (
        "아래는 [분:초] 타임스탬프가 붙은 유튜브 영상 자막입니다.\n"
        "영상을 흐름에 따라 5~10개의 핵심 구간으로 나누세요.\n"
        "오직 JSON 배열로만 응답하세요. 각 항목 형식:\n"
        '{"start": 시작초(정수), "point": "핵심 내용 한 줄(한국어)"}\n\n'
        "규칙:\n"
        "- start는 자막에 실제로 등장한 시각의 '초' 단위 정수\n"
        "- 시간 순서대로 정렬\n"
        "- 자막에 없는 내용은 추가 금지\n\n"
        f"=== 자막 ===\n{transcript_text}"
    )
    raw = (_generate(prompt, as_json=True) or "").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # 혹시 코드펜스(```json ... ```)로 감싸 오면 벗겨내고 재시도
        cleaned = raw.strip("`").lstrip("json").strip()
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            return []

    items = data if isinstance(data, list) else data.get("highlights", [])
    out = []
    for it in items:
        try:
            out.append({"start": int(it["start"]), "point": str(it["point"]).strip()})
        except (KeyError, ValueError, TypeError):
            continue
    return out


def answer_question(transcript_text: str, question: str, history: list) -> str:
    """자막 내용에 근거해 사용자 질문에 답변 (이전 대화 맥락 포함)."""
    history_text = ""
    for turn in history:
        role = "사용자" if turn["role"] == "user" else "도우미"
        history_text += f"{role}: {turn['content']}\n"

    prompt = (
        "당신은 아래 유튜브 영상 자막에 대해 질문에 답하는 도우미입니다.\n"
        "반드시 자막 내용에 근거해서만 한국어로 답하세요. "
        "자막에 없는 내용이면 '영상에서 다루지 않았습니다'라고 솔직히 말하세요.\n\n"
        f"=== 자막 ===\n{transcript_text}\n\n"
        f"=== 이전 대화 ===\n{history_text}\n"
        f"=== 질문 ===\n{question}"
    )
    return _generate(prompt)
