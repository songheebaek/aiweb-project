# 👹 나의 스트레스 몬스터 — 11주차 기말 프로젝트

> 사용자가 오늘의 스트레스를 한 문장 이상 입력하면 AI가 스트레스 유형을 분석해 귀엽고 웃긴 스트레스 몬스터 캐릭터 카드를 생성하는 서비스.
>
> 인프라/배포 구조는 10주차 칼로리카운터 zip을 그대로 재활용했고, 콘텐츠만 스트레스 몬스터로 교체했다.

---

## 1. 한 장 요약

- **트랙**: A. LLM API 챗봇 (OpenAI API + UI)
- **인프라**: OCI Oracle E2.1.Micro + Docker + nginx + Cloudflare (10주차와 동일)
- **모델**:
  - 텍스트 분석: `gpt-4o-mini` (OpenAI Chat API, JSON mode + LangChain LCEL)
  - 이미지 생성: `dall-e-3` (OpenAI Images API)
- **UI**: Gradio 5
- **완료 도메인**: `<id>-demo.aiweb2026.site`

## 2. 입력 → AI 처리 → 출력

```
사용자 스트레스 텍스트
   │
   ▼
[LCEL 체인] gpt-4o-mini (JSON mode)
   prompt | ChatOpenAI | JsonOutputParser
   │
   ▼
JSON {stress_type, stress_index, monster_name, traits, weakness, description, image_prompt}
   │
   ▼
[Images API] DALL·E 3  ← image_prompt(영문)
   │
   ▼
🪪 오늘의 스트레스 몬스터 카드 (이미지 + 마크다운)
```

## 3. 폴더 구조 (10주차 zip과 동일)

```
stress-monster/
├── README.md                         ← 이 파일
├── .gitignore                        ← .env 제외 (필수)
│
├── app.py                            ← Gradio UI + LCEL 체인 + text-to-image 호출
├── model_config.py                   ← 모델 상수 + 토큰 로더
├── requirements.txt                  ← Python 의존성
│
├── Dockerfile                        ← 컨테이너 이미지 정의
├── docker-compose.yml                ← 운영용 compose (메모리 제한, 헬스체크)
├── .env.example                      ← OPENAI_API_KEY 자리 (.env로 복사 후 입력)
│
├── nginx-stress-monster.conf         ← nginx reverse proxy (80번만, HTTPS는 Cloudflare가 처리)
│
└── .github/
    └── workflows/
        └── deploy.yml                ← GitHub Actions CI/CD 정의
```

## 4. 로컬 빠른 실행

```powershell
# Windows PowerShell
cp .env.example .env
# .env 안에 OPENAI_API_KEY=hf_xxx 입력

# (옵션) 가상환경
python -m venv .venv
.\.venv\Scripts\Activate.ps1

pip install -r requirements.txt
python app.py
# → http://127.0.0.1:7860
```

Docker로 실행:

```bash
docker compose up -d --build
docker compose logs -f
# → http://127.0.0.1:7860
```

## 5. Oracle 배포 (10주차 STAGE 1 절차 그대로)

```bash
# 학생 PC에서
scp -i ~/.ssh/oracle_key -r ~/stress-monster ubuntu@<본인_IP>:~/

# 서버에서
ssh -i ~/.ssh/oracle_key ubuntu@<본인_IP>
cd ~/stress-monster
cp .env.example .env && vi .env       # OPENAI_API_KEY 입력
chmod 600 .env

docker compose up -d --build          # ~3분
docker compose logs -f                # "Running on local URL" 확인
curl -I http://127.0.0.1:7860         # HTTP/1.1 200 OK
```

nginx 설치 + ID 치환:

```bash
sudo apt update && sudo apt install -y nginx
sudo cp ~/stress-monster/nginx-stress-monster.conf /etc/nginx/sites-available/stress-monster
sudo vi /etc/nginx/sites-available/stress-monster
# __STUDENT_ID__ → 본인 ID(s01, s02, ...) 치환

sudo ln -s /etc/nginx/sites-available/stress-monster /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

→ `https://<본인ID>-demo.aiweb2026.site` 접속 → Gradio UI → 스트레스 문장 입력 → 몬스터 카드 응답.

## 6. CI/CD (10주차 STAGE 2 절차 그대로)

GitHub 리포 → Settings → Secrets → 4개 등록:

| Secret | 값 |
|---|---|
| `SSH_HOST` | Oracle Public IP |
| `SSH_USER` | `ubuntu` |
| `SSH_KEY` | `~/.ssh/oracle_key` 전체 내용 (개행 포함) |
| `OPENAI_API_KEY` | `hf_xxx` |

```bash
git push origin main
```

→ GitHub Actions 그린 체크 → `<id>-demo.aiweb2026.site` 새로고침 → 변경 즉시 반영.

## 7. 적격성 체크리스트

- [x] AI 기능이 핵심에 있음 (단순 CRUD/회원관리가 아님)
- [x] inference만 필요 (모델 학습 불필요)
- [x] 모델 1GB 이하 또는 외부 API 호출 (HF Inference API 호출)
- [x] 한 요청 30초 이내 (FLUX schnell 4-step 고속 생성, 콜드스타트 시 첫 호출만 예외)
- [x] 영구 저장 불필요 (세션 내 표시만)
- [x] 데이터 출처 적격 (사용자가 직접 입력한 텍스트만 사용)
- [x] 학기 후 본인 3계정(GitHub + Cloudflare + OCI)으로 유지 가능
- [x] 완료 조건 한 줄로 명확

## 8. 자주 막히는 함정

| # | 증상 | 처리 |
|---|------|------|
| 1 | `OPENAI_API_KEY 환경변수가 비어 있습니다` | `.env`에 키 넣고 컨테이너 재기동 (`docker compose up -d`) |
| 2 | DALL·E 3 호출이 10~20초 걸림 | 정상 (이미지 생성 1회 호출이 그 정도). 사용자에게 로딩 표시 |
| 3 | JSON 파싱 실패 | gpt-4o-mini JSON mode 사용 중이라 거의 없음 — 재시도 |
| 4 | 401 invalid_api_key | OpenAI 콘솔에서 키 재발급 후 `.env` 갱신 |
| 5 | 429 / quota exceeded | OpenAI 결제 한도 초과 → 결제 카드 등록 또는 사용량 확인 |
| 6 | 분석 무한 로딩 (UI는 뜸) | nginx WebSocket 헤더 누락 — `nginx-stress-monster.conf` 그대로 쓰면 OK |

---

**작성일**: 2026-05-14
**관련**: 10주차 칼로리카운터 zip 인프라 재활용
