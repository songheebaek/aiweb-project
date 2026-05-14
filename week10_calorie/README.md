# 10주차 칼로리카운터 — Oracle 배포 + GitHub Actions CI/CD

> 이 zip은 10주차 수업에서 학생이 e-class로 받는 자료입니다.
> 6주차에 만든 칼로리카운터 코드 + Docker + nginx + CI/CD가 모두 들어있어, **학생은 본인 ID와 HF_TOKEN만 입력하면** 한 차시 안에 본인 칼로리카운터가 `<id>-demo.aiweb2026.site`로 떠야 합니다.

상세 절차는 [`docs/10_week10_lesson.md`](../../docs/10_week10_lesson.md) 본 강의안 참고.

---

## 0. 진행 순서 — 두 단계로 나뉨 (헷갈리지 말 것)

```
┌─────────────────────────┐    ┌─────────────────────────┐
│  STAGE 1: 수동 배포      │    │  STAGE 2: 자동 배포      │
│  (zip 업로드)           │    │  (CI/CD)                │
│                        │ →  │                        │
│  zip → scp → unzip     │    │  GitHub push           │
│  → docker → nginx       │    │  → Actions → SSH       │
│  → 시트 입력            │    │  → docker rebuild      │
│                        │    │                        │
│  목표: 한 번 떠 있는 것  │    │  목표: 앞으로 자동      │
│  확인 (§ 3~6)           │    │  배포 되도록 (§ 7~8)    │
└─────────────────────────┘    └─────────────────────────┘
```

**먼저 STAGE 1로 본인 도메인에 칼로리카운터가 뜨는 것 확인 → 그 다음 STAGE 2로 자동화.** STAGE 1을 건너뛰고 STAGE 2부터 시작하면 처음 동작 자체가 안 떠서 디버깅이 불가능.

---

## 1. zip 안에 들어있는 것

```
week10_calorie/
├── README.md                        ← 이 파일
├── .gitignore                       ← .env 제외 (필수)
│
├── app.py                           ← 6주차 칼로리카운터 코드
├── model_config.py                  ← 모델 상수 + InferenceClient
├── requirements.txt                 ← Python 의존성
│
├── Dockerfile                       ← 컨테이너 이미지 정의
├── docker-compose.yml               ← 운영용 compose (메모리 제한, 헬스체크)
├── .env.example                     ← HF_TOKEN 자리 (.env로 복사 후 입력)
│
├── nginx-calorie.conf               ← nginx reverse proxy (80번만, HTTPS는 Cloudflare가 처리)
│
└── .github/
    └── workflows/
        └── deploy.yml               ← GitHub Actions CI/CD 정의
```

---

## 2. 학생이 직접 만들 것 — 단 3개

| 항목 | 내용 |
|------|------|
| `.env` | `cp .env.example .env` 후 `HF_TOKEN=hf_xxx` 입력 |
| nginx 설정의 `__STUDENT_ID__` 치환 | `s01`, `s02` … 본인 ID로 |
| 시트의 본인 행 입력 | Page Link `http://<본인 Public IP>` + Public IP 컬럼에 IP |

나머지는 zip 동봉본 그대로 사용. **HTTPS 인증서는 학생이 만질 일 없음** — Cloudflare Universal SSL이 `*.aiweb2026.site` 자동 처리.

---

## 3. 빠른 시작 (수업 중 슬롯 매핑)

### ── STAGE 1: 수동 배포 (먼저 한 번 떠 있는 것 확인) ──

### 3-1. Oracle 서버에 zip 업로드 (§ 4-2)

```bash
# 학생 PC에서
scp -i ~/.ssh/oracle_key week10_calorie.zip ubuntu@<본인_IP>:~/

# 서버에서
ssh -i ~/.ssh/oracle_key ubuntu@<본인_IP>
sudo apt install -y unzip          # OCI Ubuntu 22.04 cloud image에 기본 미포함
unzip ~/week10_calorie.zip -d ~/calorie
cd ~/calorie
```

### 3-2. .env 작성 (§ 4-3)

```bash
cp .env.example .env
vi .env
# HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxxxxxx
chmod 600 .env
```

### 3-3. 첫 컨테이너 기동 (§ 4-4)

```bash
docker compose up -d --build       # ~3분
docker compose logs -f             # "Running on local URL" 확인
curl -I http://127.0.0.1:7860      # HTTP/1.1 200 OK
```

### 3-4. nginx 설치 + 본인 ID 치환 (§ 5)

```bash
sudo apt update && sudo apt install -y nginx
sudo cp ~/calorie/nginx-calorie.conf /etc/nginx/sites-available/calorie-counter
sudo vi /etc/nginx/sites-available/calorie-counter
# __STUDENT_ID__ → 본인 ID(s01, s02, ...) 치환

sudo ln -s /etc/nginx/sites-available/calorie-counter /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

### 3-5. 시트에 본인 Public IP 입력 (§ 2 참고)

분배 시트의 본인 행에서 **D열 (Public IP)**만 추가 입력:
- D: **Public IP** = `<본인 Oracle Public IP>` (예: `168.110.127.64`, IP만)

C열(Page Link)은 9주차에 본인 소개 페이지 URL로 이미 채워져 있음. Worker가 한 행을 두 매핑으로 풀어줌:
- `s01.aiweb2026.site` → C (본인 소개 페이지)
- `s01-demo.aiweb2026.site` → `http://D` (Oracle 데모)

5분 안에 Cloudflare Worker 캐시 새로고침되어 `https://<id>-demo.aiweb2026.site` 자동 라우팅.

브라우저 검증:
```
https://<본인ID>-demo.aiweb2026.site
→ 자물쇠 (Cloudflare Universal SSL) + Gradio UI
→ 음식 사진 업로드 → 칼로리 응답
```

**여기까지 STAGE 1 끝.** 본인 도메인에 칼로리카운터가 떠 있는 것 확인 후 STAGE 2로 진행.

### ── STAGE 2: 자동 배포 (앞으로 push만 하면 자동 갱신) ──

### 3-6. GitHub 리포 생성 + Secret 등록 (§ 7)

```bash
# 학생 PC에서, .env 빼고 push
git clone https://github.com/<본인>/my-calorie-counter.git
cd my-calorie-counter
cp -r ~/Downloads/week10_calorie/* .

# .gitignore가 .env 차단
cat .gitignore | grep -E "^\.env$"

git add .
git commit -m "init: 10주차 칼로리카운터 + CI/CD"
git push origin main
```

GitHub 리포 → Settings → Secrets → 4개 등록:
- `SSH_HOST` = Oracle Public IP
- `SSH_USER` = `ubuntu`
- `SSH_KEY` = `~/.ssh/oracle_key` 전체 내용 (개행 포함)
- `HF_TOKEN` = `hf_xxx`

### 3-7. 첫 자동 배포 (§ 8-3)

```bash
# 학생 PC에서, my-calorie-counter 리포에 변경 push
vi app.py    # title 한 글자 변경
git commit -am "test: 첫 자동 배포"
git push origin main
```

→ GitHub Actions 그린 체크 → `<id>-demo.aiweb2026.site` 새로고침 → 변경 즉시 반영.

### 3-8. 9주차 페이지 "Live Demo" 살리기 (§ 9)

```bash
# 9주차 페이지 리포에서
vi index.html
# <a href="#">Live Demo (Coming Week 10)</a>
# → <a href="https://<본인ID>-demo.aiweb2026.site" target="_blank">Live Demo</a>

git commit -am "feat: Live Demo 링크 활성화"
git push origin main
```

→ 1~2분 후 `<id>.aiweb2026.site` 새로고침 → 버튼이 살아남.

---

## 4. 자주 막히는 함정

| # | 증상 | 처리 |
|---|------|------|
| 1 | `Permissions 0644 for ssh key are too open` | `chmod 600 ~/.ssh/oracle_key` |
| 2 | `Connection timed out` (SSH) | OCI Security List에서 22번 포트 허용 |
| 3 | docker compose 첫 빌드 OOM | swap 2GB 설정 (§ 3-2) |
| 4 | 외부에서 80번 접속 안 됨 | iptables REJECT 위에 ALLOW 추가 (§ 3-3) + Security List 80 ingress |
| 5 | 502 Bad Gateway | `docker compose ps` → 죽었으면 `logs`로 OOM/HF_TOKEN 누락 확인 |
| 6 | `<id>-demo.aiweb2026.site` 접속 시 404 "학생 페이지 등록 안 됨" | 시트 Page Link 비어있음 또는 5분 캐시 대기 |
| 7 | 분석 무한 로딩 (UI는 뜸) | nginx WebSocket 헤더 누락 — `nginx-calorie.conf` 그대로 쓰면 OK |
| 8 | GitHub Actions `Permission denied (publickey)` | SSH_KEY가 CRLF로 들어감. 개행 포함 통째로 다시 등록 |

---

## 5. 학기 종료 후 인프라 유지

| 자산 | 학기 종료 후 |
|------|-------------|
| Oracle Always Free 인스턴스 | 영구 무료. 본인 OCI 계정에서 관리 |
| 본인 GitHub 리포 + Actions | 영구 무료 (Public 리포 무제한) |
| `aiweb2026.site` 도메인 | 강사가 1년 후 갱신 안 함. 학기 한정 |
| Cloudflare Worker 라우팅 | 강사 자산. 학기 종료 시 폐기 |
| HF_TOKEN | 본인 계정. 영구 |

→ 본인 도메인 구매(`Cloudflare_Domain_Setup_Guide.md`)로 학기 종료 후 본인 인프라로 이전 가능.

---

**작성일**: 2026-05-06
**관련 강의**: [10주차 강의안](../../docs/10_week10_lesson.md), [9주차 강의안](../../docs/09_week09_lesson.md)
