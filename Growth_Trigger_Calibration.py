# -*- coding: utf-8 -*-
"""
Growth_Trigger_Calibration.py
================================
"주가가 전분기 대비 크게 오른 시점에 매출/영업이익 증가율이 어땠는가"를
과거 3년 데이터로 역산해서, STEP2 성장기준(YOY_GROWTH_MIN/ACCEL_MIN)을
감(고정값)이 아니라 실제 데이터로 캘리브레이션하는 스크립트.

방법론
------
1) 유니버스 전체 티커의 분기 매출/영업이익을 FMP에서 최근 3년(~16분기) 수집
   (Stock_Entry_Agent.py의 캐시/수집 로직을 그대로 재사용)
2) 각 분기 실적의 "공시일(filingDate)"부터 "다음 분기 공시일"까지의 주가 수익률을
   계산한다. 분기 마감일이 아니라 공시일 기준으로 맞추는 이유: 마감일 기준으로
   맞추면 시장이 아직 모르는 정보를 미리 반영한 것처럼 계산되는 look-ahead bias가
   생긴다.
3) 같은 달력분기(예: 2025Q2) 안에서 전체 종목의 수익률을 상대적으로 순위 매겨
   상위 15%를 "주가 급등 분기(SURGE)"로 표시한다. 절대 수익률 기준(예: +20%)을
   쓰지 않는 이유: 업종별 변동성이 다르고(반도체 vs 유틸리티) 매크로 사이클에
   따라 전체 시장이 오르내리는 시기가 다르기 때문에, 상대 순위가 더 안정적이다.
4) SURGE 그룹과 비SURGE 그룹의 REV_YOY / OP_YOY / REV_ACCEL / 영업마진 변화 /
   턴어라운드 비율을 비교해서 실제로 급등을 갈랐던 기준을 찾는다.
5) SURGE 그룹의 25th percentile 값을 새 임계값으로 제안해
   output/growth_trigger_thresholds.json에 저장한다.
   Stock_Entry_Agent.py는 다음 실행부터 이 파일을 자동으로 읽어 사용한다.

한계
----
- 생존편향: 현재 살아있는 종목만 대상이라 상장폐지된 실패 케이스는 빠진다.
- 상관관계이지 인과관계 증명이 아니다. 주가 급등은 실적 외에 가이던스, 자사주
  매입, M&A, 숏스퀴즈 등 다른 요인으로도 발생할 수 있다.

실행: python Growth_Trigger_Calibration.py
"""

import os
import json
import numpy as np
import pandas as pd
from datetime import datetime

import Stock_Entry_Agent as agent  # 유니버스 로드 / FMP 수집 / 가격 다운로드 재사용

OUTPUT_DIR = agent.OUTPUT_DIR

CALIB_LIMIT_Q = 16              # 3년 + 계산 여유(YoY/가속도 lookback 포함)
SURGE_TOP_PCT = 0.85             # 같은 달력분기 내 상위 15%를 SURGE로 표시
DEFAULT_FILING_LAG_DAYS = 35     # filingDate가 없을 때 대체값(분기 마감 + 통상 공시 리드타임)
MIN_BUCKET_SIZE = 20             # 이보다 표본이 적은 분기 버킷은 순위 계산에서 제외(노이즈 방지)


def build_ticker_quarter_table(tickers: list) -> pd.DataFrame:
    print(f"📥 {len(tickers)}개 종목 최근 {CALIB_LIMIT_Q}분기 재무데이터 수집...")
    income_df = agent.collect_fundamentals(tickers, limit=CALIB_LIMIT_Q)
    if income_df.empty:
        raise RuntimeError("재무데이터 수집 결과가 비어 있음")

    df = income_df.sort_values(["TICKER", "DATE"]).copy()
    fallback_date = df["DATE"] + pd.Timedelta(days=DEFAULT_FILING_LAG_DAYS)
    df["EFFECTIVE_DATE"] = df["FILING_DATE"].fillna(fallback_date)

    df["REV_LAG1"] = df.groupby("TICKER")["REVENUE_MUSD"].shift(1)
    df["REV_LAG4"] = df.groupby("TICKER")["REVENUE_MUSD"].shift(4)
    df["OP_LAG4"] = df.groupby("TICKER")["OPINCOME_MUSD"].shift(4)

    # 분모가 0/음수에 가까우면 비율이 폭발(inf)하거나 부호가 뒤집혀 의미가 없어짐 → NaN 처리
    rev_lag1_safe = df["REV_LAG1"].where(df["REV_LAG1"] > 0)
    rev_lag4_safe = df["REV_LAG4"].where(df["REV_LAG4"] > 0)
    df["REV_QOQ"] = (df["REVENUE_MUSD"] / rev_lag1_safe - 1).clip(-5, 5)
    df["REV_YOY"] = (df["REVENUE_MUSD"] / rev_lag4_safe - 1).clip(-5, 5)
    op_lag4_abs = df["OP_LAG4"].abs().replace(0, np.nan)
    df["OP_YOY"] = ((df["OPINCOME_MUSD"] - df["OP_LAG4"]) / op_lag4_abs).clip(-20, 20)

    rev_safe = df["REVENUE_MUSD"].where(df["REVENUE_MUSD"] > 0)
    df["OP_MARGIN"] = (df["OPINCOME_MUSD"] / rev_safe).clip(-3, 3)
    df["OP_MARGIN_LAG4"] = df.groupby("TICKER")["OP_MARGIN"].shift(4)
    df["OP_MARGIN_CHANGE"] = df["OP_MARGIN"] - df["OP_MARGIN_LAG4"]

    df["TURNAROUND"] = ((df["OP_LAG4"] <= 0) & (df["OPINCOME_MUSD"] > 0)).astype(int)

    # 최근 4개 QoQ 성장률 중 뒤 2개 평균 - 앞 2개 평균 = 매출 성장 가속도
    df["REV_ACCEL"] = df.groupby("TICKER")["REV_QOQ"].transform(
        lambda s: s.rolling(4, min_periods=4).apply(lambda x: x.iloc[-2:].mean() - x.iloc[:2].mean(), raw=False)
    )

    df["NEXT_EFFECTIVE_DATE"] = df.groupby("TICKER")["EFFECTIVE_DATE"].shift(-1)
    return df


def asof_price(dates: pd.Series, px: pd.DataFrame) -> pd.Series:
    """dates의 각 시점 '이후 첫 거래일' 종가를 찾아 원래 순서/인덱스로 반환."""
    tmp = pd.DataFrame({"DATE": pd.to_datetime(dates.values).astype("datetime64[ns]")}, index=dates.index)
    tmp["_ORDER"] = range(len(tmp))
    valid = tmp.dropna(subset=["DATE"]).sort_values("DATE")
    if valid.empty:
        return pd.Series(np.nan, index=dates.index)
    px = px.copy()
    px["DATE"] = pd.to_datetime(px["DATE"]).astype("datetime64[ns]")
    merged = pd.merge_asof(valid, px.sort_values("DATE"), on="DATE", direction="forward")
    result = pd.Series(np.nan, index=tmp.index)
    result.loc[merged["_ORDER"].map(lambda o: tmp.index[tmp["_ORDER"] == o][0])] = merged["CLOSE"].values
    return result


def compute_forward_returns(qtable: pd.DataFrame, tickers: list) -> pd.DataFrame:
    print("📥 가격 데이터 다운로드(3년, 전체 유니버스 일괄)...")
    end = datetime.today()
    start = end - pd.DateOffset(years=3, days=45)
    raw = agent.download_price_history(sorted(set(tickers)), start, end)
    if raw.empty:
        raise RuntimeError("가격 데이터 다운로드 실패")

    out_frames = []
    n = qtable["TICKER"].nunique()
    for i, (ticker, g) in enumerate(qtable.groupby("TICKER"), 1):
        px = raw.loc[raw["TICKER"] == ticker, ["DATE", "CLOSE"]].dropna()
        if px.empty:
            continue
        g = g.copy()
        g["PRICE_AT_FILING"] = asof_price(g["EFFECTIVE_DATE"], px)
        g["PRICE_AT_NEXT_FILING"] = asof_price(g["NEXT_EFFECTIVE_DATE"], px)
        out_frames.append(g)
        if i % 50 == 0 or i == n:
            print(f"   ...가격 정렬 {i}/{n}")

    if not out_frames:
        raise RuntimeError("가격-실적 정렬 결과가 비어 있음")

    result = pd.concat(out_frames, ignore_index=True)
    result["FWD_RET"] = result["PRICE_AT_NEXT_FILING"] / result["PRICE_AT_FILING"] - 1
    return result


def classify_surge(df: pd.DataFrame) -> pd.DataFrame:
    df = df.dropna(subset=["FWD_RET"]).copy()
    df["QUARTER_KEY"] = df["EFFECTIVE_DATE"].dt.to_period("Q").astype(str)

    bucket_size = df.groupby("QUARTER_KEY")["FWD_RET"].transform("count")
    df = df[bucket_size >= MIN_BUCKET_SIZE].copy()

    df["FWD_RET_RANK"] = df.groupby("QUARTER_KEY")["FWD_RET"].rank(pct=True)
    df["SURGE"] = (df["FWD_RET_RANK"] >= SURGE_TOP_PCT).astype(int)
    return df


def summarize_and_calibrate(df: pd.DataFrame):
    """
    임계값 선택 방식(중요): SURGE 그룹의 25th percentile을 그대로 쓰면 "급등은 했지만
    실적은 안 좋았던 케이스(가이던스, 자사주매입, 숏스퀴즈 등 다른 이유로 오른 경우)"까지
    포함되어 문턱값이 거의 0에 수렴해버린다(실제로 1차 실행에서 YOY_GROWTH_MIN이 -1.45%로
    나와 사실상 필터 역할을 못 했다). 대신 SURGE 그룹의 중앙값(50th percentile)을 쓴다 —
    "과거 급등 분기의 절반이 이 성장률 이상이었다"는 훨씬 더 의미 있는 기준이 되고,
    음수로 떨어지지 않도록 0 이상으로 하한을 둔다.
    """
    valid = df.dropna(subset=["REV_YOY"]).copy()
    surge = valid[valid["SURGE"] == 1]
    nonsurge = valid[valid["SURGE"] == 0]

    metrics = ["REV_YOY", "OP_YOY", "REV_ACCEL", "OP_MARGIN_CHANGE"]
    summary = valid.groupby("SURGE")[metrics].describe(percentiles=[.25, .5, .75]).T

    new_yoy_min = max(0.0, float(surge["REV_YOY"].median()))

    # REV_ACCEL이 SURGE/비SURGE를 실제로 구분하는지 먼저 확인한다.
    # 중앙값이 오히려 비슷하거나 역전되면 "가속도" 조건은 하드 게이트로 쓸 근거가 없다.
    accel_surge_median = float(surge["REV_ACCEL"].median()) if surge["REV_ACCEL"].notna().sum() >= 20 else np.nan
    accel_nonsurge_median = float(nonsurge["REV_ACCEL"].median()) if nonsurge["REV_ACCEL"].notna().sum() >= 20 else np.nan
    accel_discriminative = (
        pd.notna(accel_surge_median) and pd.notna(accel_nonsurge_median)
        and accel_surge_median > accel_nonsurge_median
    )
    new_accel_min = max(0.0, accel_surge_median) if accel_discriminative else 0.0

    base_rate = float(valid["SURGE"].mean())
    pred_yoy_only = valid["REV_YOY"] >= new_yoy_min
    precision = float(valid.loc[pred_yoy_only, "SURGE"].mean()) if pred_yoy_only.sum() > 0 else np.nan
    surge_mask = valid["SURGE"] == 1
    recall = float(pred_yoy_only[surge_mask].mean()) if surge_mask.sum() > 0 else np.nan

    margin_surge_median = float(surge["OP_MARGIN_CHANGE"].median()) if surge["OP_MARGIN_CHANGE"].notna().sum() >= 20 else None
    margin_nonsurge_median = float(nonsurge["OP_MARGIN_CHANGE"].median()) if nonsurge["OP_MARGIN_CHANGE"].notna().sum() >= 20 else None

    thresholds = {
        "YOY_GROWTH_MIN": round(new_yoy_min, 4),
        "ACCEL_MIN": round(new_accel_min, 4),
        "ACCEL_IS_DISCRIMINATIVE": bool(accel_discriminative),
        "ACCEL_NOTE": (
            "REV_ACCEL은 SURGE/비SURGE 그룹을 거의 구분하지 못해 하드 게이트에서 제외 권장"
            if not accel_discriminative else "REV_ACCEL이 SURGE 그룹에서 유의하게 높음"
        ),
        "TURNAROUND_RATE_IN_SURGE": round(float(surge["TURNAROUND"].mean()), 4) if len(surge) else None,
        "TURNAROUND_RATE_BASELINE": round(float(nonsurge["TURNAROUND"].mean()), 4) if len(nonsurge) else None,
        "OP_MARGIN_CHANGE_MEDIAN_SURGE": round(margin_surge_median, 4) if margin_surge_median is not None else None,
        "OP_MARGIN_CHANGE_MEDIAN_BASELINE": round(margin_nonsurge_median, 4) if margin_nonsurge_median is not None else None,
        "BASE_RATE": round(base_rate, 4),
        "PRECISION_AT_THRESHOLD": round(precision, 4) if pd.notna(precision) else None,
        "RECALL_AT_THRESHOLD": round(recall, 4) if pd.notna(recall) else None,
        "N_SURGE_QUARTERS": int(len(surge)),
        "N_TOTAL_QUARTERS": int(len(valid)),
        "OLD_YOY_GROWTH_MIN": agent.YOY_GROWTH_MIN,
        "OLD_ACCEL_MIN": agent.ACCEL_MIN,
        "CALIBRATED_AT": datetime.today().strftime("%Y-%m-%d"),
    }
    return summary, thresholds


def main():
    theme_df = agent.load_universe()
    tickers = theme_df["TICKER"].unique().tolist()
    print(f"🚀 Growth Trigger Calibration 시작 (대상 {len(tickers)}개 티커, 최근 3년)")

    qtable = build_ticker_quarter_table(tickers)
    fwd = compute_forward_returns(qtable, tickers)
    surged = classify_surge(fwd)

    if surged.empty:
        raise RuntimeError("SURGE 분류 결과가 비어 있음 (표본 부족)")

    summary, thresholds = summarize_and_calibrate(surged)

    calib_csv = os.path.join(OUTPUT_DIR, "growth_trigger_calibration.csv")
    agent.atomic_write_csv(surged, calib_csv)

    summary_csv = os.path.join(OUTPUT_DIR, "growth_trigger_summary_stats.csv")
    summary.to_csv(summary_csv, encoding="utf-8-sig")

    thresholds_path = os.path.join(OUTPUT_DIR, agent.THRESHOLDS_JSON_PATH_NAME)
    with open(thresholds_path, "w", encoding="utf-8") as f:
        json.dump(thresholds, f, ensure_ascii=False, indent=2)

    print("\n===== 캘리브레이션 결과 =====")
    for k, v in thresholds.items():
        print(f"  {k}: {v}")
    print(f"\n✅ 저장 완료: {calib_csv}")
    print(f"✅ 저장 완료: {summary_csv}")
    print(f"✅ 저장 완료: {thresholds_path} (Stock_Entry_Agent.py가 다음 실행부터 자동 사용)")


if __name__ == "__main__":
    main()
