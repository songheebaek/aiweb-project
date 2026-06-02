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
    .block-container { padding-top: 4.5rem; padding-bottom: 1rem; max-width: 1180px; }

    /* ----- 헤더 ----- */
    .hero { display: flex; align-items: center; gap: 18px; margin-bottom: 4px; }
    .yt-logo {
        width: 64px; height: 46px; background: #ff0000; border-radius: 14px;
        display: flex; align-items: center; justify-content: center; flex-shrink: 0;
        box-shadow: 0 6px 16px rgba(255,0,0,.25);
    }
    .yt-logo:after {
        content: ""; border-style: solid; border-width: 10px 0 10px 17px;
        border-color: transparent transparent transparent #fff; margin-left: 4px;
    }
    .hero-title { font-size: 2.6rem; font-weight: 800; line-height: 1.05; color: #15182b; letter-spacing: -1px; }
    .hero-sub { font-size: 1.05rem; color: #6b7392; margin: 14px 0 22px 82px; font-weight: 500; }

    /* ----- 카드 (st.container border 보강) ----- */
    div[data-testid="stVerticalBlockBorderWrapper"] {
        background: #ffffff; border: 1px solid #eceef5 !important;
        border-radius: 18px; box-shadow: 0 8px 30px rgba(40,50,90,.06);
        padding: 6px 10px;
    }
    .card-head { font-size: 1.12rem; font-weight: 700; color: #1f2438; margin: 4px 2px 12px; }

    /* ----- 기능 카드 (빈 화면) ----- */
    .feat { text-align: left; }
    .feat-ico { font-size: 1.6rem; }
    .feat-title { font-size: 1.08rem; font-weight: 700; color: #1f2438; margin-top: 6px; }
    .feat-desc { font-size: .92rem; color: #6b7392; margin-top: 4px; line-height: 1.45; }

    /* ----- 타임스탬프 행 ----- */
    .ts-row { display: flex; gap: 14px; padding: 9px 4px; border-bottom: 1px solid #f1f2f8; }
    .ts-row:last-child { border-bottom: none; }
    .ts-time { color: #16a34a; font-weight: 700; font-variant-numeric: tabular-nums;
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

    /* ----- 하단 기능 칩 ----- */
    .chips { display: flex; justify-content: center; gap: 38px; flex-wrap: wrap;
             margin: 26px 0 6px; color: #5b627e; font-weight: 600; font-size: .95rem; }
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
]:
    st.session_state.setdefault(key, default)


# ----------------------------- 헤더 -----------------------------
st.markdown(
    """
    <div class="hero">
        <div class="yt-logo"></div>
        <div>
            <div class="hero-title">AI YouTube Summarizer</div>
        </div>
    </div>
    <div class="hero-sub">영상의 핵심을 빠르게 파악하고, 궁금한 것은 바로 물어보세요</div>
    """,
    unsafe_allow_html=True,
)


# ----------------------------- URL 입력 바 -----------------------------
col_url, col_btn = st.columns([6, 1], vertical_alignment="bottom")
with col_url:
    url = st.text_input(
        "URL",
        placeholder="요약하고 싶은 영상 url을 입력해주세요. (https://www.youtube.com/watch?v=xxxxxxxxx)",
        label_visibility="collapsed",
    )
with col_btn:
    analyze = st.button("요약하기", type="primary", use_container_width=True)


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

    with st.spinner("AI가 요약하는 중... (조금 걸릴 수 있어요)"):
        try:
            st.session_state.summary = model_config.summarize_video(st.session_state.transcript_text)
            st.session_state.highlights = model_config.extract_highlights(st.session_state.transcript_text)
        except Exception as e:
            st.error(ai_error_msg(e)); st.stop()


# ----------------------------- 본문 -----------------------------
if st.session_state.summary:
    vid = st.session_state.video_id

    # 1단: 전체 요약 | 영상
    # 영상 박스는 자동 높이(영상에 딱 맞게 → 위아래 여백 균등), 요약 박스는 영상 높이에 맞춰 고정.
    SUMMARY_H = 310
    c_sum, c_vid = st.columns([1, 1], gap="medium")
    with c_sum:
        with st.container(height=SUMMARY_H, border=True):
            # 헤더 행: 제목(좌) + 아이콘 다운로드 버튼(우측 상단)
            h_title, h_btn = st.columns([6, 1], vertical_alignment="center")
            with h_title:
                st.markdown('<div class="card-head">✨ 전체 요약</div>', unsafe_allow_html=True)
            with h_btn:
                st.download_button(
                    "⬇",
                    data=build_report(vid, st.session_state.summary, st.session_state.highlights),
                    file_name=f"summary_{vid}.txt",
                    mime="text/plain",
                    help="요약을 텍스트 파일로 저장",
                    use_container_width=True,
                )
            st.markdown(st.session_state.summary)
    with c_vid:
        with st.container(border=True):
            st.video(f"https://www.youtube.com/watch?v={vid}")

    # 2단: 타임스탬프 핵심 | Q&A
    c_ts, c_qa = st.columns(2, gap="medium")

    with c_ts:
        with st.container(border=True):
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
        with st.container(border=True):
            st.markdown('<div class="card-head">💬 영상과 대화하기 (Q&A)</div>', unsafe_allow_html=True)

            bubbles = '<div class="bubbles">'
            if not st.session_state.chat:
                bubbles += '<div class="bubble-row bot"><div class="bubble bot">영상 내용에 대해 무엇이든 물어보세요. 자막에 근거해 답합니다.</div></div>'
            for turn in st.session_state.chat:
                role = "user" if turn["role"] == "user" else "bot"
                bubbles += (
                    f'<div class="bubble-row {role}"><div class="bubble {role}">'
                    f'{esc(turn["content"])}</div></div>'
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

            if sent and q:
                st.session_state.chat.append({"role": "user", "content": q})
                try:
                    ans = model_config.answer_question(
                        st.session_state.transcript_text, q, st.session_state.chat[:-1]
                    )
                except Exception as e:
                    ans = ai_error_msg(e)
                st.session_state.chat.append({"role": "assistant", "content": ans})
                st.rerun()

else:
    # 빈 화면 — 썸네일 왼쪽의 3개 기능 소개 카드
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
