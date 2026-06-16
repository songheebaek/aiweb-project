"""OpenAI GPT 모델 설정 + 요약/핵심/Q&A 프롬프트 체인.

OpenAI 공식 권장 흐름인 Responses API를 사용한다. 요약/하이라이트는
Structured Outputs(JSON Schema)로 받아서 summary/highlights 누락을 줄인다.
"""

import json
import os
import time

from openai import APIError, OpenAI

LLM_PROVIDER_NAME = "OpenAI"
LLM_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.5")

# 일시적 서버 오류만 재시도. 429는 같은 모델 재시도로 바로 해결되기 어려우므로
# 불필요한 비용/레이트 소모를 피하기 위해 즉시 실패시킨다.
_RETRYABLE = {500, 502, 503, 504}
_client = None

_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "한국어 Markdown 요약. 반드시 ## 제목, - 불릿, **강조**를 포함한다.",
        },
        "highlights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "integer"},
                    "point": {"type": "string"},
                },
                "required": ["start", "point"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "highlights"],
    "additionalProperties": False,
}


def get_client():
    """OpenAI client 싱글톤 반환. 키 없으면 친절한 에러."""
    global _client
    if _client is not None:
        return _client

    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError(
            "OPENAI_API_KEY가 없습니다. .env 또는 배포 Secret에 키를 넣었는지 확인하세요."
        )
    _client = OpenAI()
    return _client


def _generate(prompt: str, *, as_json: bool = False, retries: int = 2) -> str:
    """프롬프트 → OpenAI Responses API 응답 텍스트."""
    client = get_client()
    kwargs = {
        "model": LLM_MODEL,
        "input": prompt,
        "store": False,
    }
    if as_json:
        kwargs["text"] = {
            "format": {
                "type": "json_schema",
                "name": "youtube_summary_response",
                "schema": _SUMMARY_SCHEMA,
                "strict": True,
            }
        }

    last_err = None
    for attempt in range(retries):
        try:
            print(
                f"[DIAG] openai: responses.create 시작 "
                f"(model={LLM_MODEL}, as_json={as_json}, attempt={attempt + 1}/{retries})",
                flush=True,
            )
            response = client.responses.create(**kwargs)
            text = response.output_text or ""
            print(
                f"[DIAG] openai: responses.create 반환 "
                f"(model={LLM_MODEL}, response_chars={len(text)})",
                flush=True,
            )
            return text
        except APIError as e:
            last_err = e
            code = getattr(e, "status_code", None) or getattr(e, "code", None)
            print(
                f"[DIAG] openai: APIError "
                f"(code={code}, attempt={attempt + 1}/{retries}, error={type(e).__name__})",
                flush=True,
            )
            if code == 429 or code not in _RETRYABLE:
                raise
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
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
                    "[DIAG] openai: JSON 뒤 추가 텍스트 무시 "
                    f"(suffix_chars={len(suffix)}, suffix_prefix={suffix[:120]!r})",
                    flush=True,
                )
            return data
        except json.JSONDecodeError as second_error:
            print(
                "[DIAG] openai: JSON 파싱 실패 "
                f"(raw_chars={len(raw)}, first={first_error}, second={second_error}, "
                f"raw_prefix={raw[:300]!r})",
                flush=True,
            )
            return None


def _clean_highlights(items) -> list:
    """[{"start": 초(int), "point": "한 줄"}] 형태로 정제."""
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        try:
            out.append({"start": int(item["start"]), "point": str(item["point"]).strip()})
        except (KeyError, ValueError, TypeError):
            continue
    return out


def _format_summary_markdown(summary: str) -> str:
    """모델이 산문형으로 반환해도 UI에서 Markdown 구조로 보이게 보정."""
    summary = (summary or "").strip()
    if not summary:
        return ""

    markdown_markers = ("# ", "## ", "### ", "- ", "* ", "1. ", "> ")
    if any(line.lstrip().startswith(markdown_markers) for line in summary.splitlines()):
        return summary

    lines = [line.strip() for line in summary.splitlines() if line.strip()]
    if not lines:
        return summary

    intro = []
    bullets = []
    for line in lines:
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
    """전체 요약 + 타임스탬프 핵심을 한 번의 GPT 호출로 생성."""
    prompt = (
        "당신은 한국어 영상 요약 전문가입니다. 아래는 [분:초] 타임스탬프가 붙은 유튜브 영상 자막입니다.\n"
        "다음 두 가지를 생성해 JSON Schema에 맞게 응답하세요.\n\n"
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
    print(f"[DIAG] summarize: OpenAI 원문 응답 수신 (chars={len(raw or '')})", flush=True)
    data = _parse_json(raw)
    if not isinstance(data, dict):
        raise RuntimeError("OpenAI 요약 응답이 JSON 객체가 아닙니다. 로그의 JSON 파싱 실패 원문 prefix를 확인하세요.")

    summary = _format_summary_markdown(str(data.get("summary", "")).strip())
    highlights = _clean_highlights(data.get("highlights", []))
    print(
        f"[DIAG] summarize: JSON 파싱 성공 "
        f"(provider=openai, keys={sorted(data.keys())}, summary_chars={len(summary)}, "
        f"raw_highlights_type={type(data.get('highlights')).__name__}, clean_highlights={len(highlights)})",
        flush=True,
    )

    if not summary:
        raise RuntimeError(
            "OpenAI 요약 응답의 summary가 비어 있습니다. "
            f"응답 키={sorted(data.keys())}, 원문 prefix={(raw or '')[:300]!r}"
        )

    return {"summary": summary, "highlights": highlights}


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
