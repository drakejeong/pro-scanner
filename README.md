# PRO Scanner 셋업 가이드

쇼핑몰 신상품을 1시간마다 자동으로 모니터링하고 텔레그램으로 알림을 받는 시스템입니다.

## 📦 구성

```
pro-scanner/
├── crawler/              ← 서버에서 돌아가는 Python 크롤러
│   ├── scanner.py        ← 메인 로직 (RSS → sitemap → Gemini 순)
│   └── requirements.txt
├── dashboard/            ← 웹 대시보드 (Firebase Hosting 또는 정적 호스팅)
│   └── index.html
└── .github/workflows/
    └── crawl.yml         ← 1시간마다 자동 실행
```

---

## ✅ 사장님이 직접 하실 일 (순서대로)

### STEP 1. GitHub 저장소 만들기 (5분)

1. https://github.com/new 에서 새 저장소 생성 (**Private 추천**)
2. 이 폴더의 파일들을 통째로 업로드 (또는 git push)

```bash
cd pro-scanner
git init
git add .
git commit -m "initial commit"
git branch -M main
git remote add origin https://github.com/사장님계정/저장소이름.git
git push -u origin main
```

---

### STEP 2. Firebase 서비스 계정 키 발급 (5분)

서버에서 Firestore에 쓰려면 일반 API 키가 아니라 **서비스 계정 JSON**이 필요합니다.

1. https://console.firebase.google.com → `drake130-8d318` 프로젝트 선택
2. ⚙️ 설정 → **프로젝트 설정** → **서비스 계정** 탭
3. **새 비공개 키 생성** 클릭 → JSON 파일 다운로드
4. 다운로드한 파일을 메모장으로 열어서 **전체 내용 복사** (중괄호 포함)

---

### STEP 3. Gemini API 키 새로 발급 (3분)

기존 키는 HTML에 노출됐으니 폐기하고 새로 발급하세요.

1. https://aistudio.google.com/app/apikey
2. **Create API key** → 키 복사
3. (기존 키는 같은 페이지에서 삭제)

---

### STEP 4. 텔레그램 봇 만들기 (5분)

1. 텔레그램에서 `@BotFather` 검색 → 대화 시작
2. `/newbot` 입력 → 봇 이름 정하기 (예: `pro_scanner_bot`)
3. 받은 **HTTP API Token** 복사 (`123456:ABC-DEF...` 형태)
4. 본인 텔레그램에서 만든 봇과 대화 시작 (아무 메시지 1번 전송)
5. 브라우저에서 다음 주소 열기:
   ```
   https://api.telegram.org/bot{토큰}/getUpdates
   ```
6. 응답 JSON에서 `"chat":{"id":숫자` 부분의 **숫자가 chat_id** (이거 복사)

---

### STEP 5. GitHub Secrets 등록 (5분)

GitHub 저장소 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

다음 4개를 각각 등록:

| Name | Value |
|---|---|
| `FIREBASE_CREDENTIALS_JSON` | STEP 2에서 복사한 JSON 전체 |
| `GEMINI_API_KEY` | STEP 3에서 받은 키 |
| `TELEGRAM_BOT_TOKEN` | STEP 4에서 받은 봇 토큰 |
| `TELEGRAM_CHAT_ID` | STEP 4에서 받은 chat_id (숫자) |

---

### STEP 6. 동작 테스트 (1분)

GitHub 저장소 → **Actions** 탭 → **PRO Scanner Hourly Crawl** 선택
→ 우측 **Run workflow** 버튼 클릭 → **Run workflow** 한 번 더

성공하면 ✅ 표시. 실패하면 클릭해서 로그 확인.

---

### STEP 7. 대시보드 호스팅 (선택)

`dashboard/index.html`을 어디든 올리면 됩니다. 무료 옵션 3가지:

**옵션 A. Firebase Hosting (권장 — 같은 프로젝트라 간편)**
```bash
npm install -g firebase-tools
cd dashboard
firebase login
firebase init hosting       # 프로젝트는 drake130-8d318 선택, public 디렉토리는 . 선택
firebase deploy
```

**옵션 B. GitHub Pages**
- 저장소 Settings → Pages → Source: `main` 브랜치, `/dashboard` 폴더

**옵션 C. 그냥 로컬에서 열기**
- `index.html` 더블클릭 (단, 본인 컴퓨터에서만 보임)

---

## 🔍 동작 확인 방법

1. **로그**: GitHub → Actions → 최근 실행 클릭하면 어떤 사이트가 어떤 전략(RSS/sitemap/Gemini)으로 성공했는지 보임
2. **대시보드**: 1시간 후 자동 갱신, 신상품은 파란색 NEW 뱃지로 표시
3. **텔레그램**: 신상품 발견 시 푸시 알림 (최초 등록 직후 첫 스캔에서는 알림 안 옴 — 비교 대상이 없으니)

---

## ⚠️ 주의사항

- **첫 등록 후 첫 스캔**은 모든 상품이 "기준점"으로 저장됨 → 알림 X
- **두 번째 스캔부터** 신상품 비교 시작 → 알림 O
- GitHub Actions 무료 한도: 월 2,000분 (Private 저장소 기준) — 1시간 주기면 월 ~30분 정도 사용
- Gemini 무료 한도: 분당 15회 — 사이트 30개 이하면 충분
- 일부 사이트는 봇 차단(403)으로 모든 전략이 실패할 수 있음. 이 경우 대시보드에 빨간색 ⚠ 에러 표시됨

---

## 🛠️ 문제 해결

| 증상 | 원인 | 해결 |
|---|---|---|
| Actions 실행 시 `FIREBASE_CREDENTIALS_JSON` 에러 | Secret 미등록 | STEP 5 다시 |
| 모든 사이트 "크롤링 실패" | 봇 차단 또는 JS 렌더링 사이트 | 해당 사이트만 사이트별 맞춤 크롤러 추가 필요 |
| 텔레그램 알림 안 옴 | chat_id 잘못됨 또는 봇과 대화 안 시작함 | STEP 4 다시 |
| Gemini 폴백 자주 실패 | API 키 한도 초과 | 잠시 후 재시도, 또는 결제 활성화 |
