# 6주차 칼로리카운터(Gradio 5 + LangChain + HF Inference) → Oracle E2.1.Micro 배포용 이미지
# 9주차 수업 자산. 6주차 코드(app.py, model_config.py, requirements.txt) 그대로 재활용.
#
# 빌드:   docker build -t calorie-counter:latest .
# 실행:   docker run -d -p 7860:7860 --env-file .env calorie-counter:latest
# (compose 사용 권장 → docker-compose.yml 참조)

FROM python:3.11-slim

# 시스템 의존성 (Pillow JPEG/PNG 디코딩용)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        libjpeg62-turbo \
        libpng16-16 \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 의존성 먼저 설치 (캐시 최적화: requirements.txt만 안 바뀌면 이 레이어 재사용)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# 앱 소스 (6주차 구조 그대로)
COPY app.py model_config.py ./

EXPOSE 7860

# 환경변수
# - SPACE_ID: 6주차 app.py의 is_space 분기를 트리거 → server_name="0.0.0.0" 바인딩
#             (원래 HF Space 환경 감지용. Docker에서도 동일 효과로 재활용)
# - GRADIO_SERVER_NAME: 방어적으로 추가 (Gradio가 미래에 explicit param 없으면 이걸로 fallback)
# - PYTHONUNBUFFERED: print/log 즉시 stdout (docker logs로 실시간 확인용)
ENV SPACE_ID=docker-oracle \
    GRADIO_SERVER_NAME=0.0.0.0 \
    GRADIO_SERVER_PORT=7860 \
    PYTHONUNBUFFERED=1

# 헬스체크 (1GB RAM에서 OOM으로 죽으면 docker가 재기동)
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7860/').read()" || exit 1

CMD ["python", "app.py"]
