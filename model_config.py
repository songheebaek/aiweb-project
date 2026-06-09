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

# 일시적 서버 오류만 재시도 (503 과부하 등). 429(할당량/레이트리밋 초과)는 재시도해도
# 곧바로 안 풀리고, 적은 무료 한도(RPD/RPM)만 더 깎아먹으므로 재시도하지 않고 폴백 모델로 넘긴다.
_RETRYABLE = {500, 503}

# 무료 티어 모델. 기본 gemini-2.5-flash. 다른 모델로 바꾸려면 .env의 GEMINI_MODEL 사용.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# 기본 모델이 503(과부하)로 계속 실패하면 폴백할 모델들 (둘 다 무료 티어).
_FALLBACK_MODELS = [m for m in [GEMINI_MODEL, "gemini-2.0-flash"] if m]
_FALLBACK_MODELS = list(dict.fromkeys(_FALLBACK_MODELS))  # 중복 제거(순서 유지)

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


def _generate(prompt: str, as_json: bool = False, retries: int = 2) -> str:
    """프롬프트 → Gemini 응답 텍스트.

    일시적 503은 짧게 백오프 후 재시도. 429(할당량 초과)는 같은 모델 재시도가 무의미하고
    무료 한도만 더 깎으므로 재시도 없이 곧장 다음 폴백 모델로 넘긴다(모델별 한도는 별개).
    인증/요청 오류(비재시도성)는 즉시 실패.
    """
    client = get_client()
    config = (
        types.GenerateContentConfig(response_mime_type="application/json")
        if as_json
        else None
    )
    last_err = None
    for model in _FALLBACK_MODELS:
        for attempt in range(retries):
            try:
                resp = client.models.generate_content(
                    model=model, contents=prompt, config=config
                )
                return resp.text
            except genai_errors.APIError as e:
                last_err = e
                code = getattr(e, "code", None)
                if code == 429:
                    break  # 할당량 초과: 재시도 말고 바로 다음 폴백 모델로 (호출/quota 낭비 방지)
                if code not in _RETRYABLE:
                    raise  # 인증/요청 오류 등은 모델 바꿔도 동일 → 즉시 실패
                if attempt < retries - 1:
                    time.sleep(2 * (attempt + 1))  # 2s 백오프 (503 일시 과부하 대비)
        # 이 모델 실패(503 재시도 소진 또는 429) → 다음 폴백 모델 시도
    raise last_err


def _parse_json(raw: str):
    """모델 JSON 응답 파싱. 코드펜스(```json ... ```)로 감싸 오면 벗겨내고 재시도. 실패 시 None."""
    raw = (raw or "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        cleaned = raw.strip("`").lstrip("json").strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return None


def _clean_highlights(items) -> list:
    """[{"start": 초(int), "point": "한 줄"}] 형태로 정제. 잘못된 항목은 건너뜀."""
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        try:
            out.append({"start": int(it["start"]), "point": str(it["point"]).strip()})
        except (KeyError, ValueError, TypeError):
            continue
    return out


def summarize_and_highlight(transcript_text: str) -> dict:
    """전체 요약 + 타임스탬프 핵심을 '한 번의 호출'로 함께 생성 (무료 한도 절약: 2회→1회).

    반환: {"summary": str(마크다운), "highlights": [{"start": int, "point": str}, ...]}
    UI는 summary를 마크다운으로, highlights를 '초록 시각 + 텍스트' 행으로 렌더링한다.
    """
    prompt = (
        "당신은 한국어 영상 요약 전문가입니다. 아래는 [분:초] 타임스탬프가 붙은 유튜브 영상 자막입니다.\n"
        "다음 두 가지를 생성해 '오직 JSON 객체 하나'로만 응답하세요.\n\n"
        "형식:\n"
        "{\n"
        '  "summary": "한국어 요약 (마크다운, 3~5문단)",\n'
        '  "highlights": [{"start": 시작초(정수), "point": "핵심 한 줄(한국어)"}]\n'
        "}\n\n"
        "summary 규칙:\n"
        "- 첫 문단은 영상이 무엇에 대한 것인지 한 문장으로 시작\n"
        "- 사실에 근거하고, 자막에 없는 내용은 지어내지 말 것\n"
        "- 마크다운으로 읽기 좋게 정리\n\n"
        "highlights 규칙:\n"
        "- 영상을 흐름에 따라 5~10개의 핵심 구간으로 나눔\n"
        "- start는 자막에 실제로 등장한 시각의 '초' 단위 정수, 시간 순서대로 정렬\n"
        "- 자막에 없는 내용은 추가 금지\n\n"
        f"=== 자막 ===\n{transcript_text}"
    )
    data = _parse_json(_generate(prompt, as_json=True))
    if not isinstance(data, dict):
        return {"summary": "", "highlights": []}
    return {
        "summary": str(data.get("summary", "")).strip(),
        "highlights": _clean_highlights(data.get("highlights", [])),
    }


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
