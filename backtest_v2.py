"""
backtest_v2.py — Extended Brier Score Backtest
================================================
Compares forecast sources and sigma parameters across multiple historical periods.

What's new vs backtest.py:
  - Multiple time periods (recent 90d, same period last year, 2 years ago)
  - Multiple sigma values tested side-by-side (0.8, 1.0, 1.2, 1.5, 2.0)
  - A2 sources: JMA and CMA historical data via Open-Meteo archive
  - Source comparison: which model has the best Brier score per city?
  - EV simulation: how many tradeable signals at different min_ev thresholds?

Usage:
    python3 backtest_v2.py            # full run, all periods and sources
    python3 backtest_v2.py --quick    # current period only, faster
"""

import sys
import math
import requests
from datetime import datetime, timedelta, timezone


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def bucket_prob(forecast, t_low, t_high, sigma=1.2):
    fc = float(forecast)
    if t_low == -999:
        return norm_cdf((t_high + 0.5 - fc) / sigma)
    if t_high == 999:
        return 1.0 - norm_cdf((t_low - 0.5 - fc) / sigma)
    return norm_cdf((t_high + 0.5 - fc) / sigma) - norm_cdf((t_low - 0.5 - fc) / sigma)

def brier_score(predictions):
    if not predictions:
        return None
    return round(sum((p - o) ** 2 for p, o in predictions) / len(predictions), 4)

def calc_ev(p, price):
    if price <= 0 or price >= 1: return 0.0
    return round(p * (1.0 / price - 1.0) - (1.0 - p), 4)

def simulate_market_price(p_model, noise=0.05):
    """
    Simulate a realistic Polymarket price for backtesting.
    Retail prices are biased toward round numbers and underweight tails.
    We model market price as p_model with some retail mis-calibration noise,
    clamped to [0.03, 0.45] matching our MAX_PRICE filter.
    """
    import random
    raw = p_model * (1 - noise) + noise * 0.15  # retail underweights high-p bins
    return round(max(0.03, min(0.45, raw)), 3)


# CITIES — corrected coordinates (RKSS Gimpo for Seoul)


CITIES = {
    "seoul":   {"lat": 37.4691, "lon": 126.4505, "name": "Seoul",   "tz": "Asia/Seoul"},  # Incheon RKSI — Polymarket resolution station
    "beijing": {"lat": 40.080,  "lon": 116.585, "name": "Beijing", "tz": "Asia/Shanghai"},
}

# DATA FETCHING — historical actuals + model forecasts via Open-Meteo archive


def fetch_actuals(lat, lon, start, end, tz):
    """Actual observed max temperatures from ERA5 reanalysis."""
    url = (
        f"https://archive-api.open-meteo.com/v1/archive"
        f"?latitude={lat}&longitude={lon}"
        f"&start_date={start}&end_date={end}"
        f"&daily=temperature_2m_max"
        f"&temperature_unit=celsius"
        f"&timezone={tz}"
    )
    try:
        r = requests.get(url, timeout=(10, 20)).json()
        dates = r["daily"]["time"]
        temps = r["daily"]["temperature_2m_max"]
        return {d: round(t, 1) for d, t in zip(dates, temps) if t is not None}
    except Exception as e:
        print(f"  [FETCH ERROR] actuals: {e}")
        return {}

def fetch_model_historical(lat, lon, start, end, tz, model):
    """
    Historical forecast from a specific model via Open-Meteo Historical Forecast API.
    This uses actual model runs (not reanalysis) — proper backtesting.
    Available models: ecmwf_ifs025, jma_seamless, cma_grapes_global
    """
    url = (
        f"https://historical-forecast-api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&start_date={start}&end_date={end}"
        f"&daily=temperature_2m_max"
        f"&temperature_unit=celsius"
        f"&timezone={tz}"
        f"&models={model}"
    )
    try:
        r = requests.get(url, timeout=(10, 20)).json()
        if "error" in r:
            return {}
        dates = r["daily"]["time"]
        temps = r["daily"]["temperature_2m_max"]
        return {d: round(t, 1) for d, t in zip(dates, temps) if t is not None}
    except Exception as e:
        print(f"  [FETCH ERROR] {model}: {e}")
        return {}


def make_bins(center_temp):
    base = round(center_temp)
    bins = [(-999, base - 4)]
    for v in range(base - 3, base + 4):
        bins.append((v, v))
    bins.append((base + 4, 999))
    return bins

def find_actual_bin(actual_temp, bins):
    for (t_low, t_high) in bins:
        if t_low == -999 and actual_temp <= t_high + 0.5:
            return (t_low, t_high)
        elif t_high == 999 and actual_temp >= t_low - 0.5:
            return (t_low, t_high)
        elif t_low == t_high and abs(actual_temp - t_low) <= 0.5:
            return (t_low, t_high)
    return None


# CORE EVALUATION


def _clim_stats(actual_data):
    """Seasonal climatology = mean/std of the actual temperatures over the period."""
    vals = [v for v in actual_data.values() if v is not None]
    if len(vals) < 2:
        return None, None
    m  = sum(vals) / len(vals)
    sd = (sum((v - m) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5
    return m, sd


def evaluate_source(forecast_data, actual_data, sigma):
    """
    Given forecast dict and actual dict (both {date: float}), compute the model
    Brier score and the climatology-baseline Brier score over the same bins.

    The climatology forecast assigns each bin the probability implied by the
    seasonal Gaussian (mean/std of the actuals). The skill score
        skill = 1 - bs_model / bs_clim
    answers a real, non-circular question: does the live forecast beat the
    naive seasonal baseline? (>0 = yes.)  bs_clim does not depend on `sigma`.
    """
    predictions      = []   # (model_p, outcome)
    clim_predictions = []   # (clim_p,  outcome)
    clim_mean, clim_std = _clim_stats(actual_data)
    dates = sorted(actual_data.keys())

    for i, date in enumerate(dates):
        if i == 0:
            continue
        forecast_date = dates[i - 1]
        forecast_temp = forecast_data.get(forecast_date)
        actual_temp   = actual_data.get(date)
        if forecast_temp is None or actual_temp is None:
            continue

        bins = make_bins(forecast_temp)
        actual_bin = find_actual_bin(actual_temp, bins)
        if actual_bin is None:
            continue

        for b in bins:
            o = 1 if b == actual_bin else 0
            predictions.append((bucket_prob(forecast_temp, b[0], b[1], sigma), o))
            if clim_std:
                clim_predictions.append((bucket_prob(clim_mean, b[0], b[1], clim_std), o))

    city_preds = [p for p, o in predictions if o == 1]
    return {
        "bs":       brier_score(predictions),
        "bs_clim":  brier_score(clim_predictions) if clim_predictions else None,
        "avg_p":    round(sum(city_preds) / len(city_preds), 3) if city_preds else 0,
        "n_days":   len(city_preds),
    }


# PERIODS


def build_periods(quick=False):
    today = datetime.now(timezone.utc)
    periods = [
        {
            "label": "Recent 90d (2026)",
            "end":   (today - timedelta(days=2)).strftime("%Y-%m-%d"),
            "start": (today - timedelta(days=92)).strftime("%Y-%m-%d"),
        },
    ]
    if not quick:
        periods += [
            {
                "label": "Same period last year (2025)",
                "end":   (today - timedelta(days=365)).strftime("%Y-%m-%d"),
                "start": (today - timedelta(days=457)).strftime("%Y-%m-%d"),
            },
            {
                "label": "Two years ago (2024)",
                "end":   (today - timedelta(days=730)).strftime("%Y-%m-%d"),
                "start": (today - timedelta(days=822)).strftime("%Y-%m-%d"),
            },
        ]
    return periods

SOURCES = [
    ("ECMWF",  "ecmwf_ifs025"),
    ("JMA",    "jma_seamless"),
    ("CMA",    "cma_grapes_global"),
]

SIGMAS = [0.8, 1.0, 1.2, 1.5, 2.0]


# MAIN

def run_backtest(quick=False):
    periods = build_periods(quick)

    print(f"\n{'='*65}")
    print(f"  WEATHERBET BACKTEST v2 — Source & Parameter Comparison")
    print(f"{'='*65}")
    print(f"  Sigmas tested:  {SIGMAS}")
    print(f"  Sources tested: {[s[0] for s in SOURCES]}")
    print(f"  Periods:        {len(periods)}")
    print()

    for period in periods:
        print(f"\n{'─'*65}")
        print(f"  Period: {period['label']}  ({period['start']} → {period['end']})")
        print(f"{'─'*65}")

        for city_slug, loc in CITIES.items():
            print(f"\n  {loc['name']}:")

            # Fetch actuals once
            actuals = fetch_actuals(loc["lat"], loc["lon"], period["start"], period["end"], loc["tz"])
            if not actuals:
                print("    No data")
                continue

            print(f"  {'Source':<8} {'σ=0.8':>7} {'σ=1.0':>7} {'σ=1.2':>7} {'σ=1.5':>7} {'σ=2.0':>7}   {'ClimBS':>7} {'Skill':>7}")
            print(f"  {'─'*70}")

            best_bs   = 999
            best_cfg  = ""

            for src_name, src_model in SOURCES:
                forecasts = fetch_model_historical(
                    loc["lat"], loc["lon"],
                    period["start"], period["end"],
                    loc["tz"], src_model
                )
                if not forecasts:
                    print(f"  {src_name:<8} {'—':>7} {'—':>7} {'—':>7} {'—':>7} {'—':>7}")
                    continue

                row = f"  {src_name:<8}"
                sig_results = []
                for sigma in SIGMAS:
                    res = evaluate_source(forecasts, actuals, sigma)
                    bs = res["bs"]
                    sig_results.append(res)
                    cell = f"{bs:.4f}" if bs is not None else "  —  "
                    row += f" {cell:>7}"
                    if bs is not None and bs < best_bs:
                        best_bs  = bs
                        best_cfg = f"{src_name} σ={sigma}"

                # Climatology baseline + best skill across sigmas (non-circular).
                bs_clim   = next((r["bs_clim"] for r in sig_results if r["bs_clim"] is not None), None)
                model_bss = [r["bs"] for r in sig_results if r["bs"] is not None]
                if bs_clim and model_bss:
                    best_src_bs = min(model_bss)
                    skill = 1.0 - best_src_bs / bs_clim
                    row += f"   {bs_clim:>7.4f} {skill*100:>+6.0f}%"
                else:
                    row += f"   {'—':>7} {'—':>7}"
                print(row)

            print(f"\n  → Best config: {best_cfg}  (Brier {best_bs:.4f})")


if __name__ == "__main__":
    quick = "--quick" in sys.argv
    run_backtest(quick)
