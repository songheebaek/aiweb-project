# YouTube 영상 요약기 (Streamlit + LLM provider) → Oracle E2.1.Micro 배포용 이미지
# week10/스트레스 몬스터 Dockerfile 패턴 재활용. 런타임만 Gradio→Streamlit으로 교체.
#
# 빌드:  docker build -t youtube-summarizer:latest .
# 실행:  docker run -d -p 8501:8501 --env-file .env youtube-summarizer:latest
# (compose 권장 → docker-compose.yml 참조)

FROM python:3.11-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 의존성 먼저 (레이어 캐시)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# 앱 소스
COPY app.py model_config.py model_config_gemini.py model_config_openai.py ./

EXPOSE 8501

ENV PYTHONUNBUFFERED=1

# Streamlit 헬스 엔드포인트로 헬스체크 (1GB RAM에서 죽으면 docker 재기동)
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health').read()" || exit 1

# 0.0.0.0 바인딩 + headless (서버 환경에서 브라우저 자동실행/통계수집 끔)
CMD ["streamlit", "run", "app.py", \
     "--server.address=0.0.0.0", \
     "--server.port=8501", \
     "--server.headless=true", \
     "--browser.gatherUsageStats=false"]
