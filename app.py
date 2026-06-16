"""YouTube 영상 요약기 — Streamlit 프론트엔드.

URL 입력 → 자막 추출(youtube-transcript-api) → Gemini로 요약 / 타임스탬프 핵심 / Q&A.
UI는 프로젝트 썸네일 목업 디자인을 따름 (헤더 → URL바 → 요약|영상 → 타임스탬프|Q&A → 기능 칩).

로컬 실행:  streamlit run app.py
배포:       Dockerfile/docker-compose 참조 (포트 8501)
"""

import os
import random
import re
import time

import requests
import streamlit as st
from dotenv import load_dotenv
from youtube_transcript_api import (
    YouTubeTranscriptApi,
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
    IpBlocked,
    RequestBlocked,
    CouldNotRetrieveTranscript,
)
from youtube_transcript_api.proxies import WebshareProxyConfig

# .env를 먼저 로드해야 model_config 라우터가 LLM_PROVIDER를 올바르게 읽습니다.
load_dotenv(dotenv_path=".env", override=True)

import model_config


def _csv_env(name: str) -> list[str]:
    """Comma-separated env helper. Empty values are ignored."""
    return [value.strip() for value in os.getenv(name, "").split(",") if value.strip()]


def _mask(value: str | None) -> str:
    """Mask non-empty diagnostic values without leaking credentials."""
    if not value:
        return "<absent>"
    if len(value) <= 4:
        return "****"
    return f"{value[:2]}***{value[-2:]}"


def _scrub(value: object) -> str:
    """Remove known secrets from diagnostic exception strings."""
    text = str(value)
    known_secret_envs = [
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "WEBSHARE_PROXY_USERNAME",
        "WEBSHARE_PROXY_PASSWORD",
    ]
    secrets = []
    for name in known_secret_envs:
        raw = os.getenv(name)
        if raw:
            secrets.append(raw)
            if "," in raw:
                secrets.extend(part.strip() for part in raw.split(",") if part.strip())
    for secret in sorted(set(secrets), key=len, reverse=True):
        text = text.replace(secret, "<SECRET>")
    return re.sub(r"(?i)(https?://)([^\s/@:]+):([^\s/@]+)@", r"\1***:***@", text)


def _webshare_settings() -> tuple[str | None, str | None, list[str]]:
    """Return official Webshare settings for youtube-transcript-api.

    The library expects one Webshare Proxy Username and Proxy Password.
    Location rotation is configured with filter_ip_locations, not with
    username variants or fallback proxy URLs.
    """
    username = os.getenv("WEBSHARE_PROXY_USERNAME", "").strip() or None
    password = os.getenv("WEBSHARE_PROXY_PASSWORD", "").strip() or None
    locations = [location.lower() for location in _csv_env("WEBSHARE_PROXY_LOCATIONS")]
    return username, password, locations


def _webshare_proxy_ports() -> list[int]:
    """Return Webshare proxy ports to try in order.

    Webshare's rotating endpoint defaults to 80, but HF Spaces has shown
    proxy/tunnel quirks on outbound ports in some cases. Try documented
    alternative ports first, then fall back to Webshare's default port 80.
    """
    raw_values = _csv_env("WEBSHARE_PROXY_PORTS") or ["1080", "3128", "10000", "80"]
    ports: list[int] = []
    for raw in raw_values:
        try:
            port = int(raw)
        except ValueError:
            print(f"[DIAG] Webshare proxy port 무시: invalid={raw!r}", flush=True)
            continue
        if port <= 0 or port > 65535:
            print(f"[DIAG] Webshare proxy port 무시: out_of_range={port}", flush=True)
            continue
        if port not in ports:
            ports.append(port)
    return ports or [80]


def _webshare_proxy_config(
    locations_override: list[str] | None = None,
    proxy_port: int | None = None,
) -> WebshareProxyConfig | None:
    username, password, configured_locations = _webshare_settings()
    if not (username and password):
        return None
    locations = configured_locations if locations_override is None else locations_override
    return WebshareProxyConfig(
        proxy_username=username,
        proxy_password=password,
        filter_ip_locations=locations or None,
        proxy_port=proxy_port or _webshare_proxy_ports()[0],
    )


@st.cache_resource(show_spinner=False)
def _startup_diagnostics_once():
    """Log deployment diagnostics once per process instead of on every Streamlit rerun."""
    ws_user, ws_pass, ws_locations = _webshare_settings()
    print(
        "\n" + "=" * 50 + "\n"
        "[DIAG] 서버 시작 시점 환경 변수 점검:\n"
        f"  - Webshare username 로드 여부: {bool(ws_user)} ({_mask(ws_user)})\n"
        f"  - Webshare password 로드 여부: {bool(ws_pass)}\n"
        f"  - Webshare 국가 필터: {ws_locations or '전체 풀'}\n"
        f"  - Webshare 포트 시도 순서: {_webshare_proxy_ports()}\n"
        f"  - LLM provider: {model_config.LLM_PROVIDER_NAME} ({model_config.LLM_MODEL})\n"
        + "=" * 50 + "\n",
        flush=True,
    )
    return True

# 자막 언어 우선순위 (한국어 → 영어 순으로 시도)
TRANSCRIPT_LANGS = ["ko", "en"]

# 테스터가 바로 체험할 수 있는 예시 영상 (클릭하면 URL 입력창에 자동 입력)
# 칩 라벨은 짧은 카테고리, 마우스 호버 시 실제 제목(help) 표시.
EXAMPLES = [
    {"url": "https://www.youtube.com/watch?v=CDTEtw90G04", "icon": "🖥️", "label": "AI 코딩"},
    {"url": "https://www.youtube.com/watch?v=7p3w7fveSJk", "icon": "🎨", "label": "디자인 코딩"},
    {"url": "https://www.youtube.com/watch?v=wCYCYfNNGUM", "icon": "🔮", "label": "AI 미래"},
    {"url": "https://www.youtube.com/watch?v=2eqPBLgVH0U&t=192s", "icon": "📚", "label": "클로드 개념"},
    {"url": "https://www.youtube.com/watch?v=7OUWELKUac4&t=1202s", "icon": "🦾", "label": "로봇 산업"},
]

# Q&A 빈 상태에서 보여줄 추천 질문 (클릭하면 바로 질문)
SUGGESTED_QUESTIONS = [
    "이 영상의 핵심 내용은 뭐야?",
    "가장 중요한 인사이트는 뭐야?",
    "전체 내용을 3줄로 요약해줘",
    "초보자도 이해할 수 있게 설명해줘",
]


# ----------------------------- 유틸 -----------------------------
def extract_video_id(url: str) -> str | None:
    """다양한 유튜브 URL 형식에서 11자리 video id 추출."""
    url = url.strip()
    patterns = [
        r"(?:v=|/watch\?.*v=)([0-9A-Za-z_-]{11})",
        r"youtu\.be/([0-9A-Za-z_-]{11})",
        r"/embed/([0-9A-Za-z_-]{11})",
        r"/shorts/([0-9A-Za-z_-]{11})",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    if re.fullmatch(r"[0-9A-Za-z_-]{11}", url):
        return url
    return None


def fmt_time(seconds: float) -> str:
    """초 → m:ss 또는 h:mm:ss."""
    s = int(seconds)
    h, m, s = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"



def _transcript_retry_settings() -> tuple[int, float]:
    """Retry settings for transient YouTube/proxy network failures."""
    try:
        attempts = int(os.getenv("TRANSCRIPT_FETCH_RETRIES", "5"))
    except ValueError:
        attempts = 5
    try:
        backoff = float(os.getenv("TRANSCRIPT_FETCH_BACKOFF_SECONDS", "1.5"))
    except ValueError:
        backoff = 1.5
    return max(1, min(6, attempts)), max(0.2, min(10.0, backoff))


def _is_retryable_transcript_error(exc: Exception) -> bool:
    """Return True for transient proxy/network failures worth retrying.

    These failures are common with rotating proxies and YouTube responses. They
    do not mean the video lacks captions; a fresh connection often succeeds.
    """
    if isinstance(
        exc,
        (
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ConnectionError,
            requests.exceptions.ReadTimeout,
            requests.exceptions.Timeout,
            requests.exceptions.SSLError,
        ),
    ):
        return True

    text = str(exc)
    retryable_markers = (
        "Response ended prematurely",
        "RemoteDisconnected",
        "IncompleteRead",
        "Connection reset",
        "Connection aborted",
        "EOF occurred",
        "timed out",
        "Read timed out",
        "Max retries exceeded",
        "Temporary failure",
    )
    non_retryable_markers = (
        "Tunnel connection failed: 400 Bad Request",
        "407 Proxy Authentication Required",
        "401 Unauthorized",
        "403 Forbidden",
    )
    if any(marker in text for marker in non_retryable_markers):
        return False
    return any(marker in text for marker in retryable_markers)


@st.cache_resource(show_spinner=False)
def _proxy_selftest():
    """[DIAG] 프록시 경유로 (1)출구IP (2)유튜브 접속을 점검해 로그로 남김.
    자막 추출 동작에는 영향 없음 — 단순 관찰용."""
    proxy_config = _webshare_proxy_config()
    if not proxy_config:
        print("[DIAG] selftest: webshare 자격증명 없음", flush=True)
        return True

    sess = requests.Session()
    sess.proxies = proxy_config.to_requests_dict()
    for name, url in [
        ("ipify(출구IP)", "https://api.ipify.org"),
        ("youtube", "https://www.youtube.com/robots.txt"),
    ]:
        try:
            r = sess.get(url, timeout=20)
            print(f"[DIAG] selftest {name}: HTTP {r.status_code} {r.text[:40].strip()!r}", flush=True)
        except Exception as e:
            print(
                f"[DIAG] selftest {name}: WARN {type(e).__name__}: {_scrub(e)[:90]} "
                "(실제 자막 요청에서 자동 재시도/폴백)",
                flush=True,
            )
    return True


def fetch_transcript(video_id: str) -> list:
    """자막 세그먼트 [{text, start, duration}] 리스트 반환."""

    def _fetch_once(proxy_config, proxy_mode: str, attempt: int, max_attempts: int):
        print(
            f"[DIAG] transcript: YouTubeTranscriptApi 생성 시작 "
            f"(mode={proxy_mode}, attempt={attempt}/{max_attempts})",
            flush=True,
        )
        api = YouTubeTranscriptApi(proxy_config=proxy_config)
        print(
            f"[DIAG] transcript: fetch 시작 "
            f"(video_id={video_id}, languages={TRANSCRIPT_LANGS}, "
            f"mode={proxy_mode}, attempt={attempt}/{max_attempts})",
            flush=True,
        )
        fetch_started = time.perf_counter()
        fetched = api.fetch(video_id, languages=TRANSCRIPT_LANGS)
        raw_data = fetched.to_raw_data()
        print(
            f"[DIAG] transcript: fetch 성공 "
            f"(segments={len(raw_data)}, mode={proxy_mode}, "
            f"attempt={attempt}/{max_attempts}, "
            f"elapsed_sec={time.perf_counter() - fetch_started:.2f})",
            flush=True,
        )
        return raw_data

    def _fetch_with_retries(proxy_config_factory, proxy_mode: str):
        max_attempts, backoff_seconds = _transcript_retry_settings()
        last_error = None
        for attempt in range(1, max_attempts + 1):
            try:
                return _fetch_once(
                    proxy_config_factory(),
                    proxy_mode,
                    attempt,
                    max_attempts,
                )
            except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable):
                print("[DIAG] 영상 자체 문제 발생으로 즉시 중단합니다. (자막 없음 또는 비공개 등)", flush=True)
                raise
            except Exception as e:
                last_error = e
                retryable = _is_retryable_transcript_error(e)
                print(
                    f"[DIAG] transcript: fetch 실패 "
                    f"(mode={proxy_mode}, attempt={attempt}/{max_attempts}, "
                    f"retryable={retryable}, error={type(e).__name__}: {_scrub(e)})",
                    flush=True,
                )
                if not retryable or attempt >= max_attempts:
                    break
                sleep_for = (backoff_seconds * attempt) + random.uniform(0, 0.5)
                print(
                    f"[DIAG] transcript: 일시 오류로 재시도합니다 "
                    f"(sleep={sleep_for:.1f}s, next_attempt={attempt + 1}/{max_attempts})",
                    flush=True,
                )
                time.sleep(sleep_for)
        raise last_error if last_error else RuntimeError("자막 fetch 실패")

    # Webshare 공식 권장 방식: 단일 Proxy Username/Password + 선택 국가 필터.
    # HF Spaces에서는 일부 국가 풀의 YouTube TLS 연결이 중간에 끊기는 경우가 있어
    # 설정된 국가 풀이 네트워크 오류로 실패하면 Webshare 전체 풀로 한 번 더 시도한다.
    webshare_proxy = _webshare_proxy_config()
    if webshare_proxy:
        _, _, locations = _webshare_settings()
        location_msg = ",".join(locations) if locations else "전체 풀"
        print(
            f"[DIAG] Webshare 프록시 설정 확인 완료 "
            f"(username={_mask(webshare_proxy.proxy_username)}, locations={location_msg})",
            flush=True,
        )

        location_attempts: list[tuple[str, list[str] | None]] = [(location_msg, None)]
        if locations:
            location_attempts.append(("전체 풀 폴백", []))

        attempts: list[tuple[str, callable]] = []
        for location_name, location_override in location_attempts:
            for port in _webshare_proxy_ports():
                attempts.append((
                    f"Webshare[{location_name}, port={port}]",
                    lambda location_override=location_override, port=port: _webshare_proxy_config(
                        location_override, proxy_port=port
                    ),
                ))

        last_error = None
        for mode_name, proxy_factory in attempts:
            try:
                print(f"[DIAG] {mode_name}로 자막 요청을 시작합니다.", flush=True)
                return _fetch_with_retries(proxy_factory, mode_name)
            except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable):
                raise
            except Exception as e:
                last_error = e
                retryable = _is_retryable_transcript_error(e)
                print(
                    f"[DIAG] {mode_name} 최종 실패: "
                    f"retryable={retryable}, error={type(e).__name__} - {_scrub(e)}",
                    flush=True,
                )
                if not retryable:
                    break
                if mode_name != attempts[-1][0]:
                    print("[DIAG] 다음 Webshare 포트/국가 조합으로 폴백합니다.", flush=True)
        raise last_error if last_error else RuntimeError("Webshare transcript fetch 실패")

    # 프록시 없이 직접 연결
    print("[DIAG] ⚠️ Webshare 프록시 설정이 없어 직접 연결을 시도합니다. (클라우드 환경에서는 차단 가능성 높음)", flush=True)
    try:
        return _fetch_with_retries(lambda: None, "direct")
    except Exception as e:
        print(f"[DIAG] direct transcript 최종 실패: {type(e).__name__} - {_scrub(e)}", flush=True)
        raise


def build_timestamped_text(segments: list) -> str:
    """[분:초] 텍스트 형태로 합쳐 AI에 넘길 문자열 생성."""
    return "\n".join(f"[{fmt_time(seg['start'])}] {seg['text']}" for seg in segments)



def _env_int(name: str, default: int, *, min_value: int, max_value: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(min_value, min(max_value, value))


def _truncate_text(text: str, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)].rstrip() + "…"


def build_summary_transcript(segments: list) -> str:
    """Build a compact time-window transcript for initial summary latency.

    The full transcript can be 10k~50k+ chars. For initial summary/highlights we
    preserve the timeline but merge dense segment lines into windows and cap each
    window. This keeps broad coverage while reducing LLM input size.
    """
    if not segments:
        return ""

    max_total_chars = _env_int("SUMMARY_TRANSCRIPT_MAX_CHARS", 7000, min_value=2500, max_value=20000)
    window_seconds = _env_int("SUMMARY_TRANSCRIPT_WINDOW_SECONDS", 45, min_value=20, max_value=180)
    configured_window_chars = _env_int("SUMMARY_TRANSCRIPT_WINDOW_CHARS", 320, min_value=100, max_value=800)

    windows: list[dict] = []
    current: dict | None = None
    for seg in segments:
        start = float(seg.get("start", 0) or 0)
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        if current is None or start >= current["start"] + window_seconds:
            current = {"start": start, "end": start, "texts": []}
            windows.append(current)
        current["end"] = max(current["end"], start + float(seg.get("duration", 0) or 0))
        current["texts"].append(text)

    if not windows:
        return ""

    # Dynamically shrink each window so long videos do not send huge prompts.
    per_window_budget = max(100, min(configured_window_chars, (max_total_chars // len(windows)) - 18))
    lines = []
    for window in windows:
        joined = " ".join(window["texts"])
        lines.append(
            f"[{fmt_time(window['start'])}~{fmt_time(window['end'])}] "
            f"{_truncate_text(joined, per_window_budget)}"
        )

    compact = "\n".join(lines)
    if len(compact) > max_total_chars:
        compact = compact[:max_total_chars].rstrip() + "\n...[요약용 입력 길이 제한으로 일부 생략]"

    full_chars = len(build_timestamped_text(segments))
    print(
        f"[DIAG] summarize_input: 압축 자막 생성 "
        f"(segments={len(segments)}, windows={len(windows)}, "
        f"full_chars={full_chars}, compact_chars={len(compact)}, "
        f"window_seconds={window_seconds}, per_window_chars={per_window_budget})",
        flush=True,
    )
    return compact


@st.cache_data(show_spinner=False, ttl=24 * 60 * 60)
def summarize_cached(provider_name: str, model_name: str, transcript_for_summary: str) -> dict:
    """Cache summary for the same provider/model/transcript during repeated local runs."""
    print(
        f"[DIAG] summarize_cache: miss "
        f"(provider={provider_name}, model={model_name}, input_chars={len(transcript_for_summary)})",
        flush=True,
    )
    return model_config.summarize_and_highlight(transcript_for_summary)



_QA_STOPWORDS = {
    "이", "그", "저", "영상", "내용", "무엇", "뭐야", "뭔가", "어떻게", "왜", "언제", "어디",
    "알려줘", "설명", "정리", "핵심", "요약", "결론", "부분", "장면", "대해", "관련", "있는",
    "없는", "해서", "하고", "그리고", "그러면", "그럼", "좀", "주세요", "인가", "같아", "거야",
}


def _question_terms(question: str) -> list[str]:
    """Extract lightweight Korean/English search terms for local transcript snippets."""
    raw_terms = re.findall(r"[0-9A-Za-z가-힣]{2,}", question.lower())
    terms = []
    for term in raw_terms:
        if term in _QA_STOPWORDS:
            continue
        if term not in terms:
            terms.append(term)
    return terms[:10]


def _is_general_question(question: str) -> bool:
    question = question.lower()
    general_markers = [
        "핵심", "요약", "결론", "주제", "무슨 영상", "뭐야", "무엇", "전체", "정리",
        "내용 알려", "한줄", "한 줄",
    ]
    return any(marker in question for marker in general_markers)


def build_qa_context(
    transcript_text: str,
    summary: str | None,
    highlights: list | None,
    question: str,
    *,
    max_chars: int = 5200,
) -> str:
    """Build a compact Q&A context instead of sending the full transcript every turn.

    General questions use summary/highlights only. Specific questions add transcript
    lines whose text overlaps with question terms, plus nearby lines for context.
    """
    sections: list[str] = []
    summary = (summary or "").strip()
    if summary:
        sections.append(f"[전체 요약]\n{summary}")

    clean_highlights = highlights or []
    if clean_highlights:
        highlight_lines = [
            f"[{fmt_time(h['start'])}] {h['point']}"
            for h in clean_highlights[:10]
            if isinstance(h, dict) and "start" in h and "point" in h
        ]
        if highlight_lines:
            sections.append("[타임스탬프별 핵심]\n" + "\n".join(highlight_lines))

    # “핵심이 뭐야?” 류는 요약/하이라이트면 충분해서 원문 전체를 보내지 않는다.
    if _is_general_question(question) and summary:
        context = "\n\n".join(sections)
        print(
            f"[DIAG] qa_context: general question, summary/highlights only "
            f"(context_chars={len(context)}, full_transcript_chars={len(transcript_text or '')})",
            flush=True,
        )
        return context[:max_chars]

    lines = [line for line in (transcript_text or "").splitlines() if line.strip()]
    terms = _question_terms(question)
    selected_indexes: set[int] = set()
    if terms:
        for idx, line in enumerate(lines):
            low = line.lower()
            score = sum(1 for term in terms if term in low)
            if score:
                # Include a small window around each hit so the answer is not too fragmented.
                for neighbor in range(max(0, idx - 1), min(len(lines), idx + 2)):
                    selected_indexes.add(neighbor)

    selected_lines = [lines[idx] for idx in sorted(selected_indexes)]
    if selected_lines:
        excerpt = "\n".join(selected_lines)
        sections.append(f"[질문 관련 자막 발췌]\n{excerpt}")
    else:
        # Fallback: keep factual grounding, but cap aggressively for latency.
        fallback = "\n".join(lines[:80])
        if fallback:
            sections.append(f"[자막 앞부분 발췌]\n{fallback}")

    context = "\n\n".join(sections)
    if len(context) > max_chars:
        context = context[:max_chars] + "\n...[컨텍스트 길이 제한으로 일부 생략]"

    print(
        f"[DIAG] qa_context: compact context built "
        f"(terms={terms}, matched_lines={len(selected_lines)}, "
        f"context_chars={len(context)}, full_transcript_chars={len(transcript_text or '')})",
        flush=True,
    )
    return context


def compact_chat_history(history: list, *, max_turns: int = 4, max_chars_per_turn: int = 700) -> list:
    """Keep only recent short history for faster Q&A calls."""
    compact = []
    for turn in (history or [])[-max_turns:]:
        content = str(turn.get("content", ""))[:max_chars_per_turn]
        compact.append({"role": turn.get("role", "assistant"), "content": content})
    return compact


def esc(text: str) -> str:
    """HTML 삽입 시 최소 이스케이프."""
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )



def _inline_markdown_to_safe_html(text: str) -> str:
    """Convert a small safe subset of Markdown inline syntax after HTML escaping."""
    text = esc(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
    return text


def markdownish_to_safe_html(text: str) -> str:
    """Render model Q&A text as safe HTML while preserving common Markdown.

    Raw Markdown inside a custom HTML bubble is not processed by Streamlit.
    This supports the subset the app asks models to produce: headings,
    bullets/numbered bullets, paragraphs, bold, and inline code.
    """
    lines = str(text or "").splitlines()
    parts: list[str] = []
    in_ul = False
    in_ol = False

    def close_lists() -> None:
        nonlocal in_ul, in_ol
        if in_ul:
            parts.append("</ul>")
            in_ul = False
        if in_ol:
            parts.append("</ol>")
            in_ol = False

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            close_lists()
            continue

        if line.startswith("### "):
            close_lists()
            parts.append(f"<h4>{_inline_markdown_to_safe_html(line[4:])}</h4>")
            continue
        if line.startswith("## "):
            close_lists()
            parts.append(f"<h3>{_inline_markdown_to_safe_html(line[3:])}</h3>")
            continue
        if line.startswith("# "):
            close_lists()
            parts.append(f"<h3>{_inline_markdown_to_safe_html(line[2:])}</h3>")
            continue

        bullet_match = re.match(r"^[-*]\s+(.+)$", line)
        if bullet_match:
            if in_ol:
                parts.append("</ol>")
                in_ol = False
            if not in_ul:
                parts.append("<ul>")
                in_ul = True
            parts.append(f"<li>{_inline_markdown_to_safe_html(bullet_match.group(1))}</li>")
            continue

        numbered_match = re.match(r"^\d+[.)]\s+(.+)$", line)
        if numbered_match:
            if in_ul:
                parts.append("</ul>")
                in_ul = False
            if not in_ol:
                parts.append("<ol>")
                in_ol = True
            parts.append(f"<li>{_inline_markdown_to_safe_html(numbered_match.group(1))}</li>")
            continue

        close_lists()
        parts.append(f"<p>{_inline_markdown_to_safe_html(line)}</p>")

    close_lists()
    return "".join(parts)


def build_report(video_id: str, summary: str, highlights: list) -> str:
    """요약 + 타임스탬프 핵심을 텍스트 리포트로 (다운로드용)."""
    lines = [
        "🎬 YouTube 영상 요약",
        f"URL: https://www.youtube.com/watch?v={video_id}",
        "",
        "=" * 40,
        "[전체 요약]",
        summary or "",
        "",
        "=" * 40,
        "[타임스탬프별 핵심]",
    ]
    for h in highlights or []:
        lines.append(f"[{fmt_time(h['start'])}] {h['point']}")
    return "\n".join(lines)


def ai_error_msg(e: Exception) -> str:
    """AI 호출 오류를 사용자 친화적 한국어 메시지로. 429(한도 초과)와 503(서버 혼잡)을 구분."""
    s = str(e)
    if any(k in s for k in ("429", "RESOURCE_EXHAUSTED", "quota", "rate limit", "Rate limit")):
        return (
            "오늘 무료 사용 한도(분당/일일 요청 수)를 초과했어요. "
            "1~2분 후(분당 한도) 또는 내일(일일 한도) 다시 시도해주세요. 🙏"
        )
    if any(k in s for k in ("503", "UNAVAILABLE", "high demand", "overloaded", "500")):
        return f"지금 {model_config.LLM_PROVIDER_NAME} 서버가 일시적으로 혼잡해요(과부하). 잠시 후 다시 시도해주세요. 🙏"
    return f"AI 처리 중 오류가 발생했습니다: {e}"


def use_example(example_url: str) -> None:
    """예시 버튼 클릭 시 URL 입력창에 자동 입력 (on_click 콜백)."""
    st.session_state.url_input = example_url


def ask_suggestion(question: str) -> None:
    """추천 질문 클릭 시 바로 질문 전송 (on_click 콜백). 이후 rerun에서 답변 생성."""
    if not st.session_state.pending_q:
        st.session_state.chat.append({"role": "user", "content": question})
        st.session_state.pending_q = question



ANALYSIS_STEPS = ["URL 확인", "자막 추출", "AI 요약", "결과 정리"]


def render_analysis_status(title: str, detail: str, active_step: int, progress: int) -> str:
    """Toast-style progress card shown without replacing the main screen."""
    progress = max(0, min(100, int(progress)))
    chips = []
    for idx, label in enumerate(ANALYSIS_STEPS):
        state = "done" if idx < active_step else "active" if idx == active_step else "todo"
        mark = "✓" if state == "done" else str(idx + 1)
        chips.append(
            f'<span class="analysis-step {state}"><b>{mark}</b>{esc(label)}</span>'
        )
    return f"""
    <div class="analysis-panel" role="status" aria-live="polite">
        <div class="analysis-main">
            <span class="analysis-loader"></span>
            <div>
                <div class="analysis-title-row">
                    <div class="analysis-title">{esc(title)}</div>
                    <div class="analysis-percent">{progress}%</div>
                </div>
                <div class="analysis-detail">{esc(detail)}</div>
            </div>
        </div>
        <div class="analysis-progress-track"><span style="width: {progress}%"></span></div>
        <div class="analysis-steps">{''.join(chips)}</div>
    </div>
    """


def run_initial_analysis(video_id: str) -> None:
    """Run the long analysis while the already-rendered screen stays visible."""
    status_box = st.empty()

    def update_status(title: str, detail: str, active_step: int, progress: int) -> None:
        status_box.markdown(
            render_analysis_status(title, detail, active_step, progress),
            unsafe_allow_html=True,
        )

    def fail(message: str) -> None:
        status_box.empty()
        st.error(message)
        st.stop()

    should_reset_chat = video_id != st.session_state.video_id

    update_status("영상 분석을 준비하고 있어요", "URL과 실행 환경을 확인하는 중입니다.", 0, 8)

    update_status("YouTube 자막을 가져오는 중이에요", "연결이 불안정하면 자동으로 재시도합니다.", 1, 22)
    try:
        segments = fetch_transcript(video_id)
    except TranscriptsDisabled:
        fail("이 영상은 자막이 비활성화되어 있어 요약할 수 없습니다.")
    except NoTranscriptFound:
        fail(f"한국어/영어 자막을 찾지 못했습니다. (지원: {', '.join(TRANSCRIPT_LANGS)})")
    except VideoUnavailable:
        fail("영상을 찾을 수 없습니다. 비공개이거나 삭제된 영상일 수 있어요.")
    except (IpBlocked, RequestBlocked) as e:
        print(f"[DIAG] transcript IpBlocked/RequestBlocked: {type(e).__name__}: {_scrub(e)}", flush=True)
        fail(
            "유튜브가 이 서버/프록시 IP를 차단했습니다. "
            "프록시를 이미 설정했다면 그 IP도 막힌 거예요(무료 데이터센터 프록시는 자주 차단됨) — "
            "다른 IP 또는 주거용(residential) 프록시가 필요합니다. "
            "(배포 환경에선 HF Secret의 WEBSHARE_PROXY_USERNAME/WEBSHARE_PROXY_PASSWORD 사용)"
        )
    except CouldNotRetrieveTranscript as e:
        print(f"[DIAG] transcript CouldNotRetrieveTranscript: {type(e).__name__}: {_scrub(e)}", flush=True)
        fail("자막을 가져오지 못했습니다. 클라우드 서버라면 IP 차단일 수 있어요 (Webshare 프록시 설정 필요).")
    except Exception as e:
        print(f"[DIAG] transcript error: {type(e).__name__}: {_scrub(e)}", flush=True)
        m = str(e)
        if any(k in m for k in ("SSL", "Max retries", "ConnectionError", "Connection", "timed out", "RemoteDisconnected", "EOF", "ChunkedEncodingError", "Response ended prematurely", "IncompleteRead")):
            fail("자막 서버 연결이 차단됐어요. 배포 서버(클라우드) IP를 YouTube가 막는 경우예요 — Webshare 프록시 설정이 필요합니다. (로컬에선 정상 동작)")
        fail(f"자막 추출 중 오류가 발생했습니다: {e}")

    update_status("자막을 정리하고 있어요", f"{len(segments)}개의 자막 조각을 분석용 입력으로 압축합니다.", 1, 45)
    transcript_text = build_timestamped_text(segments)
    print(
        f"[DIAG] transcript: UI 상태 저장 준비 완료 "
        f"(segments={len(segments)}, transcript_chars={len(transcript_text)})",
        flush=True,
    )

    try:
        transcript_for_summary = build_summary_transcript(segments)
        update_status(
            "AI가 요약 중이에요",
            "영상의 핵심 내용과 타임스탬프를 정리하고 있습니다.",
            2,
            66,
        )
        summarize_started = time.perf_counter()
        print(
            f"[DIAG] summarize: 요청 시작 "
            f"(full_transcript_chars={len(transcript_text)}, "
            f"summary_input_chars={len(transcript_for_summary)})",
            flush=True,
        )
        result = summarize_cached(
            model_config.LLM_PROVIDER_NAME,
            model_config.LLM_MODEL,
            transcript_for_summary,
        )
        update_status("거의 다 됐어요", "요약 카드와 타임스탬프를 화면에 배치하는 중입니다.", 3, 88)
        summary = result["summary"]
        highlights = result["highlights"]
        print(
            f"[DIAG] summarize: 성공 "
            f"(summary_chars={len(summary)}, "
            f"highlights={len(highlights)}, "
            f"elapsed_sec={time.perf_counter() - summarize_started:.2f})",
            flush=True,
        )
    except Exception as e:
        fail(ai_error_msg(e))

    if not summary:
        fail("요약을 생성하지 못했어요. 잠시 후 다시 시도해주세요.")

    updates = {
        "video_id": video_id,
        "segments": segments,
        "transcript_text": transcript_text,
        "summary": summary,
        "highlights": highlights,
    }
    if should_reset_chat:
        updates.update(chat=[], pending_q=None)
    st.session_state.update(**updates)
    update_status("분석이 완료됐어요", "요약 결과를 표시합니다.", len(ANALYSIS_STEPS), 100)
    time.sleep(0.25)
    status_box.empty()



# ----------------------------- 페이지 설정 + 스타일 -----------------------------
st.set_page_config(
    page_title="AI YouTube Summarizer",
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# [DIAG] 배포/로컬 진단 1회 (로그 확인용, 동작 영향 없음)
_startup_diagnostics_once()
_proxy_selftest()

st.markdown(
    """
    <style>
    /* ----- 전체 배경 / 여백 ----- */
    /* 기본 100% 화면에서도 살짝 축소된(≈90%) 비율로 보이게 — 콘텐츠가 한 화면에 여유롭게 들어감 */
    .stApp { background: linear-gradient(180deg, #f7f8fc 0%, #eef0f8 100%); zoom: 0.9; }
    /* Streamlit 기본 상단바(헤더/Deploy·메뉴·실행상태)는 숨김 */
    [data-testid="stHeader"] { display: none; }
    [data-testid="stToolbar"] { display: none; }
    /* 헤더 숨김 후, 전체 콘텐츠를 상단에서 살짝(+20px) 더 내려 배치 */
    .block-container { padding-top: calc(2rem + 20px); padding-bottom: 72px; max-width: 1180px; }

    /* ----- 헤더 (가운데 정렬, 로고는 제목+부제목 세로 중앙) ----- */
    .hero { display: flex; align-items: center; justify-content: center; gap: 16px; margin-bottom: 24px; }
    .hero-logo {
        width: 64px; height: 46px; flex-shrink: 0;
        background: linear-gradient(135deg, #9b8cff 0%, #6c5ce7 100%);
        border-radius: 14px; display: flex; align-items: center; justify-content: center;
        box-shadow: 0 8px 18px rgba(108,92,231,.32);
    }
    .hero-logo:after {
        content: ""; border-style: solid; border-width: 10px 0 10px 17px;
        border-color: transparent transparent transparent #fff; margin-left: 4px;
    }
    .hero-title { font-size: 2.6rem; font-weight: 800; line-height: 1.05; color: #1f2438; letter-spacing: -1px; }
    .hero-sub { font-size: 1.05rem; color: #a3a7ba; margin-top: 8px; font-weight: 500; }

    /* ----- URL 입력 카드 ----- */
    .st-key-url_card { padding: 16px 20px !important; margin-bottom: 4px; }
    .url-label { font-size: 1.05rem; font-weight: 700; color: #1f2438; margin: 2px 2px 12px; }
    .url-hint { font-size: .9rem; color: #8a86b8; font-weight: 500; margin: 12px 2px 2px; }


    /* ----- 분석 진행 상태: 메인 화면을 밀어내지 않는 토스트 ----- */
    .analysis-panel {
        position: fixed;
        right: 24px;
        bottom: 92px;
        z-index: 1000;
        width: min(420px, calc(100vw - 48px));
        background: rgba(255,255,255,.96);
        border: 1px solid #eceef5;
        border-radius: 18px;
        box-shadow: 0 18px 55px rgba(40,50,90,.18);
        padding: 16px 18px;
        backdrop-filter: blur(10px);
        animation: analysis-toast-in .22s ease-out both;
    }
    @keyframes analysis-toast-in {
        from { opacity: 0; transform: translateY(10px) scale(.98); }
        to { opacity: 1; transform: translateY(0) scale(1); }
    }
    .analysis-main { display: flex; align-items: center; gap: 12px; }
    .analysis-title-row { display: flex; align-items: center; justify-content: space-between; gap: 14px; }
    .analysis-title { font-size: 1rem; font-weight: 800; color: #1f2438; }
    .analysis-percent {
        flex: 0 0 auto;
        min-width: 48px;
        padding: 3px 8px;
        border-radius: 999px;
        background: #f0eefb;
        color: #5a4bd4;
        font-size: .82rem;
        font-weight: 800;
        text-align: center;
    }
    .analysis-detail { margin-top: 3px; font-size: .9rem; color: #7b8199; line-height: 1.35; }
    .analysis-loader {
        width: 28px; height: 28px; border-radius: 50%; flex: 0 0 auto;
        border: 3px solid #e8e5fb; border-top-color: #6c5ce7;
        animation: analysis-spin .9s linear infinite;
    }
    @keyframes analysis-spin { to { transform: rotate(360deg); } }
    .analysis-progress-track {
        height: 6px;
        border-radius: 999px;
        overflow: hidden;
        background: #eeecfb;
        margin-top: 14px;
    }
    .analysis-progress-track span {
        display: block;
        height: 100%;
        border-radius: inherit;
        background: linear-gradient(90deg, #9b8cff, #6c5ce7);
        transition: width .25s ease;
    }
    .analysis-steps { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }
    .analysis-step {
        display: inline-flex; align-items: center; gap: 6px;
        padding: 5px 10px; border-radius: 999px;
        font-size: .82rem; font-weight: 600;
        background: #f6f5fc; color: #8a86b8; border: 1px solid #ebe9f6;
    }
    .analysis-step b {
        display: inline-flex; align-items: center; justify-content: center;
        width: 18px; height: 18px; border-radius: 50%;
        font-size: .72rem; background: #ece9fb; color: #6c5ce7;
    }
    .analysis-step.active { background: #f0eefb; color: #4b4570; border-color: #d8d2f3; }
    .analysis-step.active b { background: #6c5ce7; color: #fff; }
    .analysis-step.done { background: #f3fbf2; color: #407a3c; border-color: #d8efd4; }
    .analysis-step.done b { background: #18a915; color: #fff; }

    /* ----- 카드 (st.container border 보강) ----- */
    div[data-testid="stVerticalBlockBorderWrapper"] {
        background: #ffffff; border: 1px solid #eceef5 !important;
        border-radius: 18px; box-shadow: 0 8px 30px rgba(40,50,90,.06);
        padding: 6px 10px;
    }
    .card-head { font-size: 1.12rem; font-weight: 700; color: #1f2438; margin: 4px 2px 12px; }
    /* Streamlit/브라우저 다크모드에서 Markdown 글자색이 흰색으로 상속되는 것을 방지 */
    div[data-testid="stVerticalBlockBorderWrapper"],
    div[data-testid="stVerticalBlockBorderWrapper"] [data-testid="stMarkdownContainer"],
    .st-key-sum_body, .st-key-sum_body *,
    .st-key-ts_body, .st-key-ts_body *,
    .st-key-qa_body, .st-key-qa_body * {
        color: #2b3047 !important;
    }
    div[data-testid="stVerticalBlockBorderWrapper"] h1,
    div[data-testid="stVerticalBlockBorderWrapper"] h2,
    div[data-testid="stVerticalBlockBorderWrapper"] h3,
    div[data-testid="stVerticalBlockBorderWrapper"] h4,
    .st-key-sum_body h1, .st-key-sum_body h2, .st-key-sum_body h3, .st-key-sum_body h4 {
        color: #1f2438 !important;
    }
    div[data-testid="stVerticalBlockBorderWrapper"] strong,
    .st-key-sum_body strong, .st-key-qa_body strong {
        color: #171b2d !important;
    }
    .st-key-sum_body a, .st-key-ts_body a, .st-key-qa_body a {
        color: #5a4bd4 !important;
    }
    /* 제목(헤더)은 카드 상단에 고정하고, 본문 영역만 스크롤되게 (제목이 스크롤에 안 사라짐) */
    .st-key-sum_body { max-height: 226px; overflow-y: auto; }   /* 전체 요약 본문 */
    /* 전체 요약 Markdown: Streamlit 기본 h2/h3가 카드 안에서 과하게 커지는 것 보정 */
    .st-key-sum_body [data-testid="stMarkdownContainer"] {
        font-size: .94rem !important;
        line-height: 1.5 !important;
    }
    .st-key-sum_body h1,
    .st-key-sum_body h2,
    .st-key-sum_body h3 {
        font-size: 1.02rem !important;
        line-height: 1.35 !important;
        margin: 8px 0 6px !important;
        padding: 0 !important;
        border: 0 !important;
        font-weight: 800 !important;
    }
    .st-key-sum_body h1:first-child,
    .st-key-sum_body h2:first-child,
    .st-key-sum_body h3:first-child {
        margin-top: 0 !important;
    }
    .st-key-sum_body p {
        margin: 0 0 8px !important;
    }
    .st-key-sum_body ul,
    .st-key-sum_body ol {
        margin: 4px 0 10px 18px !important;
        padding: 0 !important;
    }
    .st-key-sum_body li {
        margin: 4px 0 !important;
        padding-left: 2px !important;
    }
    .st-key-sum_body hr { display: none !important; }
    /* 타임스탬프·Q&A 카드: 내용량과 무관하게 '항상 같은 고정 높이'.
       Streamlit height= 대신 CSS로 카드 높이를 고정(height= 는 내부 행을 늘려 렌더가 깨짐).
       헤더는 카드 상단에 고정, 본문(ts_body·qa_body)만 max-height로 스크롤, Q&A 입력창은 본문 아래 고정. */
    .st-key-ts_card, .st-key-qa_card { min-height: 360px; max-height: 360px; overflow: hidden; }
    .st-key-ts_body { max-height: 300px; overflow-y: auto; }    /* 타임스탬프 본문 */
    /* Q&A: 채팅 영역이 남는 공간을 채워(flex) 입력창(form)을 카드 맨 아래에 고정, 채팅만 스크롤 */
    .st-key-qa_card > [data-testid="stLayoutWrapper"]:has(.st-key-qa_body) { flex: 1 1 auto; min-height: 0; }
    .st-key-qa_body { flex: 1 1 auto; min-height: 0; overflow-y: auto; }
    /* Q&A 빈 상태: 안내 문구는 제목 바로 아래(상단) 고정, 추천질문(제목+칩)만 남은 공간 세로 중앙으로
       → 추천질문 블록의 위/아래 여백이 같아지고 입력창과의 간격도 적당히 줄어듦 */
    .st-key-qa_body > [data-testid="stLayoutWrapper"]:has(.st-key-sug_zone) { margin-top: auto; margin-bottom: auto; }
    /* 요약·영상 박스 동일 고정 높이. 영상은 박스 안에서 세로·가로 모두 중앙 정렬. */
    .st-key-video_card div[data-testid="stVerticalBlock"] { height: 100%; justify-content: center; align-items: center; }
    .st-key-video_card [data-testid="stVideo"] { margin: 0 auto; }

    /* ----- 기능 카드 (빈 화면) ----- */
    .feat { text-align: center; padding-bottom: 14px; }
    .feat-ico { font-size: 1.6rem; }
    .feat-title { font-size: 1.08rem; font-weight: 700; color: #1f2438; margin-top: 6px; }
    .feat-desc { font-size: .92rem; color: #6b7392; margin-top: 4px; line-height: 1.45; }

    /* ----- 타임스탬프 행 ----- */
    .ts-row { display: flex; gap: 14px; padding: 9px 4px; border-bottom: 1px solid #f1f2f8; }
    .ts-row:last-child { border-bottom: none; }
    /* 링크(<a>)라서 Streamlit 기본 링크색을 덮어쓰도록 !important + 방문/링크 상태 모두 지정 */
    .ts-time, .ts-time:link, .ts-time:visited, .ts-time:active {
        color: #18cf15 !important; font-weight: 700; font-variant-numeric: tabular-nums;
        text-decoration: none; flex-shrink: 0; min-width: 52px;
    }
    .ts-time:hover { color: #18cf15 !important; text-decoration: underline; }
    .ts-text { color: #2b3047; font-size: .96rem; line-height: 1.4; }

    /* ----- Q&A 말풍선 ----- */
    .bubbles { min-height: 60px; }
    .bubble-row { display: flex; margin: 8px 0; }
    .bubble-row.user { justify-content: flex-end; }
    .bubble { padding: 10px 14px; border-radius: 16px; max-width: 85%; font-size: .95rem; line-height: 1.45; }
    .bubble.user { background: #eef0fb; color: #2b3047; border-bottom-right-radius: 5px; }
    .bubble.bot { background: #f4f5f9; color: #2b3047; border-bottom-left-radius: 5px; }
    .bubble.bot p { margin: 0 0 8px; }
    .bubble.bot p:last-child { margin-bottom: 0; }
    .bubble.bot h3, .bubble.bot h4 { margin: 2px 0 8px; font-size: 1rem; line-height: 1.35; }
    .bubble.bot ul, .bubble.bot ol { margin: 6px 0 8px 18px; padding: 0; }
    .bubble.bot li { margin: 4px 0; }
    .bubble.bot code { background: #e8eaf2; border-radius: 4px; padding: 1px 4px; }
    /* 답변 생성 중 타이핑 로딩(점 3개 애니메이션) */
    .typing { display: inline-flex; gap: 5px; align-items: center; }
    .typing .dot { width: 7px; height: 7px; border-radius: 50%; background: #aeb4cc;
                   animation: typing-blink 1.2s infinite both; }
    .typing .dot:nth-child(2) { animation-delay: .2s; }
    .typing .dot:nth-child(3) { animation-delay: .4s; }
    @keyframes typing-blink { 0%,80%,100% { opacity: .25; transform: translateY(0); }
                              40% { opacity: 1; transform: translateY(-2px); } }

    /* ----- Q&A 빈 상태: 안내 문구 + 추천 질문 pill ----- */
    .qa-guide { font-size: .92rem; color: #8a86b8 !important; margin: 2px 2px 16px; line-height: 1.45; }
    .qa-sug-head { font-size: .9rem; font-weight: 700; color: #6b6f86 !important; margin: 2px 2px 10px; }
    .st-key-suggestions { flex-direction: row !important; flex-wrap: wrap !important;
        gap: 8px !important; width: 100% !important; align-items: stretch !important; }
    .st-key-suggestions > [data-testid="stElementContainer"] { flex: 0 0 calc(50% - 4px) !important; width: calc(50% - 4px) !important; }
    .st-key-suggestions .stButton { width: 100% !important; }
    .st-key-suggestions .stButton > button {
        width: 100% !important; height: 100%;
        background: #f0eefb !important; color: #4b4570 !important; border: 1px solid #ddd8f4 !important;
        border-radius: 999px; font-weight: 500; font-size: .85rem;
        padding: .34rem .9rem; min-height: 0; box-shadow: none; white-space: normal;
    }
    .st-key-suggestions .stButton > button:hover { background: #e8e4fb !important; color: #5a4bd4 !important; border-color: #d0c8f0 !important; }

    /* ----- 버튼: 보라색 ----- */
    .stButton > button, .stFormSubmitButton > button, .stDownloadButton > button {
        background: #6c5ce7; color: #fff; border: none; border-radius: 12px;
        font-weight: 700; padding: .55rem 1.1rem;
    }
    .stButton > button:hover, .stFormSubmitButton > button:hover, .stDownloadButton > button:hover {
        background: #5a4bd4; color: #fff;
    }

    /* ----- 입력창: Streamlit 위젯은 시스템/브라우저 다크모드 영향을 받아 따로 고정 필요 ----- */
    .stTextInput input,
    .stTextInput input:focus,
    .stTextInput input:active,
    div[data-baseweb="input"] input,
    div[data-baseweb="base-input"] input {
        background-color: #ffffff !important;
        color: #1f2438 !important;
        caret-color: #6c5ce7 !important;
        border-radius: 12px !important;
        -webkit-text-fill-color: #1f2438 !important;
        color-scheme: light !important;
    }
    .stTextInput input::placeholder,
    div[data-baseweb="input"] input::placeholder,
    div[data-baseweb="base-input"] input::placeholder {
        color: #a3a7ba !important;
        opacity: 1 !important;
        -webkit-text-fill-color: #a3a7ba !important;
    }
    .stTextInput [data-baseweb="input"],
    .stTextInput [data-baseweb="base-input"],
    div[data-baseweb="input"],
    div[data-baseweb="base-input"] {
        background-color: #ffffff !important;
        border-color: #e6e3f5 !important;
        border-radius: 12px !important;
        color-scheme: light !important;
    }
    .stTextInput [data-baseweb="input"]:focus-within,
    .stTextInput [data-baseweb="base-input"]:focus-within,
    div[data-baseweb="input"]:focus-within,
    div[data-baseweb="base-input"]:focus-within {
        border-color: #9b8cff !important;
        box-shadow: 0 0 0 1px #9b8cff33 !important;
    }
    input:-webkit-autofill,
    input:-webkit-autofill:hover,
    input:-webkit-autofill:focus {
        -webkit-box-shadow: 0 0 0 1000px #ffffff inset !important;
        -webkit-text-fill-color: #1f2438 !important;
        transition: background-color 9999s ease-in-out 0s;
    }

    /* ----- 예시 영상 칩 (요약 전 화면) — 가로 한 줄 알약 ----- */
    .ex-head { font-size: .95rem; font-weight: 700; color: #4b5168; margin: 2px 2px 24px; text-align: center; }
    /* 홈(빈 화면): 한 화면 채우기 — 예시는 가운데, 기능 카드는 맨 아래(푸터 위) */
    .st-key-home { display: flex; flex-direction: column; min-height: calc(100vh - 580px); margin-top: 30px; }
    /* 예시 영역 래퍼를 늘려 화면 중앙 정렬 (기능 카드는 자연히 맨 아래=푸터 위로) */
    .st-key-home > [data-testid="stLayoutWrapper"]:first-child {
        flex: 1 1 auto; display: flex; flex-direction: column; justify-content: center;
    }
    .st-key-ex_zone { justify-content: center; }   /* 예시(제목+칩)를 영역 내 세로 중앙으로 */
    /* 예시 칩 ↔ 기능 카드 사이 간격 */
    .st-key-home > [data-testid="stLayoutWrapper"]:last-child { margin-top: 70px; }
    /* 칩 컨테이너(stVerticalBlock)를 가로 flex로 → 칩들이 한 줄에 배치 */
    .st-key-exchips { flex-direction: row !important; flex-wrap: wrap !important;
        gap: 35px !important; width: 100% !important; align-items: flex-start !important;
        justify-content: center !important; }
    .st-key-exchips > [data-testid="stElementContainer"] { width: auto !important; flex: 0 0 auto !important; }
    .st-key-exchips .stButton { width: auto !important; }
    .st-key-exchips .stButton > button {
        width: max-content !important;
        background: #f4f3fb; color: #4b4570; border: 1px solid #e6e3f5;
        border-radius: 999px; font-weight: 600; font-size: .9rem;
        padding: .42rem 1.15rem; min-height: 0; box-shadow: none;
    }
    .st-key-exchips .stButton > button:hover {
        background: #ece9fb; color: #5a4bd4; border-color: #cfc8f3;
    }
    .st-key-exchips .stButton > button p { white-space: nowrap; }

    /* ----- 하단 기능 칩 (고정 푸터) ----- */
    .chips { position: fixed; left: 0; right: 0; bottom: 0; z-index: 100;
             display: flex; justify-content: center; gap: 38px; flex-wrap: wrap;
             padding: 12px 16px; color: #5b627e; font-weight: 600; font-size: .95rem;
             background: rgba(245,246,251,0.92); backdrop-filter: blur(6px);
             border-top: 1px solid #e6e8f2; }
    .chips span { display: inline-flex; align-items: center; gap: 8px; }

    [data-testid="stMetricValue"] { font-size: 1.3rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ----------------------------- 세션 상태 -----------------------------
for key, default in [
    ("segments", None),
    ("transcript_text", None),
    ("summary", None),
    ("highlights", None),
    ("chat", []),
    ("video_id", None),
    ("pending_q", None),
]:
    st.session_state.setdefault(key, default)


# ----------------------------- 헤더 -----------------------------
st.markdown(
    """
    <div class="hero">
        <div class="hero-logo"></div>
        <div>
            <div class="hero-title">AI YouTube Summarizer</div>
            <div class="hero-sub">영상의 핵심을 빠르게 파악하고, 궁금한 것은 바로 물어보세요</div>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)


# ----------------------------- URL 입력 카드 -----------------------------
with st.container(key="url_card", border=True):
    st.markdown('<div class="url-label">🔗&nbsp; YouTube 영상 URL 입력</div>', unsafe_allow_html=True)
    col_url, col_btn = st.columns([6, 1], vertical_alignment="bottom")
    with col_url:
        url = st.text_input(
            "URL",
            placeholder="https://www.youtube.com/watch?v=...",
            label_visibility="collapsed",
            key="url_input",
        )
    with col_btn:
        analyze = st.button("✦ 요약하기", type="primary", use_container_width=True)
    # st.markdown(
    #     '<div class="url-hint">💡 YouTube 영상의 URL을 입력하면 AI가 핵심 내용을 요약해드려요!</div>',
    #     unsafe_allow_html=True,
    # )


# ----------------------------- 분석 요청 -----------------------------
analysis_video_id = None
if analyze:
    analysis_video_id = extract_video_id(url)
    if not analysis_video_id:
        st.error("유효한 유튜브 URL이 아닙니다. 주소를 다시 확인해주세요.")
        st.stop()


# ----------------------------- 본문 -----------------------------
if st.session_state.summary:
    vid = st.session_state.video_id

    # 1단: 전체 요약 | 영상 — 둘 다 같은 고정 높이로 박스 크기 통일 (영상은 박스 안 세로·가로 중앙 정렬)
    CARD_H = 320
    c_sum, c_vid = st.columns([1, 1], gap="medium")
    with c_sum:
        with st.container(height=CARD_H, border=True, key="summary_card"):
            # 헤더 행: 제목(좌) + 아이콘 다운로드 버튼(우측 상단)
            h_title, h_btn = st.columns([6, 1], vertical_alignment="center")
            with h_title:
                st.markdown('<div class="card-head">✨ 전체 요약</div>', unsafe_allow_html=True)
            with h_btn:
                st.download_button(
                    ":material/download:",
                    data=build_report(vid, st.session_state.summary, st.session_state.highlights),
                    file_name=f"summary_{vid}.txt",
                    mime="text/plain",
                    help="요약을 텍스트 파일로 저장",
                    use_container_width=True,
                )
            # 본문(스크롤): 헤더는 위에 고정, 요약 내용만 스크롤
            with st.container(key="sum_body"):
                st.markdown(st.session_state.summary)
    with c_vid:
        with st.container(height=CARD_H, border=True, key="video_card"):
            st.video(f"https://www.youtube.com/watch?v={vid}")

    # 2단: 타임스탬프 핵심 | Q&A — 카드 높이는 CSS(.st-key-ts_card/.st-key-qa_card)로 동일 고정
    c_ts, c_qa = st.columns(2, gap="medium")

    with c_ts:
        with st.container(border=True, key="ts_card"):
            st.markdown('<div class="card-head">🕒 타임스탬프별 핵심 내용</div>', unsafe_allow_html=True)
            # 본문(스크롤): 헤더는 위에 고정, 행 목록만 스크롤
            with st.container(key="ts_body"):
                hls = st.session_state.highlights or []
                if hls:
                    rows = ""
                    for h in hls:
                        label = fmt_time(h["start"])
                        link = f"https://www.youtube.com/watch?v={vid}&t={h['start']}s"
                        rows += (
                            f'<div class="ts-row">'
                            f'<a class="ts-time" href="{link}" target="_blank">{label}</a>'
                            f'<span class="ts-text">{esc(h["point"])}</span>'
                            f'</div>'
                        )
                    st.markdown(rows, unsafe_allow_html=True)
                else:
                    st.caption("타임스탬프 핵심을 생성하지 못했어요. 다시 시도해보세요.")

    with c_qa:
        with st.container(border=True, key="qa_card"):
            st.markdown('<div class="card-head">💬 영상과 대화하기 (Q&A)</div>', unsafe_allow_html=True)

            # 채팅/추천 영역(스크롤되는 부분) — 고정 컨테이너로 감싸 구조 안정화(폼 중복 방지) + 이 영역만 스크롤
            with st.container(key="qa_body"):
                if not st.session_state.chat and not st.session_state.pending_q:
                    # 빈 상태: 안내 문구(제목 바로 아래 상단 고정) + 추천 질문(아래쪽 중앙)
                    st.markdown(
                        '<div class="qa-guide">이 영상을 바탕으로 궁금한 점을 자유롭게 질문해보세요.</div>',
                        unsafe_allow_html=True,
                    )
                    # 추천 질문(제목+칩)만 남은 공간에서 세로 중앙으로 → 입력창과 여백 균형
                    with st.container(key="sug_zone"):
                        st.markdown('<div class="qa-sug-head">💡 추천 질문</div>', unsafe_allow_html=True)
                        with st.container(key="suggestions"):
                            for i, sq in enumerate(SUGGESTED_QUESTIONS):
                                st.button(sq, key=f"sug{i}", on_click=ask_suggestion, args=(sq,))
                else:
                    # 채팅 상태: 말풍선 (+ 답변 생성 중이면 타이핑 로딩)
                    bubbles = '<div class="bubbles">'
                    for turn in st.session_state.chat:
                        role = "user" if turn["role"] == "user" else "bot"
                        content_html = (
                            esc(turn["content"]).replace("\n", "<br>")
                            if role == "user"
                            else markdownish_to_safe_html(turn["content"])
                        )
                        bubbles += (
                            f'<div class="bubble-row {role}"><div class="bubble {role}">'
                            f'{content_html}</div></div>'
                        )
                    if st.session_state.pending_q:
                        bubbles += (
                            '<div class="bubble-row bot"><div class="bubble bot">'
                            '<span class="typing"><span class="dot"></span>'
                            '<span class="dot"></span><span class="dot"></span></span></div></div>'
                        )
                    bubbles += "</div>"
                    st.markdown(bubbles, unsafe_allow_html=True)

            with st.form("qa_form", clear_on_submit=True):
                fcol, bcol = st.columns([5, 1], vertical_alignment="bottom")
                with fcol:
                    q = st.text_input(
                        "질문", placeholder="질문을 입력하세요...", label_visibility="collapsed"
                    )
                with bcol:
                    sent = st.form_submit_button("➤", use_container_width=True)

            # 1단계: 전송 → 내 질문 말풍선 즉시 표시 + 답변 대기 플래그 (바로 rerun)
            if sent and q and not st.session_state.pending_q:
                st.session_state.chat.append({"role": "user", "content": q})
                st.session_state.pending_q = q
                st.rerun()

            # 2단계: 대기 중이면(질문 말풍선+로딩이 위에 렌더된 뒤) AI 답변 생성 → 교체
            if st.session_state.pending_q:
                pq = st.session_state.pending_q
                try:
                    qa_context = build_qa_context(
                        st.session_state.transcript_text,
                        st.session_state.summary,
                        st.session_state.highlights,
                        pq,
                    )
                    ans = model_config.answer_question(
                        qa_context, pq, compact_chat_history(st.session_state.chat[:-1])
                    )
                except Exception as e:
                    ans = ai_error_msg(e)
                st.session_state.chat.append({"role": "assistant", "content": ans})
                st.session_state.pending_q = None
                st.rerun()

else:
    # 빈 화면(홈) — 한 화면 채우기: 예시 영상은 화면 중앙, 기능 카드는 푸터 바로 위(맨 아래)
    with st.container(key="home"):
        with st.container(key="ex_zone"):
            st.markdown(
                '<div class="ex-head">예시 영상으로 바로 시작해보세요</div>',
                unsafe_allow_html=True,
            )
            with st.container(key="exchips"):
                for i, ex in enumerate(EXAMPLES, 1):
                    st.button(
                        f'{ex["icon"]}  {ex["label"]}',
                        key=f"ex{i}",
                        on_click=use_example,
                        args=(ex["url"],),
                    )

        f1, f2, f3 = st.columns(3, gap="medium")
        feats = [
            (f1, "📄", "AI 요약", "핵심 내용을 깔끔하게 요약해 드립니다."),
            (f2, "🕒", "타임스탬프 핵심 정리", "중요한 내용이 언제 나오는지 확인하세요."),
            (f3, "💬", "영상 기반 Q&A", "영상 내용을 기반으로 궁금한 점을 물어보세요."),
        ]
        for col, ico, title, desc in feats:
            with col:
                with st.container(border=True):
                    st.markdown(
                        f'<div class="feat"><div class="feat-ico">{ico}</div>'
                        f'<div class="feat-title">{title}</div>'
                        f'<div class="feat-desc">{desc}</div></div>',
                        unsafe_allow_html=True,
                    )


# ----------------------------- 하단 기능 칩 -----------------------------
st.markdown(
    f"""
    <div class="chips">
        <span>✨ AI Powered ({esc(model_config.LLM_PROVIDER_NAME)} · {esc(model_config.LLM_MODEL)})</span>
        <span>📄 자막 기반 분석</span>
        <span>🛡️ 빠르고 정확한 요약</span>
    </div>
    """,
    unsafe_allow_html=True,
)


# ----------------------------- 분석 실행(토스트 오버레이) -----------------------------
# 본문과 하단 칩을 먼저 그린 뒤 오래 걸리는 작업을 실행해, 진행 상태가 화면을 대체하지 않게 한다.
if analysis_video_id:
    run_initial_analysis(analysis_video_id)
    st.rerun()
