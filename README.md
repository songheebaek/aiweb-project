# 🎬 YouTube 영상 요약기

유튜브 URL만 넣으면 자막을 추출해 **AI 요약 · 타임스탬프별 핵심 · Q&A**를 제공하는 Streamlit 앱.
AI웹(aiweb2026) 기말 프로젝트 — **트랙 A (LLM API)**.

## 스택
- **UI**: Streamlit
- **자막 추출**: `youtube-transcript-api`
- **AI**: Google **Gemini 2.5 Flash** (무료 티어)
- **배포**: Oracle E2.1.Micro + Docker + nginx + Cloudflare → `<id>-demo.aiweb2026.site`

## 기능
1. URL 입력 → 한국어/영어 자막 자동 추출
2. 전체 내용 3~5문단 요약
3. 타임스탬프별 핵심 구간 정리
4. 영상 내용에 대한 Q&A (자막 근거 기반)

## 로컬 실행
```bash
python -m venv .venv
.venv\Scripts\activate          # (mac/linux: source .venv/bin/activate)
pip install -r requirements.txt

copy .env.example .env          # (mac/linux: cp) → .env에 GOOGLE_API_KEY 입력
streamlit run app.py            # http://localhost:8501
```
API 키는 [Google AI Studio](https://aistudio.google.com/apikey)에서 무료 발급.

## 배포 (Oracle + Docker)
```bash
docker compose up -d --build    # http://서버:8501
# nginx-youtube-summarizer.conf 의 __STUDENT_ID__ 채워서 reverse proxy 설정
```

## ⚠️ 알려진 이슈 — 클라우드 자막 차단
유튜브는 데이터센터 IP(Oracle 등)의 자막 요청을 차단하는 경우가 많습니다.
로컬은 되는데 배포 서버에서 자막 추출이 실패하면 `.env`에 `PROXY_URL`을 설정하세요.
```
PROXY_URL=http://user:pass@host:port
```
