"""YouTube 영상 요약기 — Streamlit 프론트엔드.

URL 입력 → 자막 추출(youtube-transcript-api) → Gemini로 요약 / 타임스탬프 핵심 / Q&A.
UI는 프로젝트 썸네일 목업 디자인을 따름 (헤더 → URL바 → 요약|영상 → 타임스탬프|Q&A → 기능 칩).

로컬 실행:  streamlit run app.py
배포:       Dockerfile/docker-compose 참조 (포트 8501)
"""

import os
import re

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
from youtube_transcript_api.proxies import GenericProxyConfig

import model_config

load_dotenv()

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
    "이 영상의 핵심 내용은 무엇인가요?",
    "가장 중요한 인사이트는 무엇인가요?",
    "3줄로 요약해주세요",
    "초보자도 이해할 수 있게 설명해주세요",
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


def fetch_transcript(video_id: str) -> list:
    """자막 세그먼트 [{text, start, duration}] 리스트 반환.

    youtube-transcript-api 1.x 인스턴스 API 사용. 클라우드 IP 차단 대비 PROXY_URL 지원.
    """
    proxy_url = os.getenv("PROXY_URL")
    proxy_config = (
        GenericProxyConfig(http_url=proxy_url, https_url=proxy_url) if proxy_url else None
    )
    api = YouTubeTranscriptApi(proxy_config=proxy_config)
    return api.fetch(video_id, languages=TRANSCRIPT_LANGS).to_raw_data()


def build_timestamped_text(segments: list) -> str:
    """[분:초] 텍스트 형태로 합쳐 AI에 넘길 문자열 생성."""
    return "\n".join(f"[{fmt_time(seg['start'])}] {seg['text']}" for seg in segments)


def esc(text: str) -> str:
    """HTML 삽입 시 최소 이스케이프."""
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


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
    """AI 호출 오류를 사용자 친화적 한국어 메시지로."""
    s = str(e)
    if any(k in s for k in ("503", "UNAVAILABLE", "high demand", "429", "overloaded")):
        return "지금 Gemini 서버가 일시적으로 혼잡해요(과부하). 잠시 후 다시 시도해주세요. 🙏"
    return f"AI 처리 중 오류가 발생했습니다: {e}"


def use_example(example_url: str) -> None:
    """예시 버튼 클릭 시 URL 입력창에 자동 입력 (on_click 콜백)."""
    st.session_state.url_input = example_url


def ask_suggestion(question: str) -> None:
    """추천 질문 클릭 시 바로 질문 전송 (on_click 콜백). 이후 rerun에서 답변 생성."""
    if not st.session_state.pending_q:
        st.session_state.chat.append({"role": "user", "content": question})
        st.session_state.pending_q = question


# ----------------------------- 페이지 설정 + 스타일 -----------------------------
st.set_page_config(
    page_title="AI YouTube Summarizer",
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
    /* ----- 전체 배경 / 여백 ----- */
    .stApp { background: linear-gradient(180deg, #f7f8fc 0%, #eef0f8 100%); }
    /* Streamlit 기본 헤더/메뉴(다크·라이트 토글 등)는 유지. 제목과 안 겹치게 상단 여백만 확보. */
    .block-container { padding-top: 4.5rem; padding-bottom: 72px; max-width: 1180px; }

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

    /* ----- 카드 (st.container border 보강) ----- */
    div[data-testid="stVerticalBlockBorderWrapper"] {
        background: #ffffff; border: 1px solid #eceef5 !important;
        border-radius: 18px; box-shadow: 0 8px 30px rgba(40,50,90,.06);
        padding: 6px 10px;
    }
    .card-head { font-size: 1.12rem; font-weight: 700; color: #1f2438; margin: 4px 2px 12px; }
    /* 타임스탬프 박스: 최대 높이를 Q&A 빈 상태 기본 높이(약 330px)에 맞춤. 초과 시 스크롤 */
    .st-key-ts_card { max-height: 330px; overflow-y: auto; }
    /* Q&A 박스: 채팅이 길어지면 380까지 늘었다가 스크롤 */
    .st-key-qa_card { max-height: 380px; overflow-y: auto; }
    /* 요약·영상 박스를 같은 고정 높이로(아래 N) → 항상 같은 크기. 영상은 박스 안에서 세로 중앙 정렬. */
    .st-key-video_card div[data-testid="stVerticalBlock"] { height: 100%; justify-content: center; align-items: center; }

    /* ----- 기능 카드 (빈 화면) ----- */
    .feat { text-align: center; padding-bottom: 14px; }
    .feat-ico { font-size: 1.6rem; }
    .feat-title { font-size: 1.08rem; font-weight: 700; color: #1f2438; margin-top: 6px; }
    .feat-desc { font-size: .92rem; color: #6b7392; margin-top: 4px; line-height: 1.45; }

    /* ----- 타임스탬프 행 ----- */
    .ts-row { display: flex; gap: 14px; padding: 9px 4px; border-bottom: 1px solid #f1f2f8; }
    .ts-row:last-child { border-bottom: none; }
    .ts-time { color: #18cf15; font-weight: 700; font-variant-numeric: tabular-nums;
               text-decoration: none; flex-shrink: 0; min-width: 52px; }
    .ts-time:hover { text-decoration: underline; }
    .ts-text { color: #2b3047; font-size: .96rem; line-height: 1.4; }

    /* ----- Q&A 말풍선 ----- */
    .bubbles { min-height: 60px; }
    .bubble-row { display: flex; margin: 8px 0; }
    .bubble-row.user { justify-content: flex-end; }
    .bubble { padding: 10px 14px; border-radius: 16px; max-width: 85%; font-size: .95rem; line-height: 1.45; }
    .bubble.user { background: #eef0fb; color: #2b3047; border-bottom-right-radius: 5px; }
    .bubble.bot { background: #f4f5f9; color: #2b3047; border-bottom-left-radius: 5px; }
    /* 답변 생성 중 타이핑 로딩(점 3개 애니메이션) */
    .typing { display: inline-flex; gap: 5px; align-items: center; }
    .typing .dot { width: 7px; height: 7px; border-radius: 50%; background: #aeb4cc;
                   animation: typing-blink 1.2s infinite both; }
    .typing .dot:nth-child(2) { animation-delay: .2s; }
    .typing .dot:nth-child(3) { animation-delay: .4s; }
    @keyframes typing-blink { 0%,80%,100% { opacity: .25; transform: translateY(0); }
                              40% { opacity: 1; transform: translateY(-2px); } }

    /* ----- Q&A 빈 상태: 안내 문구 + 추천 질문 pill ----- */
    .qa-guide { font-size: .92rem; color: #8a86b8; margin: 2px 2px 16px; line-height: 1.45; }
    .qa-sug-head { font-size: .9rem; font-weight: 700; color: #6b6f86; margin: 2px 2px 10px; }
    .st-key-suggestions { flex-direction: row !important; flex-wrap: wrap !important;
        gap: 8px !important; width: 100% !important; align-items: stretch !important; }
    .st-key-suggestions > [data-testid="stElementContainer"] { flex: 0 0 calc(50% - 4px) !important; width: calc(50% - 4px) !important; }
    .st-key-suggestions .stButton { width: 100% !important; }
    .st-key-suggestions .stButton > button {
        width: 100% !important; height: 100%;
        background: #f6f5fc; color: #5b5876; border: 1px solid #ebe9f6;
        border-radius: 999px; font-weight: 500; font-size: .85rem;
        padding: .34rem .9rem; min-height: 0; box-shadow: none; white-space: normal;
    }
    .st-key-suggestions .stButton > button:hover { background: #efecfb; color: #5a4bd4; border-color: #d8d2f3; }

    /* ----- 버튼: 보라색 ----- */
    .stButton > button, .stFormSubmitButton > button, .stDownloadButton > button {
        background: #6c5ce7; color: #fff; border: none; border-radius: 12px;
        font-weight: 700; padding: .55rem 1.1rem;
    }
    .stButton > button:hover, .stFormSubmitButton > button:hover, .stDownloadButton > button:hover {
        background: #5a4bd4; color: #fff;
    }

    /* ----- 입력창 ----- */
    .stTextInput input { border-radius: 12px; }

    /* ----- 예시 영상 칩 (요약 전 화면) — 가로 한 줄 알약 ----- */
    .ex-head { font-size: .95rem; font-weight: 700; color: #4b5168; margin: 2px 2px 12px; }
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


# ----------------------------- 분석 실행 -----------------------------
if analyze:
    video_id = extract_video_id(url)
    if not video_id:
        st.error("유효한 유튜브 URL이 아닙니다. 주소를 다시 확인해주세요.")
        st.stop()

    if video_id != st.session_state.video_id:
        st.session_state.update(video_id=video_id, summary=None, highlights=None, chat=[])

    with st.spinner("자막을 추출하는 중..."):
        try:
            segments = fetch_transcript(video_id)
        except TranscriptsDisabled:
            st.error("이 영상은 자막이 비활성화되어 있어 요약할 수 없습니다."); st.stop()
        except NoTranscriptFound:
            st.error(f"한국어/영어 자막을 찾지 못했습니다. (지원: {', '.join(TRANSCRIPT_LANGS)})"); st.stop()
        except VideoUnavailable:
            st.error("영상을 찾을 수 없습니다. 비공개이거나 삭제된 영상일 수 있어요."); st.stop()
        except (IpBlocked, RequestBlocked):
            st.error("유튜브가 이 서버의 IP를 차단했습니다. 클라우드 배포 환경이라면 `.env`에 PROXY_URL을 설정하세요."); st.stop()
        except CouldNotRetrieveTranscript:
            st.error("자막을 가져오지 못했습니다. 클라우드 서버라면 IP 차단일 수 있어요 (PROXY_URL 설정 필요)."); st.stop()
        except Exception as e:
            st.error(f"자막 추출 중 오류가 발생했습니다: {e}"); st.stop()

    st.session_state.segments = segments
    st.session_state.transcript_text = build_timestamped_text(segments)

    with st.spinner("영상 요약하는 중... (조금 걸릴 수 있어요)"):
        try:
            st.session_state.summary = model_config.summarize_video(st.session_state.transcript_text)
            st.session_state.highlights = model_config.extract_highlights(st.session_state.transcript_text)
        except Exception as e:
            st.error(ai_error_msg(e)); st.stop()


# ----------------------------- 본문 -----------------------------
if st.session_state.summary:
    vid = st.session_state.video_id

    # 1단: 전체 요약 | 영상 — 둘 다 같은 고정 높이로 박스 크기 통일 (영상은 박스 안 세로 중앙 정렬)
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
            st.markdown(st.session_state.summary)
    with c_vid:
        with st.container(height=CARD_H, border=True, key="video_card"):
            st.video(f"https://www.youtube.com/watch?v={vid}")

    # 2단: 타임스탬프 핵심 | Q&A
    c_ts, c_qa = st.columns(2, gap="medium")

    with c_ts:
        with st.container(border=True, key="ts_card"):
            st.markdown('<div class="card-head">🕒 타임스탬프별 핵심 내용</div>', unsafe_allow_html=True)
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

            if not st.session_state.chat and not st.session_state.pending_q:
                # 빈 상태: 안내 문구 + 추천 질문 pill (첫 질문 전까지)
                st.markdown(
                    '<div class="qa-guide">이 영상을 바탕으로 궁금한 점을 자유롭게 질문해보세요.</div>'
                    '<div class="qa-sug-head">💡 추천 질문</div>',
                    unsafe_allow_html=True,
                )
                with st.container(key="suggestions"):
                    for i, sq in enumerate(SUGGESTED_QUESTIONS):
                        st.button(sq, key=f"sug{i}", on_click=ask_suggestion, args=(sq,))
            else:
                # 채팅 상태: 말풍선 (+ 답변 생성 중이면 타이핑 로딩)
                bubbles = '<div class="bubbles">'
                for turn in st.session_state.chat:
                    role = "user" if turn["role"] == "user" else "bot"
                    bubbles += (
                        f'<div class="bubble-row {role}"><div class="bubble {role}">'
                        f'{esc(turn["content"])}</div></div>'
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
                    ans = model_config.answer_question(
                        st.session_state.transcript_text, pq, st.session_state.chat[:-1]
                    )
                except Exception as e:
                    ans = ai_error_msg(e)
                st.session_state.chat.append({"role": "assistant", "content": ans})
                st.session_state.pending_q = None
                st.rerun()

else:
    # 빈 화면 — 예시 영상 칩(가로 한 줄, 클릭 시 입력창 자동 입력) + 3개 기능 소개 카드
    st.markdown(
        '<div class="ex-head">▶&nbsp;&nbsp;예시 영상으로 바로 시작해보세요</div>',
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

    st.write("")
    f1, f2, f3 = st.columns(3, gap="medium")
    feats = [
        (f1, "📄", "AI 요약", "핵심 내용을 깔끔하게 요약해 드립니다."),
        (f2, "🕒", "타임스탬프 핵심 정리", "중요한 내용이 언제 나오는지 확인하세요."),
        (f3, "💬", "영상과 대화하기 (Q&A)", "영상 내용을 기반으로 궁금한 점을 물어보세요."),
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
        <span>✨ AI Powered (Gemini · {esc(model_config.GEMINI_MODEL)})</span>
        <span>📄 자막 기반 분석</span>
        <span>🛡️ 빠르고 정확한 요약</span>
    </div>
    """,
    unsafe_allow_html=True,
)
