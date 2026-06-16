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

# 기본 모델 gemini-3.5-flash(안정·비프리뷰). 다른 모델로 바꾸려면 .env의 GEMINI_MODEL 사용.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")

# 기본 모델이 503(과부하)로 계속 실패하면 폴백할 모델.
# 폴백은 gemini-3-flash-preview.
_FALLBACK_MODELS = [m for m in [GEMINI_MODEL, "gemini-3-flash-preview"] if m]
_FALLBACK_MODELS = list(dict.fromkeys(_FALLBACK_MODELS))  # 중복 제거(순서 유지)

LLM_PROVIDER_NAME = "Gemini"
LLM_MODEL = GEMINI_MODEL

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
                print(
                    f"[DIAG] gemini: generate_content 시작 "
                    f"(model={model}, as_json={as_json}, attempt={attempt + 1}/{retries})",
                    flush=True,
                )
                resp = client.models.generate_content(
                    model=model, contents=prompt, config=config
                )
                text = resp.text or ""
                print(
                    f"[DIAG] gemini: generate_content 반환 "
                    f"(model={model}, response_chars={len(text)})",
                    flush=True,
                )
                return text
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


def _strip_code_fence(raw: str) -> str:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    return raw


def _parse_json(raw: str):
    """모델 JSON 응답 파싱.

    Gemini/OpenAI가 가끔 정상 JSON 뒤에 코드펜스/공백/설명 조각을 덧붙이면
    json.loads()는 Extra data로 실패한다. 이 경우 JSONDecoder.raw_decode로
    첫 번째 JSON 객체를 추출해 사용한다.
    """
    raw = _strip_code_fence(raw)
    decoder = json.JSONDecoder()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as first_error:
        try:
            data, end_idx = decoder.raw_decode(raw)
            suffix = raw[end_idx:].strip()
            if suffix:
                print(
                    "[DIAG] gemini: JSON 뒤 추가 텍스트 무시 "
                    f"(suffix_chars={len(suffix)}, suffix_prefix={suffix[:120]!r})",
                    flush=True,
                )
            return data
        except json.JSONDecodeError as second_error:
            print(
                "[DIAG] gemini: JSON 파싱 실패 "
                f"(raw_chars={len(raw)}, first={first_error}, second={second_error}, "
                f"raw_prefix={raw[:300]!r})",
                flush=True,
            )
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


def _format_summary_markdown(summary: str) -> str:
    """Ensure summary renders as structured Markdown even when the model returns prose."""
    summary = (summary or "").strip()
    if not summary:
        return ""

    # If the model already produced clear Markdown structure, preserve it.
    markdown_markers = ("# ", "## ", "### ", "- ", "* ", "1. ", "> ")
    if any(line.lstrip().startswith(markdown_markers) for line in summary.splitlines()):
        return summary

    lines = [line.strip() for line in summary.splitlines() if line.strip()]
    if not lines:
        return summary

    intro = []
    bullets = []
    for line in lines:
        # Convert Korean label-style prose like "부실한 정비: 내용" to bullets.
        if ":" in line or "：" in line:
            sep = ":" if ":" in line else "："
            title, body = line.split(sep, 1)
            title = title.strip().strip("-•* ")
            body = body.strip()
            if title and body and len(title) <= 40:
                bullets.append(f"- **{title}**: {body}")
                continue
        intro.append(line)

    sections = ["## 핵심 요약", ""]
    if intro:
        sections.extend(intro[:2])
    if bullets:
        if intro:
            sections.append("")
        sections.append("## 주요 내용")
        sections.append("")
        sections.extend(bullets)
    elif len(intro) > 2:
        sections.append("")
        sections.append("## 세부 내용")
        sections.append("")
        sections.extend(f"- {line}" for line in intro[2:])

    return "\n".join(sections).strip()


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
        '  "summary": "한국어 Markdown 요약. 반드시 ## 제목, - 불릿, **강조**를 포함",\n'
        '  "highlights": [{"start": 시작초(정수), "point": "핵심 한 줄(한국어)"}]\n'
        "}\n\n"
        "summary 규칙:\n"
        "- 반드시 Markdown 문법을 사용\n"
        "- 첫 줄은 '## 핵심 요약' 제목으로 시작\n"
        "- 다음에 영상 주제를 1~2문장으로 설명\n"
        "- 그 다음 '## 주요 내용' 제목 아래에 3~6개의 '- **항목명**: 설명' 불릿 작성\n"
        "- 필요한 경우 '## 쟁점/결론' 제목을 추가\n"
        "- 사실에 근거하고, 자막에 없는 내용은 지어내지 말 것\n\n"
        "highlights 규칙:\n"
        "- 영상을 흐름에 따라 5~10개의 핵심 구간으로 나눔\n"
        "- start는 자막에 실제로 등장한 시각의 '초' 단위 정수, 시간 순서대로 정렬\n"
        "- 자막에 없는 내용은 추가 금지\n\n"
        f"=== 자막 ===\n{transcript_text}"
    )
    raw = _generate(prompt, as_json=True)
    print(f"[DIAG] summarize: Gemini 원문 응답 수신 (chars={len(raw or '')})", flush=True)
    data = _parse_json(raw)
    if not isinstance(data, dict):
        raise RuntimeError("Gemini 요약 응답이 JSON 객체가 아닙니다. 로그의 JSON 파싱 실패 원문 prefix를 확인하세요.")

    summary = _format_summary_markdown(str(data.get("summary", "")).strip())
    highlights = _clean_highlights(data.get("highlights", []))
    print(
        f"[DIAG] summarize: JSON 파싱 성공 "
        f"(keys={sorted(data.keys())}, summary_chars={len(summary)}, "
        f"raw_highlights_type={type(data.get('highlights')).__name__}, clean_highlights={len(highlights)})",
        flush=True,
    )

    if not summary:
        raise RuntimeError(
            "Gemini 요약 응답의 summary가 비어 있습니다. "
            f"응답 키={sorted(data.keys())}, 원문 prefix={(raw or '')[:300]!r}"
        )

    return {
        "summary": summary,
        "highlights": highlights,
    }


def answer_question(context_text: str, question: str, history: list) -> str:
    """요약/하이라이트/관련 자막 컨텍스트에 근거해 사용자 질문에 답변."""
    history_text = ""
    for turn in history:
        role = "사용자" if turn["role"] == "user" else "도우미"
        history_text += f"{role}: {turn['content']}\n"

    prompt = (
        "당신은 아래 유튜브 영상 컨텍스트에 대해 질문에 답하는 도우미입니다.\n"
        "반드시 제공된 컨텍스트에 근거해서만 한국어로 답하세요. "
        "답변은 Markdown 형식으로 작성하고, 핵심어는 **굵게** 표시하세요. "
        "여러 항목이면 - 불릿 목록을 사용하세요. "
        "컨텍스트에 없는 내용이면 '제공된 컨텍스트에서는 확인할 수 없습니다'라고 솔직히 말하세요.\n\n"
        f"=== 영상 컨텍스트 ===\n{context_text}\n\n"
        f"=== 이전 대화 ===\n{history_text}\n"
        f"=== 질문 ===\n{question}"
    )
    return _generate(prompt)
