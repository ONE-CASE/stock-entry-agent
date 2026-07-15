# Stock Entry Agent

매크로(유동성/신용/레짐) + 개별종목 펀더멘털(분기 매출·영업이익 YoY) + 가격행동(모멘텀·RSI·볼린저밴드·MDD)을
결합해 "오늘 살펴볼 진입 후보"를 매일 뽑아주는 개인용 스톡 스크리닝 에이전트.

## 구조

- `Stock_Entry_Agent.py` — 메인 파이프라인. 매주 월요일에만 FMP로 전체 유니버스를 스캔해
  `output/watchlist.csv`를 갱신하고, 나머지 요일은 그 워치리스트만으로 가격/RSI/MDD를 빠르게 체크한다.
- `Growth_Trigger_Calibration.py` — 과거 3년 데이터로 "실적 급등 트리거" 임계값을 실증적으로
  재계산해 `output/growth_trigger_thresholds.json`에 저장한다(분기 실적 시즌마다 재실행 권장).
- `2026_Global_Master_Universe_1000.csv` — 스캔 대상 종목 유니버스(TICKER, THEME).
- `assets/fonts/NanumBarunGothic.ttf` — 클라우드(리눅스) 러너에도 한글 차트가 깨지지 않도록 번들.
- `output/` — 매일 갱신되는 결과(CSV/HTML), 캐시, 히스토리. GitHub Actions가 매일 커밋해서 남긴다.
- `docs/index.html` — 최신 리포트를 GitHub Pages로 공개하기 위한 복사본.

## 로컬 실행

```
pip install -r requirements.txt
python Stock_Entry_Agent.py
```

## 필요한 환경변수

| 변수 | 용도 | 필수 |
|---|---|---|
| `FMP_API_KEY` | 분기 매출/영업이익 수집 | O |
| `FRED_API_KEY` | 매크로 지표(유동성/신용스프레드 등) | O |
| `SLACK_WEBHOOK_URL` | 결과를 Slack으로 전송 | Slack 쓸 때만 |
| `ENABLE_SLACK` | `true`면 Slack 전송 활성화 | 기본 false |

## GitHub Actions로 매일 자동 실행 (PC 절전모드와 무관)

`.github/workflows/daily.yml`이 평일 09:00(KST) 자동 실행되도록 설정되어 있다. 저장소를 GitHub에
올린 뒤 아래 두 가지를 설정해야 한다:

1. **Repository → Settings → Secrets and variables → Actions** 에서 Secret 3개 등록
   - `FMP_API_KEY`, `FRED_API_KEY`, `SLACK_WEBHOOK_URL`
2. **Repository → Settings → Pages** 에서 Source를 "Deploy from a branch", Branch를
   `main` / `/docs` 로 설정 → 부여되는 URL이 매일 갱신되는 홈페이지가 된다.
   ⚠️ 무료 GitHub Pages는 **공개** 페이지다(URL을 아는 사람은 누구나 열람 가능, 검색엔진 노출은 안 됨).

Actions 탭에서 "Run workflow" 버튼으로 수동 실행도 가능하다(즉시 테스트 용도).

## 로컬 Windows 작업 스케줄러와의 관계

기존에 등록한 `StockEntryAgent_Daily`(Windows 작업 스케줄러, 평일 09:00)는 GitHub Actions와
중복 실행되면 Slack 알림이 두 번 오므로, 클라우드 실행이 정상 확인되면 비활성화하는 것을 권장한다.

```powershell
Disable-ScheduledTask -TaskName "StockEntryAgent_Daily"
```
