# 키워드 검색량 · 수요판단 서비스

네이버 검색광고와 API HUB를 묶어, 제품 키워드 하나로 시장 규모·수요 등급·미해결 불만을 보는 사내용 도구입니다.

**운영 중인 주소** — https://keyword-service-rcny.onrender.com (비밀번호 필요)

---

## 무엇을 하는가

| 탭 | 하는 일 |
|---|---|
| **검색량 조회** | 키워드의 월간 PC·모바일 검색량, 연관 키워드, 36개월 추이 그래프, 전년 대비 증감, 성수기·비수기 |
| **수요판단** | 키워드 하나로 6개 항목을 자동 채점해 A~D 등급 판정. 월 검색량 1만이 기준선 |
| **미해결 신호** | 지식iN·카페·블로그 글을 모아 AI가 읽고, 아직 안 풀린 불만과 제품 방향을 정리 |

---

## 수요판단 기준

**월 검색량 1만 회가 기준선입니다.** 이 값을 넘으면 "수요 있음"으로 보고, 못 넘으면 다른
항목이 아무리 좋아도 C 위로는 올라가지 않습니다. 화면의 *수요 기준 검색량* 칸에서
카테고리에 맞게 바꿀 수 있고, 바꾼 값은 브라우저에 저장됩니다.

배점은 기준선(B)의 배수로 계산하므로, 기준을 바꾸면 연관 수요 배점까지 같이 움직입니다.

| 항목 | 배점 | 만점 조건 |
|---|---|---|
| 핵심 검색량 | 25 | 기준의 10배. 기준을 딱 채우면 18점 |
| 연관 수요 | 20 | 문제형 연관 검색량 합이 기준의 2배 |
| 구매의도 | 15 | 연관 검색량 중 구매형 30% 이상 |
| 경쟁강도 | 10 | 낮음 10 · 중간 9 · 높음 6 |
| 광고 포화도 | 10 | 노출 광고 3~6개. 0개는 7점 |
| 검색 추세 | 15 | 전년 대비 +20% 이상. 보합(±5%)도 10점 |

등급 컷 — **A 80점 · B 62점 · C 48점 · D 그 아래.** 추세 데이터가 없으면 85점 만점을
100점으로 환산합니다.

바꾼 이유:

- 예전에는 3만 회를 넘어야 검색량 만점이라, 1만~3만대의 멀쩡한 생활용품이 전부 C·D로
  깔렸습니다. 1만을 통과선으로 놓고 그 위를 배수로 벌렸습니다.
- 경쟁 '높음'을 15점 중 5점으로 크게 깎던 것을 10점 중 6점으로 줄였습니다. 경쟁이 높다는
  건 그 카테고리에 돈이 돌고 있다는 뜻이기도 해서, 차별화 난이도만큼만 반영합니다.
- 광고가 많으면 0점이던 항목을 뒤집었습니다. 광고가 **아예 없는** 쪽이 오히려 아직 돈이
  안 도는 시장이라 만점을 주지 않습니다.
- 검색 추세를 36개월 첫 달 대 끝 달 비교에서 **전년 대비(최근 12개월 합 vs 직전 12개월 합)**
  로 바꿨습니다. 예전 방식은 그 두 달이 성수기냐 비수기냐에 따라 값이 통째로 흔들려서
  같은 키워드도 조회 시점마다 등급이 달라졌습니다. 보합도 이제 15점 중 10점을 받습니다.
- 전년 대비 20% 이상 빠지는 키워드에는 등급과 별도로 경고 문구가 붙습니다.

---

## 필요한 계정과 키

세 곳에서 발급받아야 합니다. 셋 다 무료이거나 종량제입니다.

### 1. 네이버 검색광고 (검색량)

`searchad.naver.com` → 사업자 광고주 가입 → 상단 **도구 → API 사용 관리**

발급받을 값 세 개:

| 화면 표기 | 환경변수 |
|---|---|
| 엑세스라이선스 | `NAVER_API_KEY` |
| 비밀키 | `NAVER_SECRET_KEY` |
| CUSTOMER_ID | `NAVER_CUSTOMER_ID` |

무료입니다. 광고를 집행하지 않아도 API는 쓸 수 있습니다.

### 2. NAVER API HUB (추이·검색)

`ncloud.com` → 콘솔 → **NAVER API HUB** → Application 등록

사용 API에서 아래를 모두 체크:
- 검색어트렌드 (Data Lab) — 36개월 추이용
- 지식iN, 카페, 블로그 (NAVER 검색) — 미해결 신호용

발급되는 Client ID / Secret 을 각각 `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET` 에 넣습니다.

한도: 검색어트렌드 월 3만 건, 검색 API 하루 2.5만 건까지 무료.

> 주의 — 개발자센터(`developers.naver.com`)가 아니라 **클라우드 플랫폼**입니다. 인증 헤더가 다릅니다.

### 3. Anthropic (AI 검수)

`console.anthropic.com` → API Keys → Create Key → `ANTHROPIC_API_KEY`

Billing 에서 결제 수단 등록이 필요합니다. 조회당 약 5원, 결과는 3일 캐시됩니다.
이 키가 없으면 AI 항목만 빠지고 나머지는 정상 작동합니다.

---

## 로컬에서 돌리기

```bash
git clone https://github.com/nkiss2152-sudo/naver.git
cd naver
pip install -r requirements.txt

cp .env.example .env      # 값 채우기
export $(grep -v '^#' .env | xargs)   # macOS / Linux
python app.py
```

Windows PowerShell:

```powershell
$env:NAVER_API_KEY="..."
$env:NAVER_SECRET_KEY="..."
$env:NAVER_CUSTOMER_ID="..."
$env:NAVER_CLIENT_ID="..."
$env:NAVER_CLIENT_SECRET="..."
$env:ANTHROPIC_API_KEY="..."
$env:ACCESS_PASSWORD="..."
python app.py
```

http://localhost:8000 접속.

---

## 배포 (Render)

이미 배포돼 있습니다. 새 환경에 다시 올릴 때만 아래를 따르세요.

1. GitHub 저장소 준비
2. `render.com` 가입 (GitHub 계정으로)
3. **+ New → Blueprint** → 저장소 선택 → `render.yaml` 이 설정을 자동으로 채웁니다
4. `sync: false` 로 표시된 키 다섯 개는 대시보드에서 직접 입력
5. Deploy

코드를 수정해 `main` 에 커밋하면 자동으로 재배포됩니다.

### 배포 후 화면이 안 바뀔 때

브라우저가 이전 `index.html` 을 캐시한 경우입니다. **Ctrl + Shift + R** 로 강제 새로고침하세요.

---

## 환경변수

| 이름 | 필수 | 기본값 | 설명 |
|---|---|---|---|
| `NAVER_API_KEY` | ● | – | 검색광고 엑세스라이선스 |
| `NAVER_SECRET_KEY` | ● | – | 검색광고 비밀키 |
| `NAVER_CUSTOMER_ID` | ● | – | 검색광고 고객 ID |
| `NAVER_CLIENT_ID` | | – | API HUB Client ID (추이·검색) |
| `NAVER_CLIENT_SECRET` | | – | API HUB Client Secret |
| `ANTHROPIC_API_KEY` | | – | AI 검수용. 없으면 AI 항목만 빠짐 |
| `ACCESS_PASSWORD` | | 빈값 | 사내 접근 비밀번호. 비우면 누구나 접속 |
| `AI_MODEL` | | claude-haiku-4-5-20251001 | 검수 모델 |
| `TREND_MONTHS` | | 36 | 추이 조회 개월 수 |
| `CACHE_TTL` | | 86400 | 검색량 캐시 (초) |
| `TREND_TTL` | | 604800 | 추이 캐시 (초) |
| `RATE_LIMIT` | | 20 | IP당 허용 횟수 |
| `RATE_WINDOW` | | 600 | 위 횟수를 세는 구간(초) |
| `DAILY_UPSTREAM_CAP` | | 3000 | 하루 네이버 호출 상한 |
| `SEARCH_TTL` | | 259200 | 불만 분석 캐시(초) |
| `AI_TTL` | | 259200 | AI 결과 캐시(초) |
| `PORT` | | 8000 | 로컬 실행 포트 |
| `DB_PATH` | | keyword_cache.db | 캐시 파일 경로 |

---

## 구조

```
app.py          Flask 서버 — API 호출, 캐시, 채점, AI 검수
index.html      화면 전체 (탭 3개, 단일 파일)
render.yaml     Render 배포 설정
requirements.txt
Procfile        gunicorn 실행 명령
```

`app.py` 의 주요 부분:

| 함수 | 역할 |
|---|---|
| `call_naver` | 검색광고 키워드도구 호출 (HMAC 서명) |
| `fetch_trend` / `trend_stats` | 36개월 추이와 연간 요약 계산 |
| `search_docs` | 지식iN·카페·블로그 검색 |
| `analyze_complaints` | 글 수집 → 관련성 검사 → 불만 표현 분류 |
| `extract_candidates` | 조사·어미를 떼고 자주 나온 말 집계 |
| `ai_review` | 후보와 글 제목을 AI에 넘겨 미해결 문제 도출 |

### API 엔드포인트

| 경로 | 용도 |
|---|---|
| `POST /api/search` | 검색량 + 추이. body: `{keywords:[], related:bool}` |
| `POST /api/unmet` | 불만 분석. body: `{keyword:"", ai:bool}` |
| `POST /api/login` | 비밀번호 확인 |
| `GET /healthz` | 각 기능 활성 여부 확인 |

비밀번호가 설정돼 있으면 `X-Access-Password` 헤더가 필요합니다.

---

## 알아둘 한계

**검색량 추이는 추정치입니다.** 데이터랩은 상대지수만 주므로, 마지막 달 지수를 현재 검색량에 맞춰 나머지 달을 환산합니다.

**AI 검수는 제목과 요약만 봅니다.** 본문 전체는 읽지 않으므로, 근거로 제시된 글이 정말 불만인지는 원문을 열어 확인하는 편이 안전합니다.

**카페·블로그에는 광고글이 섞입니다.** AI가 할인·공동구매·협찬 리뷰를 걸러내도록 지시했지만 완벽하지는 않습니다.

**무료 플랜은 15분 무접속 시 잠듭니다.** 다음 방문자의 첫 요청이 1분쯤 걸립니다. 유료 플랜(월 7달러)으로 없앨 수 있습니다.

**캐시는 재시작 시 사라집니다.** 무료 플랜은 디스크가 초기화되므로 캐시가 비워지지만, 다시 채워질 뿐 고장은 아닙니다.

**약관을 한 번 확인하세요.** 네이버 검색광고 API 이용약관은 본인 광고 계정 관리·분석을 전제로 합니다. 사내 사용은 문제없지만 외부 공개나 재판매로 확장할 때는 검색광고 고객센터에 문의해 두는 편이 안전합니다.
