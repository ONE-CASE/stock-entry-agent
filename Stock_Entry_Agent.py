# -*- coding: utf-8 -*-
"""
Stock_Entry_Agent.py
=====================
매크로(유동성/신용/레짐) + 개별종목 펀더멘털(분기 매출·영업이익 YoY) +
가격행동(모멘텀·상대강도·RSI·MDD)을 결합해 "오늘 살펴볼 진입 후보"를
로컬 CSV/HTML 리포트로 뽑아주는 통합 스크립트.

기존 4개 스크립트(1.STOCK_CANDIDATE.PY, 2.STOCK.PY, 3.Recommended_STOCK.PY,
4.STOCK_BI.PY)와 매크로 엔진(1.StockRaw.py)을 하나의 파이프라인으로 합쳤다.
Google Sheets 연동은 제외. Slack은 SLACK_WEBHOOK_URL 환경변수가 설정되고
ENABLE_SLACK=true일 때만 TOP5 요약을 전송한다(기본은 로컬 저장만).

실행: python Stock_Entry_Agent.py
"""

import os
import sys
import time
import numpy as np
import pandas as pd
import requests
import yfinance as yf
from datetime import datetime, timedelta
from fredapi import Fred

# Windows 콘솔 기본 코드페이지(cp949)에서 이모지 출력 시 UnicodeEncodeError 방지
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# =========================================================
# 0. 설정
# =========================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT_CSV = os.path.join(BASE_DIR, "2026_Global_Master_Universe_1000.csv")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
CACHE_DIR = os.path.join(OUTPUT_DIR, "cache")

FMP_API_KEY = os.getenv("FMP_API_KEY", "iS3VC5nd1HinXpsfr4hCmCS4oYHARFRf")
FRED_API_KEY = os.getenv("FRED_API_KEY", "911d87da6a0f07cef751e81b6268730d")

# 테스트 모드: True면 TEST_TICKERS(+ FORCE_TICKERS)만 돌려서 파이프라인 검증.
# FMP가 유료 플랜으로 전환되어 실전 운영은 False(전체 유니버스)로 설정한다.
TEST_MODE = False
TEST_TICKERS = ["AAPL", "NVDA", "MSFT"]

# 유니버스 스코어링과 무관하게 항상 최종 리포트에 포함할 티커(삼성전자/SK하이닉스)
FORCE_TICKERS = ["005930.KS", "000660.KS"]
FORCE_THEME = "AI_Semicon"

# ---- 펀더멘털(분기 매출/영업이익) ----
LIMIT_Q = 8                      # FMP에서 가져올 분기 수
FUNDAMENTALS_MAX_AGE_DAYS = 7     # 캐시 유효기간(분기 실적이라 자주 안 바뀜)
# 아래 두 값은 기본값(fallback)이며, Growth_Trigger_Calibration.py를 실행하면
# output/growth_trigger_thresholds.json에 저장된 실증 기반 값으로 자동 대체된다.
YOY_GROWTH_MIN = 0.40
ACCEL_MIN = 0.15
REV_ACCEL_CAP = 0.5
USE_TURNAROUND = True
STEP2_SCORE_MIN = 60
THRESHOLDS_JSON_PATH_NAME = "growth_trigger_thresholds.json"

# ---- 주간/일간 분리 ----
# FMP 전체 유니버스 스캔(느림)은 주 1회(월요일)만 수행하고, 그 결과(워치리스트)를
# 나머지 요일에는 그대로 재사용해서 "가격/RSI/MDD만" 매일 갱신한다.
# 이렇게 하면 매일 489종목 전체를 다시 스캔할 필요가 없어 실행 시간이 크게 줄어든다.
WEEKLY_REFRESH_WEEKDAY = 0        # 0=월요일 (datetime.weekday() 기준)
WATCHLIST_MAX_AGE_DAYS = 7        # 이보다 오래되면 요일과 무관하게 강제 재스캔
WATCHLIST_PATH_NAME = "watchlist.csv"

# ---- 가격행동(모멘텀/RS/ATR/RSI/MDD/볼린저밴드) ----
LOOKBACK_DAYS = 420
RSI_PERIOD = 14
MDD_WINDOW = 252                 # 최근 1년(거래일) 고점 대비 낙폭
ATR_PERIOD = 20
BB_PERIOD = 20                    # 볼린저밴드 기준(20일 SMA ± 2표준편차)
BB_STD_MULT = 2.0
MA_LONG_PERIOD = 200              # 장기추세 필터(200일선)
STEP3_SCORE_MIN = 50
MIN_DOLLAR_VOL_20D = 5_000_000
ATR_MAX_PCT = 0.25
BENCH_SPY, BENCH_QQQ = "SPY", "QQQ"
MARKET_TICKER = "QQQ"

RSI_OVERBOUGHT = 70
RSI_DIP_LOW, RSI_DIP_HIGH = 25, 35   # "RSI 30 부근" 눌림목 매수 구간
BB_LOWER_TOUCH_PCTB = 0.10            # %B <= 0.10 이면 밴드 하단 근접/터치로 간주
PULLBACK_MIN, PULLBACK_MAX = -0.35, -0.05   # "건강한 눌림목" 구간(1년 고점 대비)
DEEP_RISK_MDD = -0.50                        # 최근 1년 최악 낙폭이 이보다 깊으면 리스크 플래그

# ---- 최종 대시보드 ----
STOP_ATR_MULTIPLIER = 1.5

# ---- Slack 알림 ----
# 보안: 실제 웹훅 URL은 코드에 하드코딩하지 않는다. 환경변수로만 주입.
#   PowerShell 예시: [Environment]::SetEnvironmentVariable("SLACK_WEBHOOK_URL","https://hooks.slack.com/...","User")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
ENABLE_SLACK = os.getenv("ENABLE_SLACK", "false").strip().lower() == "true"
SLACK_TOP_N = 10

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "stock-entry-agent/1.0"})


# =========================================================
# 1. 공통 유틸
# =========================================================
def sget(df: pd.DataFrame, col: str) -> pd.Series:
    return df[col] if col in df.columns else pd.Series(np.nan, index=df.index)


def rolling_z(s: pd.Series, window: int = 252, eps: float = 1e-9) -> pd.Series:
    mu = s.rolling(window, min_periods=max(60, window // 5)).mean()
    sd = s.rolling(window, min_periods=max(60, window // 5)).std().clip(lower=eps)
    z = (s - mu) / sd
    return z.replace([np.inf, -np.inf], np.nan).fillna(0)


def calc_velocity(s: pd.Series, window: int = 20, eps: float = 1e-9) -> pd.Series:
    mu = s.rolling(window, min_periods=window).mean()
    sd = s.rolling(window, min_periods=window).std().clip(lower=eps)
    v = (s - mu) / sd
    return v.replace([np.inf, -np.inf], np.nan).fillna(0)


def calc_rsi_series(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50)


def atomic_write_csv(df: pd.DataFrame, out_path: str, encoding="utf-8-sig") -> str:
    out_dir = os.path.dirname(out_path)
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    tmp_path = out_path.replace(".csv", f"_tmp_{ts}.csv")
    df.to_csv(tmp_path, index=False, encoding=encoding)
    try:
        os.replace(tmp_path, out_path)
        return out_path
    except PermissionError:
        fb_path = out_path.replace(".csv", f"_fallback_{ts}.csv")
        os.replace(tmp_path, fb_path)
        print(f"⚠️ 파일 잠금 중 → fallback 저장: {fb_path}")
        return fb_path


def safe_get_json(url, params, retries=3, timeout=8):
    backoff = 1.2
    last_err = None
    for i in range(1, retries + 1):
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                time.sleep(backoff * i)
                continue
            if r.status_code == 403:
                raise RuntimeError(f"403 Forbidden: {params.get('symbol')}")
            if 400 <= r.status_code < 500:
                r.raise_for_status()
            if 500 <= r.status_code < 600:
                if i < retries:
                    time.sleep(0.3)
                    continue
                r.raise_for_status()
            return r.json()
        except requests.exceptions.Timeout:
            last_err = "timeout"
            continue
        except Exception as e:
            last_err = e
            break
    raise RuntimeError(f"HTTP 실패: {last_err}")


def normalize_yf_long(data: pd.DataFrame, tickers: list) -> pd.DataFrame:
    """yf.download(group_by='ticker') 결과를 DATE/TICKER/OHLCV 롱포맷으로 정리"""
    rows = []
    if isinstance(data.columns, pd.MultiIndex):
        top = set(data.columns.get_level_values(0))
        for t in tickers:
            if t not in top:
                continue
            sub = data[t].dropna(subset=["Close"]).reset_index()
            sub["TICKER"] = t
            rows.append(sub)
    else:
        sub = data.dropna(subset=["Close"]).reset_index()
        sub["TICKER"] = tickers[0]
        rows.append(sub)

    if not rows:
        return pd.DataFrame()

    out = pd.concat(rows, ignore_index=True)
    out = out.rename(columns={
        "Date": "DATE", "Open": "OPEN", "High": "HIGH",
        "Low": "LOW", "Close": "CLOSE", "Volume": "VOLUME",
    })
    out["DATE"] = pd.to_datetime(out["DATE"])
    out["TICKER"] = out["TICKER"].astype(str).str.upper().str.strip()
    return out.sort_values(["TICKER", "DATE"]).reset_index(drop=True)


def download_price_history(tickers: list, start: datetime, end: datetime) -> pd.DataFrame:
    data = yf.download(
        tickers=tickers, start=start.strftime("%Y-%m-%d"), end=end.strftime("%Y-%m-%d"),
        auto_adjust=True, group_by="ticker", progress=False, threads=True,
    )
    return normalize_yf_long(data, tickers)


# =========================================================
# 2. STAGE 1 — 매크로 엔진 (1.StockRaw.py 축약판)
# =========================================================
MACRO_SERIES = [
    {"name": "SPY", "src": "yahoo", "ticker": "SPY"},
    {"name": "VIX", "src": "yahoo", "ticker": "^VIX"},
    {"name": "GOLD", "src": "yahoo", "ticker": "GC=F"},
    {"name": "BTC", "src": "yahoo", "ticker": "BTC-USD"},
    {"name": "HYG", "src": "yahoo", "ticker": "HYG"},
    {"name": "IEF", "src": "yahoo", "ticker": "IEF"},
    {"name": "FED_BS", "src": "fred", "sid": "WALCL", "freq": "W", "lag": 1},
    {"name": "TGA", "src": "fred", "sid": "WTREGEN", "freq": "W", "lag": 1},
    {"name": "RRP", "src": "fred", "sid": "RRPONTSYD", "freq": "D", "lag": 1},
    {"name": "HY_OAS", "src": "fred", "sid": "BAMLH0A0HYM2", "freq": "D", "lag": 0},
    {"name": "CPI", "src": "fred", "sid": "CPIAUCSL", "freq": "M", "lag": 15, "yoy": True},
    {"name": "ICSA", "src": "fred", "sid": "ICSA", "freq": "W", "lag": 7},
]


def run_macro_engine(years: int = 6) -> dict:
    """매크로 유동성/신용/변동성 지표로 시장 컨피던스 스코어와 레짐을 계산해
    최신 스냅샷(dict)을 반환. 전체 시계열은 output/macro_panel.csv에 저장."""
    print("📊 [STAGE 1] 매크로 엔진 실행 중...")
    end = pd.Timestamp.today().normalize()
    start = end - pd.DateOffset(years=years)
    daily_index = pd.date_range(start=start, end=end, freq="B")
    panel = pd.DataFrame(index=daily_index)

    yahoo_items = [s for s in MACRO_SERIES if s["src"] == "yahoo"]
    tickers_map = {s["name"]: s["ticker"] for s in yahoo_items}
    try:
        raw_y = yf.download(list(tickers_map.values()), start=start, end=end,
                             progress=False, auto_adjust=True)
        close = raw_y["Close"].rename(columns={v: k for k, v in tickers_map.items()})
        panel = panel.join(close.reindex(daily_index).ffill())
    except Exception as e:
        print(f"⚠️ 매크로 Yahoo 데이터 실패: {e}")

    fred = Fred(api_key=FRED_API_KEY)
    for item in [s for s in MACRO_SERIES if s["src"] == "fred"]:
        name, sid = item["name"], item["sid"]
        try:
            raw_s = fred.get_series(sid).sort_index().loc[start - pd.DateOffset(years=1):end]
            lag_days = item.get("lag", 0)
            if item.get("yoy"):
                yoy = raw_s.pct_change(12)
                yoy.index = yoy.index + pd.Timedelta(days=lag_days)
                panel[f"{name}_YOY"] = yoy.reindex(daily_index, method="ffill")
            raw_s.index = raw_s.index + pd.Timedelta(days=lag_days)
            panel[name] = raw_s.reindex(daily_index, method="ffill")
        except Exception as e:
            print(f"⚠️ FRED {name}({sid}) 실패: {e}")
            panel[name] = np.nan

    panel = panel.sort_index().ffill()

    panel["FED_BS_USD_BN"] = sget(panel, "FED_BS") / 1000.0
    panel["TGA_USD_BN"] = sget(panel, "TGA") / 1000.0
    panel["RRP_USD_BN"] = sget(panel, "RRP")
    panel["REAL_LIQUIDITY_USD_BN"] = (
        sget(panel, "FED_BS_USD_BN") - sget(panel, "RRP_USD_BN") - sget(panel, "TGA_USD_BN")
    )
    panel["LIQ_ROC_20"] = sget(panel, "REAL_LIQUIDITY_USD_BN").pct_change(20)
    panel["HY_DIFF20"] = sget(panel, "HY_OAS").diff(20)

    core_cols = ["SPY", "FED_BS_USD_BN", "RRP_USD_BN", "TGA_USD_BN", "HY_OAS"]
    panel["DATA_OK"] = pd.concat([sget(panel, c) for c in core_cols], axis=1).notna().all(axis=1).astype(int)

    # Physics 엔진: 가격 가속도 + 유동성 가속도 + 신용스프레드 속도 → 백분위 스코어
    eng = panel.copy()
    for col in ["SPY", "REAL_LIQUIDITY_USD_BN", "HY_OAS", "BTC"]:
        eng[f"{col}_V"] = sget(eng, col).pct_change(5)
        eng[f"{col}_A"] = eng[f"{col}_V"].diff(5)

    raw_physics = (
        sget(eng, "SPY_A").rolling(120).rank(pct=True) * 0.35
        + sget(eng, "REAL_LIQUIDITY_USD_BN_A").rolling(120).rank(pct=True) * 0.35
        + sget(eng, "HY_OAS_V").rolling(120).rank(pct=True, ascending=False) * 0.30
    )
    panel["MARKET_CONFIDENCE_SCORE"] = np.where(
        panel["DATA_OK"] == 1,
        raw_physics.rolling(504, min_periods=60).apply(lambda x: x.rank(pct=True).iloc[-1] * 100, raw=False),
        np.nan,
    )

    panel["HY_Z"] = rolling_z(sget(panel, "HY_OAS"), window=120)
    panel["LIQ_Z"] = rolling_z(sget(panel, "LIQ_ROC_20"), window=252)
    MCS = panel["MARKET_CONFIDENCE_SCORE"].astype(float)
    panel["MCS_SLOPE"] = (MCS - MCS.shift(30)) / 30.0
    panel["MCS_SLOPE_Z"] = rolling_z(sget(panel, "MCS_SLOPE"), window=252)
    panel["VIX_Z"] = rolling_z(sget(panel, "VIX"), window=252)

    cond_mcs = -sget(panel, "MCS_SLOPE_Z") >= 0.7
    cond_hy = (sget(panel, "HY_Z") >= 1.0) & (sget(panel, "HY_DIFF20") > 0)
    cond_liq = (-sget(panel, "LIQ_Z") >= 0.7) & (sget(panel, "LIQ_ROC_20") < 0)
    panel["TRIPLE_BRAKE"] = (cond_mcs & cond_hy & cond_liq).astype(int)
    panel["OVERHEAT"] = ((MCS >= 90) & ((sget(panel, "VIX_Z") > 0.6) | (-sget(panel, "LIQ_Z") > 0.3))).astype(int)

    def classify(row):
        if row["DATA_OK"] != 1:
            return "DATA_ERROR"
        if row["TRIPLE_BRAKE"] == 1:
            return "RISK_OFF (Triple Brake)"
        if row["OVERHEAT"] == 1:
            return "OVERHEAT (No Chase)"
        if row["MARKET_CONFIDENCE_SCORE"] >= 45:
            return "RISK_ON (Aggressive OK)"
        return "NEUTRAL (Selective)"

    panel["REGIME_NAME"] = panel.apply(classify, axis=1)

    def score_to_lev(s):
        if pd.isna(s):
            return np.nan
        if s >= 85:
            return 0.2
        if s >= 65:
            return np.interp(s, [65, 85], [1.0, 0.2])
        if s >= 45:
            return np.interp(s, [45, 65], [2.5, 1.0])
        if s >= 25:
            return np.interp(s, [25, 45], [2.0, 2.5])
        return 0.0

    panel["TARGET_LEVERAGE"] = panel["MARKET_CONFIDENCE_SCORE"].apply(score_to_lev)
    panel = panel.assign(DATE=panel.index.strftime("%Y-%m-%d"))

    atomic_write_csv(panel.reset_index(drop=True), os.path.join(OUTPUT_DIR, "macro_panel.csv"))

    latest = panel.iloc[-1]
    snapshot = {
        "DATE": latest["DATE"],
        "MARKET_CONFIDENCE_SCORE": round(float(latest["MARKET_CONFIDENCE_SCORE"]), 1) if pd.notna(latest["MARKET_CONFIDENCE_SCORE"]) else np.nan,
        "REGIME_NAME": latest["REGIME_NAME"],
        "TRIPLE_BRAKE": int(latest["TRIPLE_BRAKE"]),
        "OVERHEAT": int(latest["OVERHEAT"]),
        "TARGET_LEVERAGE": latest["TARGET_LEVERAGE"],
        "VIX": latest.get("VIX", np.nan),
        "HY_OAS": latest.get("HY_OAS", np.nan),
    }
    print(f"   → REGIME: {snapshot['REGIME_NAME']} | SCORE: {snapshot['MARKET_CONFIDENCE_SCORE']}")
    return snapshot


# =========================================================
# 3. STAGE 2 — 펀더멘털(분기 매출/영업이익) 수집 (캐시 포함)
# =========================================================
def load_universe() -> pd.DataFrame:
    df = pd.read_csv(INPUT_CSV)
    df.columns = [c.strip().upper() for c in df.columns]
    df["TICKER"] = df["TICKER"].astype(str).str.upper().str.strip()
    df = df.dropna(subset=["TICKER"])
    df = df[df["TICKER"] != ""]
    return df


def fetch_income_quarterly(symbol: str, limit: int = LIMIT_Q) -> pd.DataFrame:
    url = "https://financialmodelingprep.com/stable/income-statement"
    params = {"symbol": symbol, "period": "quarter", "limit": limit, "apikey": FMP_API_KEY}
    try:
        rows = safe_get_json(url, params)
        if not isinstance(rows, list) or not rows:
            raise RuntimeError("empty")
    except Exception:
        v3_url = f"https://financialmodelingprep.com/api/v3/income-statement/{symbol}"
        v3_params = {"period": "quarter", "limit": limit, "apikey": FMP_API_KEY}
        rows = safe_get_json(v3_url, v3_params)

    if not isinstance(rows, list) or not rows:
        return pd.DataFrame()

    out = [{
        "TICKER": symbol,
        "DATE": r.get("date"),
        "FILING_DATE": r.get("filingDate") or r.get("acceptedDate"),
        "REVENUE_MUSD": float(r["revenue"]) / 1_000_000 if r.get("revenue") is not None else None,
        "OPINCOME_MUSD": float(r["operatingIncome"]) / 1_000_000 if r.get("operatingIncome") is not None else None,
    } for r in rows]

    df = pd.DataFrame(out)
    df["DATE"] = pd.to_datetime(df["DATE"], errors="coerce")
    df["FILING_DATE"] = pd.to_datetime(df["FILING_DATE"], errors="coerce")
    return df.sort_values("DATE").reset_index(drop=True)


def collect_fundamentals(tickers: list, limit: int = LIMIT_Q) -> pd.DataFrame:
    """FMP 분기 매출/영업이익 수집. 캐시가 FUNDAMENTALS_MAX_AGE_DAYS 이내면 재사용.
    limit을 기본값(LIMIT_Q)보다 크게 주면(예: 캘리브레이션용 3년치) 해당 티커는
    캐시 분기 수가 부족할 때만 다시 받아온다."""
    print(f"📊 [STAGE 2] 펀더멘털(분기 매출/영업이익) 수집 중... 대상 {len(tickers)}개")
    cache_path = os.path.join(CACHE_DIR, "fundamentals_quarterly.csv")
    cache_cols = ["TICKER", "DATE", "FILING_DATE", "REVENUE_MUSD", "OPINCOME_MUSD", "FETCHED_AT"]

    if os.path.exists(cache_path):
        cache_df = pd.read_csv(cache_path)
        for c in cache_cols:
            if c not in cache_df.columns:
                cache_df[c] = pd.NaT if c.endswith("DATE") or c == "FETCHED_AT" else np.nan
        for c in ["DATE", "FILING_DATE", "FETCHED_AT"]:
            cache_df[c] = pd.to_datetime(cache_df[c], errors="coerce")
    else:
        cache_df = pd.DataFrame(columns=cache_cols)

    stale_cutoff = pd.Timestamp.now() - pd.Timedelta(days=FUNDAMENTALS_MAX_AGE_DAYS)
    quarter_counts = cache_df.groupby("TICKER")["DATE"].nunique() if not cache_df.empty else pd.Series(dtype=int)
    fresh_tickers = set(
        cache_df.loc[cache_df["FETCHED_AT"] >= stale_cutoff, "TICKER"].unique()
    ) if not cache_df.empty else set()
    enough_quarters = set(quarter_counts[quarter_counts >= limit].index)

    need_fetch = [t for t in tickers if not (t in fresh_tickers and t in enough_quarters)]
    print(f"   캐시 재사용: {len(tickers) - len(need_fetch)}개 | 신규 수집: {len(need_fetch)}개")

    new_rows = []
    now = pd.Timestamp.now()
    for i, t in enumerate(need_fetch, 1):
        try:
            df = fetch_income_quarterly(t, limit=limit)
            if not df.empty:
                df["FETCHED_AT"] = now
                new_rows.append(df)
        except Exception as e:
            print(f"   ❌ {t}: {e}")
        if i % 10 == 0 or i == len(need_fetch):
            print(f"   ...{i}/{len(need_fetch)}")

    if new_rows:
        fresh_df = pd.concat(new_rows, ignore_index=True)
        cache_df = cache_df[~cache_df["TICKER"].isin(fresh_df["TICKER"].unique())]
        cache_df = pd.concat([cache_df, fresh_df], ignore_index=True)
        atomic_write_csv(cache_df, cache_path)

    result = cache_df[cache_df["TICKER"].isin(tickers)].copy()
    return result


# =========================================================
# 4. STAGE 3 — STEP2: 펀더멘털 스코어링 (2.STOCK.PY)
# =========================================================
def calc_revenue_acceleration(sub: pd.DataFrame) -> float:
    qoq = sub["REV_QOQ"].dropna()
    if len(qoq) >= 4:
        return qoq.iloc[-2:].mean() - qoq.iloc[-4:-2].mean()
    return np.nan


def check_turnaround(sub: pd.DataFrame) -> int:
    op = sub["OPINCOME_MUSD"].values
    if len(op) < 2:
        return 0
    return int(op[-1] > 0 and op[-2:].sum() > 0)


def load_calibrated_thresholds() -> dict:
    """Growth_Trigger_Calibration.py가 만든 실증 임계값이 있으면 그 값을,
    없으면 하드코딩된 기본값(YOY_GROWTH_MIN/ACCEL_MIN)을 반환한다."""
    path = os.path.join(OUTPUT_DIR, THRESHOLDS_JSON_PATH_NAME)
    result = {
        "YOY_GROWTH_MIN": YOY_GROWTH_MIN, "ACCEL_MIN": ACCEL_MIN,
        "ACCEL_IS_DISCRIMINATIVE": True, "source": "default",
    }
    if os.path.exists(path):
        try:
            import json
            with open(path, "r", encoding="utf-8") as f:
                calib = json.load(f)
            result["YOY_GROWTH_MIN"] = float(calib.get("YOY_GROWTH_MIN", YOY_GROWTH_MIN))
            result["ACCEL_MIN"] = float(calib.get("ACCEL_MIN", ACCEL_MIN))
            # 실증 검증 결과 REV_ACCEL이 SURGE 그룹을 구분하지 못하면(=예측력 없음)
            # STEP2_PASS의 필수 AND 조건에서 제외하고 점수 보너스로만 반영한다.
            result["ACCEL_IS_DISCRIMINATIVE"] = bool(calib.get("ACCEL_IS_DISCRIMINATIVE", True))
            result["source"] = f"calibrated ({calib.get('CALIBRATED_AT', '?')})"
        except Exception as e:
            print(f"⚠️ 임계값 캘리브레이션 파일 로드 실패, 기본값 사용: {e}")
    return result


def score_fundamentals(income_df: pd.DataFrame, theme_df: pd.DataFrame, always_include: set) -> pd.DataFrame:
    print("📊 [STAGE 3] STEP2 펀더멘털 스코어링...")
    thresholds = load_calibrated_thresholds()
    yoy_growth_min = thresholds["YOY_GROWTH_MIN"]
    accel_min = thresholds["ACCEL_MIN"]
    accel_is_gate = thresholds["ACCEL_IS_DISCRIMINATIVE"]
    print(f"   성장기준({thresholds['source']}): YOY>={yoy_growth_min:.1%}, ACCEL>={accel_min:.1%}"
          f" (가속도 필수조건: {'적용' if accel_is_gate else '미적용 - 실증상 구분력 없음, 점수만 반영'})")

    if income_df.empty:
        latest = pd.DataFrame(columns=["TICKER", "THEME", "STEP2_SCORE", "STEP2_PASS"])
    else:
        df = income_df.sort_values(["TICKER", "DATE"]).copy()
        df["REV_LAG1"] = df.groupby("TICKER")["REVENUE_MUSD"].shift(1)
        df["REV_LAG4"] = df.groupby("TICKER")["REVENUE_MUSD"].shift(4)
        df["OP_LAG4"] = df.groupby("TICKER")["OPINCOME_MUSD"].shift(4)
        df["REV_QOQ"] = df["REVENUE_MUSD"] / df["REV_LAG1"] - 1
        df["REV_YOY"] = df["REVENUE_MUSD"] / df["REV_LAG4"] - 1

        # 영업마진 확대폭(YoY): Growth_Trigger_Calibration.py 실증 결과 REV_ACCEL보다
        # 오히려 이 지표가 SURGE 그룹에서 더 뚜렷하게 높게 나와(중앙값 약 2배) 점수에 반영한다.
        df["OP_MARGIN"] = df["OPINCOME_MUSD"] / df["REVENUE_MUSD"].replace(0, np.nan)
        df["OP_MARGIN_LAG4"] = df.groupby("TICKER")["OP_MARGIN"].shift(4)
        df["OP_MARGIN_CHANGE"] = df["OP_MARGIN"] - df["OP_MARGIN_LAG4"]

        last6 = df.groupby("TICKER").tail(6).copy()
        latest = last6.groupby("TICKER").tail(1).copy()

        accel = (
            last6.groupby("TICKER").apply(calc_revenue_acceleration, include_groups=False)
            .reset_index(name="REV_ACCEL")
        )
        latest = latest.merge(accel, on="TICKER", how="left")
        latest["REV_ACCEL_CAPPED"] = latest["REV_ACCEL"].clip(lower=-REV_ACCEL_CAP, upper=REV_ACCEL_CAP)

        turn = (
            last6.groupby("TICKER").apply(check_turnaround, include_groups=False)
            .reset_index(name="TURNAROUND_FLAG")
        )
        latest = latest.merge(turn, on="TICKER", how="left")

        latest["A_YOY_GROWTH_FLAG"] = (latest["REV_YOY"] >= yoy_growth_min).astype(int)
        latest["A_ACCEL_FLAG"] = (latest["REV_ACCEL"] >= accel_min).astype(int)
        if accel_is_gate:
            latest["A_REVENUE_MOMENTUM_FLAG"] = (
                (latest["A_YOY_GROWTH_FLAG"] == 1) & (latest["A_ACCEL_FLAG"] == 1)
            ).astype(int)
        else:
            # 캘리브레이션 결과 REV_ACCEL이 급등 여부를 구분하지 못했으므로 필수조건에서 제외.
            latest["A_REVENUE_MOMENTUM_FLAG"] = latest["A_YOY_GROWTH_FLAG"]
        latest["B_TURNAROUND_FLAG"] = latest["TURNAROUND_FLAG"].fillna(0).astype(int)

        def calc_score(row):
            growth = row["REV_YOY"]
            if pd.isna(growth):
                growth = row["REV_QOQ"]
            growth_score = np.clip((growth if pd.notna(growth) and growth > 0 else 0) / 1.2, 0, 1)
            accel_val = row["REV_ACCEL_CAPPED"] if pd.notna(row["REV_ACCEL_CAPPED"]) else 0
            accel_score = np.clip(accel_val / 0.3, 0, 1)
            margin_change = row["OP_MARGIN_CHANGE"] if pd.notna(row["OP_MARGIN_CHANGE"]) else 0
            margin_bonus = np.clip(margin_change / 0.05, 0, 1) * 0.10
            bonus = (0.15 if row["B_TURNAROUND_FLAG"] == 1 else 0) + margin_bonus
            return np.clip(0.55 * growth_score + 0.35 * accel_score + bonus, 0, 1) * 100

        latest["STEP2_SCORE"] = latest.apply(calc_score, axis=1)
        latest["STEP2_PASS"] = (
            (latest["A_REVENUE_MOMENTUM_FLAG"] == 1)
            & ((latest["B_TURNAROUND_FLAG"] == 1) | (not USE_TURNAROUND))
        ).astype(int)

    latest = latest.merge(theme_df[["TICKER", "THEME"]].drop_duplicates("TICKER"), on="TICKER", how="right")

    # ALWAYS_INCLUDE 티커가 펀더멘털 데이터 없이도(예: FMP 미지원 KR종목) 다음 단계로 넘어가도록
    # STEP2_SCORE를 통과 기준선으로 채워준다.
    missing_mask = latest["TICKER"].isin(always_include) & latest["STEP2_SCORE"].isna()
    latest.loc[missing_mask, "STEP2_SCORE"] = STEP2_SCORE_MIN
    latest.loc[missing_mask, "STEP2_PASS"] = 0

    latest["STEP2_SCORE"] = latest["STEP2_SCORE"].fillna(0)
    latest["STEP2_PASS"] = latest["STEP2_PASS"].fillna(0).astype(int)
    return latest


# =========================================================
# 5. STAGE 4 — STEP3: 가격행동 + RSI + MDD (3.Recommended_STOCK.PY 확장)
# =========================================================
def build_price_features(tickers: list) -> tuple[pd.DataFrame, pd.DataFrame]:
    print("📊 [STAGE 4] 가격 데이터 + RSI/MDD/모멘텀 계산...")
    end = datetime.today()
    start = end - timedelta(days=LOOKBACK_DAYS)
    dl_list = sorted(set(tickers) | {BENCH_SPY, BENCH_QQQ})
    raw = download_price_history(dl_list, start, end)
    if raw.empty:
        raise RuntimeError("가격 데이터 다운로드 실패 (야후 파이낸스 응답 없음)")

    bench = raw[raw["TICKER"].isin([BENCH_SPY, BENCH_QQQ])].copy()
    px = raw[~raw["TICKER"].isin([BENCH_SPY, BENCH_QQQ])].copy()

    bench_ret = {}
    for b in [BENCH_SPY, BENCH_QQQ]:
        bsub = bench[bench["TICKER"] == b].sort_values("DATE")
        for d in (20, 60, 120):
            bench_ret[(b, d)] = bsub["CLOSE"].pct_change(d).iloc[-1] if len(bsub) > d else np.nan

    mkt = bench[bench["TICKER"] == MARKET_TICKER].sort_values("DATE")[["DATE", "CLOSE"]]
    mkt = mkt.rename(columns={"CLOSE": "MKT_CLOSE"})
    mkt["MKT_RET"] = mkt["MKT_CLOSE"].pct_change()

    feature_frames = []
    for ticker, g in px.groupby("TICKER", sort=False):
        g = g.sort_values("DATE").copy()
        g["PREV_CLOSE"] = g["CLOSE"].shift(1)
        tr = np.maximum(
            g["HIGH"] - g["LOW"],
            np.maximum((g["HIGH"] - g["PREV_CLOSE"]).abs(), (g["LOW"] - g["PREV_CLOSE"]).abs()),
        )
        g["ATR"] = tr.rolling(ATR_PERIOD, min_periods=ATR_PERIOD).mean()
        g["ATR_PCT"] = g["ATR"] / g["CLOSE"]
        g["ATR_CONTRACTION"] = g["ATR_PCT"] / g["ATR_PCT"].shift(5) - 1

        g["MA20"] = g["CLOSE"].rolling(20, min_periods=20).mean()
        g["MA50"] = g["CLOSE"].rolling(50, min_periods=50).mean()
        g["MA200"] = g["CLOSE"].rolling(MA_LONG_PERIOD, min_periods=MA_LONG_PERIOD).mean()
        g["TREND_ALIGN"] = ((g["CLOSE"] > g["MA20"]) & (g["MA20"] > g["MA50"])).astype(int)
        g["LONG_TERM_UPTREND"] = (g["CLOSE"] > g["MA200"]).astype(int)

        # 볼린저밴드(20일, ±2표준편차) — 하단 터치 = 단기 과매도 눌림목 후보
        bb_mid = g["CLOSE"].rolling(BB_PERIOD, min_periods=BB_PERIOD).mean()
        bb_std = g["CLOSE"].rolling(BB_PERIOD, min_periods=BB_PERIOD).std()
        g["BB_MID"] = bb_mid
        g["BB_UPPER"] = bb_mid + BB_STD_MULT * bb_std
        g["BB_LOWER"] = bb_mid - BB_STD_MULT * bb_std
        bb_width = (g["BB_UPPER"] - g["BB_LOWER"]).replace(0, np.nan)
        g["BB_PCT_B"] = (g["CLOSE"] - g["BB_LOWER"]) / bb_width

        g["DOLLAR_VOL"] = g["CLOSE"] * g["VOLUME"]
        g["DVOL_20"] = g["DOLLAR_VOL"].rolling(20, min_periods=20).mean()

        for d in (20, 60, 120):
            g[f"RET_{d}"] = g["CLOSE"].pct_change(d)
            g[f"RS_SPY_{d}"] = g[f"RET_{d}"] - bench_ret.get((BENCH_SPY, d), np.nan)
            g[f"RS_QQQ_{d}"] = g[f"RET_{d}"] - bench_ret.get((BENCH_QQQ, d), np.nan)

        g["HIGH_120"] = g["CLOSE"].rolling(120, min_periods=20).max()
        g["IS_NEW_HIGH"] = (g["CLOSE"] >= g["HIGH_120"]).astype(int)

        # RSI(14) + 반등 확인(2일 전보다 상승 중이면 "저점 찍고 올라오는 중")
        g["RSI_14"] = calc_rsi_series(g["CLOSE"])
        g["RSI_TURNING_UP"] = (g["RSI_14"] > g["RSI_14"].shift(2)).astype(int)

        # MDD: 최근 1년 고점 대비 현재 낙폭 + 1년 내 최악 낙폭
        roll_max = g["CLOSE"].rolling(MDD_WINDOW, min_periods=20).max()
        g["DRAWDOWN_CUR"] = g["CLOSE"] / roll_max - 1
        g["MDD_1Y"] = g["DRAWDOWN_CUR"].rolling(MDD_WINDOW, min_periods=20).min()

        gm = g.merge(mkt[["DATE", "MKT_RET"]], on="DATE", how="left")
        gm["DAILY_RET"] = gm["CLOSE"].pct_change()
        g["RS_MOMENTUM"] = (gm["DAILY_RET"] - gm["MKT_RET"]).rolling(5, min_periods=5).mean().values

        feature_frames.append(g)

    feat = pd.concat(feature_frames, ignore_index=True)
    latest = feat.groupby("TICKER", as_index=False).tail(1).copy()
    latest = latest.rename(columns={"DATE": "PRICE_DATE"})
    return feat, latest


def score_price_behavior(step2_df: pd.DataFrame, latest_px: pd.DataFrame, always_include: set) -> pd.DataFrame:
    candidates = step2_df[
        (step2_df["STEP2_SCORE"] >= STEP2_SCORE_MIN) | (step2_df["TICKER"].isin(always_include))
    ].copy()

    df = candidates.merge(latest_px, on="TICKER", how="left")
    df["LIQUIDITY_FLAG"] = (df["DVOL_20"] >= MIN_DOLLAR_VOL_20D).fillna(False).astype(int)

    mom = 0.35 * df["RET_20"].fillna(0) + 0.40 * df["RET_60"].fillna(0) + 0.25 * df["RET_120"].fillna(0)
    mom_score = np.clip(mom / 0.60, 0, 1)

    rs = 0.2 * df["RS_QQQ_20"].fillna(0) + 0.5 * df["RS_QQQ_60"].fillna(0) + 0.3 * df["RS_QQQ_120"].fillna(0)
    rs_score = np.clip(rs / 0.25, 0, 1)

    high_bonus = df["IS_NEW_HIGH"].fillna(0) * 0.08
    vol_penalty = (df["ATR_PCT"] > ATR_MAX_PCT).fillna(False).astype(int) * 0.18
    # RSI 존/눌림목 보너스는 STEP3_SCORE(종목 자체의 모멘텀 품질)에서 빼고
    # build_final_dashboard의 ENTRY_SIGNAL(눌림목 매입 타이밍)에서 별도로 판정한다.
    # 여기서는 "너무 과열/너무 망가진 종목"만 감점한다.
    overbought_penalty = (df["RSI_14"] > RSI_OVERBOUGHT).fillna(False).astype(int) * 0.15
    deep_risk_penalty = (df["MDD_1Y"] <= DEEP_RISK_MDD).fillna(False).astype(int) * 0.10

    base = (
        0.55 * mom_score + 0.45 * rs_score + high_bonus - vol_penalty
        - overbought_penalty - deep_risk_penalty
    )
    df["STEP3_SCORE"] = np.clip(base, 0, 1) * 100
    df["STEP3_PASS"] = ((df["LIQUIDITY_FLAG"] == 1) & (df["STEP3_SCORE"] >= STEP3_SCORE_MIN)).astype(int)

    missing_mask = df["TICKER"].isin(always_include) & df["STEP3_SCORE"].isna()
    df.loc[missing_mask, "STEP3_SCORE"] = STEP3_SCORE_MIN
    df["STEP3_SCORE"] = df["STEP3_SCORE"].fillna(0)

    # 오늘 후보군(워치리스트) 내에서의 상대강도 순위(IBD RS Rating과 유사한 개념).
    # 주 1회만 FMP 전체 스캔을 하는 구조상 "전체 500종목 대비 순위"를 매일 계산하려면
    # 매일 전체 유니버스 가격을 다시 받아야 해서 애초의 속도 최적화 취지와 어긋난다.
    # 대신 "오늘 후보군(이미 성장기준을 통과한 종목들) 안에서의 상대순위"로 계산한다.
    blended_ret = 0.5 * df["RET_60"].fillna(-1) + 0.5 * df["RET_120"].fillna(-1)
    df["RS_RANK"] = (blended_ret.rank(pct=True) * 99).round().clip(1, 99)

    return df


# =========================================================
# 6. STAGE 5 — STEP4: 최종 대시보드 + 매크로 게이트 (4.STOCK_BI.PY)
# =========================================================
def fetch_analyst_info(ticker: str) -> dict:
    out = {
        "TICKER": ticker, "NAME": ticker, "TARGET_PRICE": np.nan, "REC_KEY": "N/A",
        "ANALYST_COUNT": 0, "EARNINGS_DATE": pd.NaT,
        "SHORT_FLOAT_PCT": np.nan, "AVG_VOLUME": np.nan,
    }
    try:
        t = yf.Ticker(ticker)
        info = t.info or {}
        out["NAME"] = info.get("shortName") or info.get("longName") or ticker
        out["TARGET_PRICE"] = info.get("targetMeanPrice", np.nan)
        out["REC_KEY"] = info.get("recommendationKey", "N/A")
        out["ANALYST_COUNT"] = info.get("numberOfAnalystOpinions", 0)
        out["SHORT_FLOAT_PCT"] = info.get("shortPercentOfFloat", np.nan)
        out["AVG_VOLUME"] = info.get("averageVolume", np.nan)
        try:
            ed = t.earnings_dates
            if ed is not None and not ed.empty:
                future = ed[ed.index > pd.Timestamp.now(tz="UTC")]
                if not future.empty:
                    out["EARNINGS_DATE"] = future.index.sort_values()[0].tz_localize(None)
        except Exception:
            pass
    except Exception as e:
        print(f"   ⚠️ {ticker} 애널리스트 정보 실패: {e}")
    return out


def build_final_dashboard(step3_df: pd.DataFrame, macro: dict) -> pd.DataFrame:
    print("📊 [STAGE 5] 최종 대시보드 + 매크로 게이트 적용...")
    trade = step3_df.copy()
    trade.loc[trade["TICKER"].isin(FORCE_TICKERS), "THEME"] = FORCE_THEME

    analyst_rows = [fetch_analyst_info(t) for t in trade["TICKER"].unique()]
    trade = trade.merge(pd.DataFrame(analyst_rows), on="TICKER", how="left")

    trade["STOP_PRICE"] = trade["CLOSE"] - trade["ATR"] * STOP_ATR_MULTIPLIER
    trade["TP1_PRICE"] = trade["CLOSE"] + trade["ATR"] * 2.0
    trade["RR_TP1"] = (
        (trade["TP1_PRICE"] - trade["CLOSE"]) / (trade["CLOSE"] - trade["STOP_PRICE"])
    ).replace([np.inf, -np.inf], np.nan).round(2)
    # RR_TP1은 TP/STOP이 둘 다 ATR의 고정 배수라 항상 동일한 값(2.0/1.5=1.33)이 나와
    # 종목별로 구분이 안 된다 (참고용 지표로만 사용). 실제 진입 게이트는 애널리스트
    # 목표가 기반 RR_TARGET(종목마다 실제로 다름)을 사용한다.
    trade["RR_TARGET"] = (
        (trade["TARGET_PRICE"] - trade["CLOSE"]) / (trade["CLOSE"] - trade["STOP_PRICE"])
    ).replace([np.inf, -np.inf], np.nan).round(2)
    trade["UPSIDE"] = (trade["TARGET_PRICE"] / trade["CLOSE"] - 1).replace([np.inf, -np.inf], np.nan).fillna(0)

    trade["SECTOR_SCORE"] = (
        0.50 * trade["RS_MOMENTUM"].fillna(0)
        + 0.30 * (trade["STEP3_SCORE"].fillna(0) / 100.0)
        + 0.20 * trade["UPSIDE"].fillna(0)
    )

    today = pd.Timestamp(datetime.today().date())
    trade["EARNINGS_DATE"] = pd.to_datetime(trade["EARNINGS_DATE"], errors="coerce")
    trade["EARNINGS_DAYS_LEFT"] = (trade["EARNINGS_DATE"] - today).dt.days

    # ---- 매크로 게이트 ----
    macro_block = int(macro.get("TRIPLE_BRAKE", 0)) == 1
    macro_caution = int(macro.get("OVERHEAT", 0)) == 1
    trade["MACRO_REGIME"] = macro.get("REGIME_NAME", "N/A")
    trade["MACRO_BLOCK"] = int(macro_block)
    trade["MACRO_CAUTION"] = int(macro_caution)

    # ---- 메인 매입 타이밍 신호: RSI 30 부근(+ 반등 확인) + 볼린저밴드 하단 터치 + 건강한 눌림목 ----
    # 장기추세(200일선 위)가 살아있는 종목으로 한정해 "우상향 종목의 조정"만 잡고,
    # 하락추세에서 반복되는 falling-knife성 RSI 30 터치는 걸러낸다.
    rsi_dip_zone = trade["RSI_14"].between(RSI_DIP_LOW, RSI_DIP_HIGH)
    bb_lower_touch = trade["BB_PCT_B"] <= BB_LOWER_TOUCH_PCTB
    healthy_pullback = trade["DRAWDOWN_CUR"].between(PULLBACK_MIN, PULLBACK_MAX)
    not_deep_risk = trade["MDD_1Y"] > DEEP_RISK_MDD

    trade["ENTRY_SIGNAL"] = (
        (trade["LONG_TERM_UPTREND"] == 1)
        & rsi_dip_zone.fillna(False)
        & (trade["RSI_TURNING_UP"] == 1)
        & bb_lower_touch.fillna(False)
        & healthy_pullback.fillna(False)
        & not_deep_risk.fillna(False)
        & (trade["LIQUIDITY_FLAG"] == 1)
        & (not macro_block)
    ).astype(int)

    # ---- 보조 신호: 추세추종형 돌파(모멘텀 확장 + 변동성 수축 + 애널리스트 목표가 기준 손익비) ----
    # ENTRY_SIGNAL(눌림목 매수)과는 다른 전략이라 별도 컬럼으로 남겨둔다.
    # 두 신호가 동시에 뜨면 가장 강한 후보.
    trade["BREAKOUT_SIGNAL"] = (
        (trade["TREND_ALIGN"] == 1)
        & (trade["RS_MOMENTUM"] > 0)
        & (trade["ATR_CONTRACTION"] < 0)
        & (trade["RR_TARGET"] >= 2)
        & (trade["RSI_14"] < RSI_OVERBOUGHT)
        & (not macro_block)
    ).astype(int)

    trade["SIGNAL_COMBO"] = trade["ENTRY_SIGNAL"] + trade["BREAKOUT_SIGNAL"]
    trade = trade.sort_values(["SIGNAL_COMBO", "ENTRY_SIGNAL", "SECTOR_SCORE", "STEP3_SCORE"], ascending=False).reset_index(drop=True)
    trade["RANK"] = trade.index + 1
    return trade


# =========================================================
# 7. HTML 리포트 (표 + 리스크/리워드 산점도 + 매크로 미니차트)
# =========================================================
import io
import base64
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm

# 한글 폰트: Windows에는 Malgun Gothic이 기본 내장되어 있지만, GitHub Actions 같은
# 리눅스 클라우드 러너에는 한글 폰트가 없어 차트 텍스트가 네모박스로 깨진다.
# assets/fonts/NanumBarunGothic.ttf(SIL OFL 오픈라이선스, 재배포 가능)를 저장소에
# 함께 넣어두고 OS와 무관하게 이 폰트를 직접 등록해서 쓴다.
_BUNDLED_FONT_PATH = os.path.join(BASE_DIR, "assets", "fonts", "NanumBarunGothic.ttf")
if os.path.exists(_BUNDLED_FONT_PATH):
    fm.fontManager.addfont(_BUNDLED_FONT_PATH)
    plt.rcParams["font.family"] = fm.FontProperties(fname=_BUNDLED_FONT_PATH).get_name()
else:
    plt.rcParams["font.family"] = "Malgun Gothic"  # 폰트 파일이 없으면 Windows 기본 폰트로 대체
plt.rcParams["axes.unicode_minus"] = False       # 마이너스 기호가 네모박스로 깨지는 것 방지

COL_LABELS = {
    "RANK": "순위", "TICKER": "티커", "NAME": "회사명", "THEME": "테마",
    "CLOSE": "현재가", "RSI_14": "RSI", "BB_PCT_B": "밴드%B", "DRAWDOWN_CUR": "현재낙폭",
    "MDD_1Y": "1Y최대낙폭", "STEP2_SCORE": "실적점수", "STEP3_SCORE": "가격점수",
    "RS_RANK": "상대강도", "SIGNAL": "신호", "RR_TARGET": "손익비", "STOP_PRICE": "손절가",
    "TP1_PRICE": "1차목표가", "TARGET_PRICE": "애널목표가", "REC_KEY": "컨센서스",
    "EARNINGS": "실적발표", "SHORT_FLOAT_PCT": "공매도비중",
}


def _fmt_price(x):
    if pd.isna(x):
        return "-"
    return f"{x:,.0f}" if abs(x) >= 1000 else f"{x:,.2f}"


def _fmt_pct(x, digits=1):
    if pd.isna(x):
        return "-"
    return f"{x * 100:.{digits}f}%"


def _fmt_score(x):
    return "-" if pd.isna(x) else f"{x:.0f}"


def _fmt_ratio(x, digits=2):
    return "-" if pd.isna(x) else f"{x:.{digits}f}"


def _signal_badge(r):
    entry, breakout = r.get("ENTRY_SIGNAL") == 1, r.get("BREAKOUT_SIGNAL") == 1
    if entry and breakout:
        return "🔥동시"
    if entry:
        return "🎯눌림목"
    if breakout:
        return "🚀돌파"
    return ""


def _earnings_badge(days_left):
    if pd.isna(days_left):
        return "⚪ 미정"
    d = int(days_left)
    icon = "🔴" if d <= 7 else ("🟠" if d <= 14 else "🟢")
    return f"{icon} D{'+' if d >= 0 else ''}{d}"


def _fig_to_base64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=115, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def render_risk_reward_chart(trade: pd.DataFrame) -> str:
    d = trade.dropna(subset=["ATR_PCT", "UPSIDE"]).copy()
    if d.empty:
        return ""
    fig, ax = plt.subplots(figsize=(6.2, 4.6))
    colors = np.where(
        (d["ENTRY_SIGNAL"] == 1) | (d["BREAKOUT_SIGNAL"] == 1), "#27ae60", "#3498db"
    )
    ax.scatter(d["ATR_PCT"] * 100, d["UPSIDE"] * 100, c=colors, s=70, alpha=0.85,
               edgecolors="white", linewidths=0.6, zorder=3)
    for _, r in d.iterrows():
        ax.annotate(r["TICKER"], (r["ATR_PCT"] * 100, r["UPSIDE"] * 100),
                    fontsize=8, xytext=(5, 4), textcoords="offset points")
    ax.axhline(0, color="#bbb", linewidth=0.8, linestyle="--", zorder=1)
    ax.set_xlabel("변동성 ATR (%)")
    ax.set_ylabel("애널리스트 상승여력 (%)")
    ax.set_title("리스크 vs 리워드 (초록 = 오늘 신호 발생)")
    ax.grid(alpha=0.25)
    fig.tight_layout()
    return _fig_to_base64(fig)


def render_macro_chart() -> str:
    path = os.path.join(OUTPUT_DIR, "macro_panel.csv")
    if not os.path.exists(path):
        return ""
    panel = pd.read_csv(path, parse_dates=["DATE"]).tail(260)
    if panel.empty:
        return ""
    fig, ax1 = plt.subplots(figsize=(9.5, 3.2))
    ax1.plot(panel["DATE"], panel["SPY"], color="#2980b9", linewidth=1.4, label="SPY")
    ax1.set_ylabel("SPY", color="#2980b9")
    ax1.tick_params(axis="y", labelcolor="#2980b9")
    ax2 = ax1.twinx()
    ax2.plot(panel["DATE"], panel["VIX"], color="#e74c3c", linewidth=1.0, label="VIX", alpha=0.8)
    ax2.set_ylabel("VIX", color="#e74c3c")
    ax2.tick_params(axis="y", labelcolor="#e74c3c")
    ax1.set_title("SPY vs VIX (최근 1년)")
    ax1.grid(alpha=0.2)
    fig.tight_layout()
    return _fig_to_base64(fig)


def render_html_report(trade: pd.DataFrame, macro: dict, out_path: str):
    regime_color = "#c0392b" if macro.get("TRIPLE_BRAKE") else ("#e67e22" if macro.get("OVERHEAT") else "#27ae60")

    table_cols = [
        "RANK", "TICKER", "NAME", "THEME", "CLOSE", "RSI_14", "BB_PCT_B",
        "DRAWDOWN_CUR", "MDD_1Y", "STEP2_SCORE", "STEP3_SCORE", "RS_RANK", "SIGNAL",
        "RR_TARGET", "STOP_PRICE", "TP1_PRICE", "TARGET_PRICE", "REC_KEY",
        "EARNINGS", "SHORT_FLOAT_PCT",
    ]

    rows_html = []
    for _, r in trade.iterrows():
        cell_values = {
            "RANK": int(r.get("RANK", 0)),
            "TICKER": r.get("TICKER", ""),
            "NAME": r.get("NAME", r.get("TICKER", "")),
            "THEME": r.get("THEME", ""),
            "CLOSE": _fmt_price(r.get("CLOSE")),
            "RSI_14": _fmt_score(r.get("RSI_14")),
            "BB_PCT_B": _fmt_ratio(r.get("BB_PCT_B")),
            "DRAWDOWN_CUR": _fmt_pct(r.get("DRAWDOWN_CUR")),
            "MDD_1Y": _fmt_pct(r.get("MDD_1Y")),
            "STEP2_SCORE": _fmt_score(r.get("STEP2_SCORE")),
            "STEP3_SCORE": _fmt_score(r.get("STEP3_SCORE")),
            "RS_RANK": _fmt_score(r.get("RS_RANK")),
            "SIGNAL": _signal_badge(r),
            "RR_TARGET": _fmt_ratio(r.get("RR_TARGET")),
            "STOP_PRICE": _fmt_price(r.get("STOP_PRICE")),
            "TP1_PRICE": _fmt_price(r.get("TP1_PRICE")),
            "TARGET_PRICE": _fmt_price(r.get("TARGET_PRICE")),
            "REC_KEY": str(r.get("REC_KEY", "")).upper(),
            "EARNINGS": _earnings_badge(r.get("EARNINGS_DAYS_LEFT")),
            "SHORT_FLOAT_PCT": _fmt_pct(r.get("SHORT_FLOAT_PCT")),
        }
        bg = ""
        if r.get("ENTRY_SIGNAL") == 1 and r.get("BREAKOUT_SIGNAL") == 1:
            bg = "background:#c8f7c5;"
        elif r.get("ENTRY_SIGNAL") == 1:
            bg = "background:#eafaf1;"
        elif r.get("BREAKOUT_SIGNAL") == 1:
            bg = "background:#eaf4fb;"
        cells = "".join(f"<td>{cell_values[c]}</td>" for c in table_cols)
        rows_html.append(f'<tr style="{bg}">{cells}</tr>')

    header_html = "".join(f"<th>{COL_LABELS.get(c, c)}</th>" for c in table_cols)

    risk_reward_b64 = render_risk_reward_chart(trade)
    macro_chart_b64 = render_macro_chart()
    risk_reward_html = (
        f'<img src="data:image/png;base64,{risk_reward_b64}" style="max-width:100%;">'
        if risk_reward_b64 else "<p>데이터 부족으로 차트 생략</p>"
    )
    macro_chart_html = (
        f'<img src="data:image/png;base64,{macro_chart_b64}" style="max-width:100%;">'
        if macro_chart_b64 else "<p>매크로 히스토리 없음</p>"
    )

    html = f"""<meta charset="utf-8">
<title>Stock Entry Agent - {macro.get('DATE', '')}</title>
<style>
  body {{ font-family: "Malgun Gothic", -apple-system, Segoe UI, sans-serif; margin: 24px; color: #222; }}
  h1 {{ font-size: 20px; }}
  h2 {{ font-size: 15px; margin-top: 28px; }}
  .macro-banner {{ padding: 12px 16px; border-radius: 8px; color: white; background: {regime_color}; margin-bottom: 16px; }}
  .charts {{ display: flex; gap: 16px; flex-wrap: wrap; align-items: flex-start; }}
  .charts > div {{ flex: 1 1 380px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 12.5px; margin-top: 8px; }}
  th, td {{ border: 1px solid #ddd; padding: 5px 7px; text-align: right; white-space: nowrap; }}
  th {{ background: #f4f4f4; position: sticky; top: 0; }}
  td:nth-child(2), td:nth-child(3), td:nth-child(4) {{ text-align: left; }}
  .table-wrap {{ overflow-x: auto; }}
</style>
<h1>📈 Stock Entry Agent — {macro.get('DATE', '')}</h1>
<div class="macro-banner">
  <b>매크로 레짐:</b> {macro.get('REGIME_NAME', 'N/A')} &nbsp; | &nbsp;
  <b>컨피던스 스코어:</b> {macro.get('MARKET_CONFIDENCE_SCORE', 'N/A')} &nbsp; | &nbsp;
  <b>목표 레버리지:</b> {macro.get('TARGET_LEVERAGE', 'N/A')} &nbsp; | &nbsp;
  <b>VIX:</b> {macro.get('VIX', 'N/A')}
</div>
<h2>매크로 추이</h2>
<div class="charts"><div>{macro_chart_html}</div></div>
<h2>오늘의 후보 — 리스크 vs 리워드</h2>
<div class="charts"><div>{risk_reward_html}</div></div>
<h2>후보 리스트 (🔥동시신호 &gt; 🎯눌림목(ENTRY) &gt; 🚀돌파(BREAKOUT) 순 정렬)</h2>
<div class="table-wrap">
<table>
<thead><tr>{header_html}</tr></thead>
<tbody>{''.join(rows_html)}</tbody>
</table>
</div>
"""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)


# =========================================================
# 8. Slack 알림 (TOP N 요약)
# =========================================================
def upside_gauge(upside, max_pct: float = 100.0) -> str:
    if pd.isna(upside):
        return "N/A"
    pct = max(upside * 100, 0)
    ratio = min(pct / max_pct, 1.0)
    filled = int(round(ratio * 10))
    return f"+{pct:.1f}% " + "🟩" * filled + "⬜" * (10 - filled)


def send_slack_message(payload: dict):
    if not SLACK_WEBHOOK_URL:
        print("⚠ SLACK_WEBHOOK_URL 미설정 → Slack 전송 스킵 (환경변수로 설정 필요)")
        return
    try:
        r = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
        if r.status_code != 200:
            print(f"⚠ Slack 전송 실패: {r.status_code} {r.text[:200]}")
        else:
            print("✅ Slack 전송 완료")
    except Exception as e:
        print(f"⚠ Slack 전송 오류: {e}")


def build_slack_payload(trade: pd.DataFrame, macro: dict, top_n: int = SLACK_TOP_N) -> dict:
    regime_icon = "🛑" if macro.get("TRIPLE_BRAKE") else ("⚠️" if macro.get("OVERHEAT") else "🟢")
    blocks = [
        {"type": "header", "text": {"type": "plain_text",
         "text": f"📈 오늘의 진입 후보 TOP{top_n} — {macro.get('DATE', '')}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": (
            f"{regime_icon} *매크로 레짐:* {macro.get('REGIME_NAME', 'N/A')}  |  "
            f"*컨피던스:* {macro.get('MARKET_CONFIDENCE_SCORE', 'N/A')}  |  "
            f"*VIX:* {macro.get('VIX', 'N/A')}"
        )}},
        {"type": "divider"},
    ]

    top = trade.head(top_n)
    if top.empty:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "오늘은 후보 종목이 없습니다."}})
        return {"blocks": blocks}

    for _, row in top.iterrows():
        entry, breakout = row.get("ENTRY_SIGNAL") == 1, row.get("BREAKOUT_SIGNAL") == 1
        if entry and breakout:
            icon = "🔥 *동시신호(눌림목+돌파)*"
        elif entry:
            icon = "🎯 *눌림목(ENTRY)*"
        elif breakout:
            icon = "🚀 *돌파(BREAKOUT)*"
        else:
            icon = "⚪ *관망*"

        gauge = upside_gauge(row.get("UPSIDE", 0))
        earn = _earnings_badge(row.get("EARNINGS_DAYS_LEFT"))
        name = row.get("NAME") or row.get("TICKER", "")
        analyst_n = row.get("ANALYST_COUNT", 0)
        analyst_n = int(analyst_n) if pd.notna(analyst_n) else 0

        text = (
            f"*{int(row.get('RANK', 0))}위. {row.get('TICKER', '')}* ({name})  _[{row.get('THEME', '')}]_  {icon}\n"
            f"• *상승여력:* `{gauge}`\n"
            f"• *실적 일정:* {earn}\n"
            f"• *현재가:* `{_fmt_price(row.get('CLOSE'))}` | *애널목표가:* `{_fmt_price(row.get('TARGET_PRICE'))}`\n"
            f"• *RSI:* `{_fmt_score(row.get('RSI_14'))}` | *밴드%B:* `{_fmt_ratio(row.get('BB_PCT_B'))}` "
            f"| *1Y고점대비:* `{_fmt_pct(row.get('DRAWDOWN_CUR'))}`\n"
            f"• *손익비(RR_TARGET):* `{_fmt_ratio(row.get('RR_TARGET'))}`\n"
            f"• *컨센서스:* `{str(row.get('REC_KEY', '')).upper()}` ({analyst_n}곳)\n"
            f"• *실적점수:* `{_fmt_score(row.get('STEP2_SCORE'))}` | *가격점수:* `{_fmt_score(row.get('STEP3_SCORE'))}` "
            f"| *후보군내 상대강도:* `{_fmt_score(row.get('RS_RANK'))}`"
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
        blocks.append({"type": "divider"})

    return {"blocks": blocks}


# =========================================================
# 9. 워치리스트(주간 FMP 스캔 ↔ 일간 가격체크 분리)
# =========================================================
def watchlist_path() -> str:
    return os.path.join(OUTPUT_DIR, WATCHLIST_PATH_NAME)


def need_weekly_refresh() -> bool:
    path = watchlist_path()
    if not os.path.exists(path):
        return True
    if datetime.today().weekday() == WEEKLY_REFRESH_WEEKDAY:
        return True
    age_days = (datetime.now() - datetime.fromtimestamp(os.path.getmtime(path))).days
    return age_days >= WATCHLIST_MAX_AGE_DAYS


def run_weekly_fundamental_scan(theme_df: pd.DataFrame, always_include: set) -> pd.DataFrame:
    """FMP로 전체 유니버스를 스캔해 STEP2를 통과한 종목만 watchlist.csv로 저장한다.
    이 단계가 느린 부분(489종목 x FMP 호출)이라 주 1회(월요일)만 수행한다."""
    tickers = sorted(set(theme_df["TICKER"].unique()) | set(FORCE_TICKERS))
    income_df = collect_fundamentals(tickers)
    step2_df = score_fundamentals(income_df, theme_df, always_include)

    candidates = step2_df[
        (step2_df["STEP2_SCORE"] >= STEP2_SCORE_MIN) | (step2_df["TICKER"].isin(always_include))
    ].copy()
    candidates["WATCHLIST_UPDATED_AT"] = datetime.today().strftime("%Y-%m-%d")
    atomic_write_csv(candidates, watchlist_path())
    print(f"📋 워치리스트 갱신 완료: {len(candidates)}개 종목 (STEP2 통과 또는 강제포함) → {watchlist_path()}")
    return candidates


def load_watchlist() -> pd.DataFrame:
    df = pd.read_csv(watchlist_path())
    df["TICKER"] = df["TICKER"].astype(str).str.upper().str.strip()
    updated_at = df["WATCHLIST_UPDATED_AT"].iloc[0] if "WATCHLIST_UPDATED_AT" in df.columns and len(df) else "?"
    print(f"📋 워치리스트 캐시 재사용: {len(df)}개 종목 (마지막 갱신: {updated_at}, FMP 재호출 없음)")
    return df


def get_step2_watchlist(theme_df: pd.DataFrame, always_include: set) -> pd.DataFrame:
    if TEST_MODE:
        # 테스트 모드는 검증 목적이라 항상 즉석 스캔(워치리스트 캐시 사용 안 함).
        # score_fundamentals는 theme_df 전체와 right-join하므로(누락 티커 방어 목적)
        # 여기서 명시적으로 테스트 대상만 다시 걸러내야 전체 유니버스가 섞이지 않는다.
        tickers = sorted(set(TEST_TICKERS) | set(FORCE_TICKERS))
        income_df = collect_fundamentals(tickers)
        step2_df = score_fundamentals(income_df, theme_df, always_include)
        return step2_df[step2_df["TICKER"].isin(tickers)].copy()

    if need_weekly_refresh():
        print(f"🗓️ 주간 갱신일(매주 월요일) 또는 워치리스트 없음/오래됨 → 전체 유니버스 FMP 재스캔")
        return run_weekly_fundamental_scan(theme_df, always_include)

    return load_watchlist()


# =========================================================
# 10. MAIN
# =========================================================
def main():
    theme_df = load_universe()
    always_include = set(FORCE_TICKERS) | (set(TEST_TICKERS) if TEST_MODE else set())

    print(f"🚀 Stock Entry Agent 시작 (TEST_MODE={TEST_MODE})")

    macro = run_macro_engine()

    step2_df = get_step2_watchlist(theme_df, always_include)
    watchlist_tickers = step2_df["TICKER"].unique().tolist()
    print(f"📈 오늘 가격/RSI/MDD 체크 대상: {len(watchlist_tickers)}개 (워치리스트만 조회, 전체 유니버스 재다운로드 없음)")

    _, latest_px = build_price_features(watchlist_tickers)
    step3_df = score_price_behavior(step2_df, latest_px, always_include)

    dashboard = build_final_dashboard(step3_df, macro)

    today_str = datetime.today().strftime("%Y-%m-%d")
    csv_path = os.path.join(OUTPUT_DIR, f"entry_signal_{today_str}.csv")
    html_path = os.path.join(OUTPUT_DIR, f"entry_signal_{today_str}.html")
    atomic_write_csv(dashboard, csv_path)
    render_html_report(dashboard, macro, html_path)

    # latest 포인터(항상 최신 파일을 가리키는 고정 이름)
    atomic_write_csv(dashboard, os.path.join(OUTPUT_DIR, "entry_signal_latest.csv"))
    render_html_report(dashboard, macro, os.path.join(OUTPUT_DIR, "entry_signal_latest.html"))

    # 누적 히스토리
    hist_path = os.path.join(OUTPUT_DIR, "daily_snapshot_history.csv")
    snap_cols = [
        "TICKER", "THEME", "CLOSE", "STEP2_SCORE", "STEP3_SCORE",
        "ENTRY_SIGNAL", "BREAKOUT_SIGNAL", "RSI_14", "BB_PCT_B", "DRAWDOWN_CUR",
    ]
    snap_cols = [c for c in snap_cols if c in dashboard.columns]
    snapshot = dashboard[snap_cols].copy()
    snapshot["DATE"] = today_str
    if os.path.exists(hist_path):
        history = pd.read_csv(hist_path)
        history = pd.concat([history, snapshot], ignore_index=True)
        history = history.drop_duplicates(subset=["DATE", "TICKER"], keep="last")
    else:
        history = snapshot
    atomic_write_csv(history, hist_path)

    print(f"\n✅ 완료: {csv_path}")
    print(f"✅ 완료: {html_path}")
    n_entry = int(dashboard["ENTRY_SIGNAL"].sum())
    n_breakout = int(dashboard["BREAKOUT_SIGNAL"].sum())
    n_combo = int(((dashboard["ENTRY_SIGNAL"] == 1) & (dashboard["BREAKOUT_SIGNAL"] == 1)).sum())
    print(f"📌 ENTRY_SIGNAL(눌림목): {n_entry}개 | BREAKOUT_SIGNAL(돌파): {n_breakout}개 | 동시신호: {n_combo}개 / {len(dashboard)}개 후보")

    if ENABLE_SLACK:
        send_slack_message(build_slack_payload(dashboard, macro))
    else:
        print("ℹ️ Slack 알림 비활성 상태(ENABLE_SLACK=false). 켜려면 환경변수 ENABLE_SLACK=true, SLACK_WEBHOOK_URL 설정 필요")


if __name__ == "__main__":
    main()
