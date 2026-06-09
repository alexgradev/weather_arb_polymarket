#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sources_a2.py — A2 Additional Forecast Sources
Drop-in additions for weatherbet.py. Each function returns a dict of
{date_str: float} matching the interface of get_ecmwf() / get_hrrr().

Sources added:
    1. JMA (Japan Met Agency)  — best East Asia skill, Seoul + Beijing, 7-day
    2. CMA (China Met Admin)   — native China model, best for Beijing ZBAA, 7-day
    3. GFS Ensemble            — 31-member spread → dynamic sigma for bucket_prob()
    4. Polymarket price history — CLOB API, YES-price time-series per token_id

STATUS NOTES (May 2026):
    - KMA via Open-Meteo is SUSPENDED — they switched from UM to KIM model
      and Open-Meteo has not yet migrated. Do NOT use until Open-Meteo confirms
      KIM support. Monitor: https://open-meteo.com/en/docs/kma-api
    - JMA replaces KMA for Seoul (similar East Asia skill, fully operational)
    - CMA is the best native model for Beijing (GRAPES global model, 15 km)
    - GFS Ensemble is the single biggest upgrade: gives a live sigma to replace
      the hardcoded SIGMA_C = 1.2 in weatherbet.py

Integration in weatherbet.py — add to take_forecast_snapshot():
    from sources_a2 import get_jma, get_cma, get_gfs_ensemble, get_dynamic_sigma

    ens  = get_gfs_ensemble(city_slug, dates, LOCATIONS, TIMEZONES)
    jma  = get_jma(city_slug, dates, LOCATIONS, TIMEZONES)          # Seoul
    cma  = get_cma(city_slug, dates, LOCATIONS, TIMEZONES)          # Beijing

    # Then in the signal block, replace get_sigma() call with:
    sigma = get_dynamic_sigma(city_slug, date, ens, _cal)
    p = bucket_prob(forecast_temp, t_low, t_high, sigma)
"""

import time
import requests
from datetime import datetime, timezone, timedelta


# 1. JMA (Japan Meteorological Agency) via Open-Meteo
#    Model: jma_seamless (MSM 5 km over Japan, GSM 20 km Asia)
#    Best for: Seoul (good East Asia coverage), also usable for Beijing
#    Horizon: 7 days, updates: 4x/day
#    Auth: none  |  URL: https://open-meteo.com/en/docs/jma-api

def get_jma(city_slug: str, dates: list, locations: dict, timezones: dict) -> dict:
    """
    Fetch JMA GSM forecast via Open-Meteo.

    Returns {date_str: float} — celsius, 1 dp.
    """
    loc = locations.get(city_slug, {})
    result = {}
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={loc['lat']}&longitude={loc['lon']}"
        f"&daily=temperature_2m_max"
        f"&temperature_unit=celsius"
        f"&forecast_days=7"
        f"&timezone={timezones.get(city_slug, 'UTC')}"
        f"&models=jma_seamless"
    )
    for attempt in range(3):
        try:
            data = requests.get(url, timeout=(5, 10)).json()
            if "error" not in data:
                for date, temp in zip(data["daily"]["time"], data["daily"]["temperature_2m_max"]):
                    if date in dates and temp is not None:
                        result[date] = round(float(temp), 1)
            break
        except Exception as e:
            if attempt < 2:
                time.sleep(3)
            else:
                print(f"  [JMA] {city_slug}: {e}")
    return result


# 2. CMA (Chinese Meteorological Administration) via Open-Meteo
#    Model: cma_grapes_global (GRAPES-GFS 15 km)
#    Best for: Beijing ZBAA — home model, best calibration for North China
#    Also useful: Seoul
#    Horizon: 7 days, updates: 4x/day
#    Auth: none  |  URL: https://open-meteo.com/en/docs/cma-api


def get_cma(city_slug: str, dates: list, locations: dict, timezones: dict) -> dict:
    """
    Fetch CMA GRAPES forecast via Open-Meteo.

    Returns {date_str: float} — celsius, 1 dp.
    Particularly valuable for Beijing — native model means better terrain
    corrections for the Capital Airport (ZBAA) area.
    """
    loc = locations.get(city_slug, {})
    result = {}
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={loc['lat']}&longitude={loc['lon']}"
        f"&daily=temperature_2m_max"
        f"&temperature_unit=celsius"
        f"&forecast_days=7"
        f"&timezone={timezones.get(city_slug, 'UTC')}"
        f"&models=cma_grapes_global"
    )
    for attempt in range(3):
        try:
            data = requests.get(url, timeout=(5, 10)).json()
            if "error" not in data:
                for date, temp in zip(data["daily"]["time"], data["daily"]["temperature_2m_max"]):
                    if date in dates and temp is not None:
                        result[date] = round(float(temp), 1)
            break
        except Exception as e:
            if attempt < 2:
                time.sleep(3)
            else:
                print(f"  [CMA] {city_slug}: {e}")
    return result


# 3. GFS Ensemble via Open-Meteo Ensemble API
#    31-member ensemble → mean temperature + spread (sigma)
#    THIS IS THE MOST IMPORTANT A2 ADDITION.
#    The spread replaces the hardcoded SIGMA_C = 1.2 in weatherbet.py,
#    giving us a live, calibrated uncertainty estimate per market.
#    Auth: none  |  URL: https://open-meteo.com/en/docs/ensemble-api


def get_gfs_ensemble(city_slug: str, dates: list, locations: dict, timezones: dict) -> dict:
    """
    Fetch GFS 31-member ensemble via Open-Meteo Ensemble API.

    Returns {date_str: {"mean": float, "sigma": float, "p10": float, "p90": float, "n": int}}

    The sigma field is the ensemble spread (std dev across members).
    Use it as the sigma argument in weatherbet's bucket_prob() to get a
    data-driven, market-specific uncertainty estimate.

    High sigma (>2.5°C) = uncertain forecast = smaller position.
    Low sigma (<1.0°C) = confident forecast = full Kelly sizing.
    """
    loc = locations.get(city_slug, {})
    result = {}
    url = (
        f"https://ensemble-api.open-meteo.com/v1/ensemble"
        f"?latitude={loc['lat']}&longitude={loc['lon']}"
        f"&daily=temperature_2m_max"
        f"&temperature_unit=celsius"
        f"&forecast_days=7"
        f"&timezone={timezones.get(city_slug, 'UTC')}"
        f"&models=gfs_seamless"
    )
    for attempt in range(3):
        try:
            data = requests.get(url, timeout=(5, 15)).json()
            if "error" in data:
                break

            times = data["daily"]["time"]
            mean_col = data["daily"].get("temperature_2m_max", [])
            member_keys = [k for k in data["daily"] if k.startswith("temperature_2m_max_member")]
            member_data = [data["daily"][k] for k in member_keys]

            for i, date in enumerate(times):
                if date not in dates:
                    continue
                mean_val = mean_col[i] if i < len(mean_col) and mean_col[i] is not None else None
                if mean_val is None:
                    continue
                vals = [m[i] for m in member_data if i < len(m) and m[i] is not None]
                if len(vals) >= 2:
                    avg = sum(vals) / len(vals)
                    sigma = round((sum((v - avg)**2 for v in vals) / (len(vals) - 1))**0.5, 2)
                    sv = sorted(vals)
                    p10 = round(sv[int(len(sv) * 0.10)], 1)
                    p90 = round(sv[int(len(sv) * 0.90)], 1)
                else:
                    sigma, p10, p90 = 1.5, round(float(mean_val), 1), round(float(mean_val), 1)
                result[date] = {
                    "mean": round(float(mean_val), 1),
                    "sigma": sigma,
                    "p10": p10,
                    "p90": p90,
                    "n": len(vals),
                }
            break
        except Exception as e:
            if attempt < 2:
                time.sleep(3)
            else:
                print(f"  [GFS_ENS] {city_slug}: {e}")
    return result



# 4. Dynamic sigma — replaces hardcoded SIGMA_C = 1.2 in weatherbet.py


def get_dynamic_sigma(
    city_slug: str,
    date_str: str,
    ensemble_result: dict,
    calibration: dict,
    fallback: float = 1.5,
) -> float:
    """
    Best available sigma for bucket_prob(), in priority order:
      1. GFS ensemble spread for this date (live, calibrated per day)
      2. Per-city MAE from calibration.json (historical accuracy)
      3. Fallback constant

    Usage in weatherbet.py scan_and_update():
        ens = get_gfs_ensemble(city_slug, dates, LOCATIONS, TIMEZONES)
        # ... in the signal loop:
        sigma = get_dynamic_sigma(city_slug, date, ens, _cal)
        p = bucket_prob(forecast_temp, t_low, t_high, sigma)
    """
    ens = ensemble_result.get(date_str, {})
    if ens.get("sigma") is not None:
        return float(ens["sigma"])
    cal_key = f"{city_slug}_ecmwf"
    if cal_key in calibration and calibration[cal_key].get("sigma") is not None:
        return float(calibration[cal_key]["sigma"])
    return fallback



# 5. Multi-source consensus forecast
#    Weighted mean of available sources

SOURCE_WEIGHTS = {
    "ecmwf": 0.45,   # globally best NWP model
    "jma":   0.25,   # strong East Asia regional skill
    "cma":   0.20,   # native model, especially Beijing
    "gfs":   0.10,   # GFS ensemble mean as fourth signal
}

def consensus_forecast(
    ecmwf_val,
    jma_val,
    cma_val,
    gfs_mean_val,
    max_disagreement: float = 2.0,
) -> dict:
    """
    Weighted mean of all available source forecasts.

    Returns {
        "temp": float or None,      # weighted mean
        "disagreement": float,      # max spread across sources
        "skip": bool,               # True if sources disagree > max_disagreement
        "sources_used": int,        # how many sources contributed
    }

    If skip=True, don't trade this market — the models disagree too much
    to have a reliable probability estimate.
    """
    sources = {}
    if ecmwf_val is not None:  sources["ecmwf"] = ecmwf_val
    if jma_val   is not None:  sources["jma"]   = jma_val
    if cma_val   is not None:  sources["cma"]   = cma_val
    if gfs_mean_val is not None: sources["gfs"] = gfs_mean_val

    if not sources:
        return {"temp": None, "disagreement": 0.0, "skip": True, "sources_used": 0}

    total_weight = sum(SOURCE_WEIGHTS.get(k, 0.10) for k in sources)
    weighted_sum = sum(v * SOURCE_WEIGHTS.get(k, 0.10) for k, v in sources.items())
    mean_temp = round(weighted_sum / total_weight, 1) if total_weight > 0 else None

    vals = list(sources.values())
    disagreement = round(max(vals) - min(vals), 2) if len(vals) > 1 else 0.0
    skip = disagreement > max_disagreement

    return {
        "temp": mean_temp,
        "disagreement": disagreement,
        "skip": skip,
        "sources_used": len(sources),
        "sources": sources,
    }


# 6. Polymarket price history (CLOB API, no auth)


def polymarket_price_history(token_id: str, fidelity: int = 60) -> list:
    """
    Fetch YES-price history from Polymarket CLOB API.

    Args:
        token_id:  market_id from weatherbet.py (Polymarket outcome token)
        fidelity:  candle size in minutes (1, 5, 60, 1440)

    Returns:
        [{"t": unix_ts, "p": float, "v": float}, ...]
    """
    try:
        resp = requests.get(
            "https://clob.polymarket.com/prices-history",
            params={"market": token_id, "fidelity": fidelity},
            timeout=(5, 10),
        )
        return [{"t": h["t"], "p": float(h["p"]), "v": float(h.get("v", 0))}
                for h in resp.json().get("history", [])]
    except Exception as e:
        print(f"  [PRICE_HISTORY] {str(token_id)[:12]}...: {e}")
        return []


def price_momentum(history: list, lookback: int = 3) -> float:
    """Price change over last N candles. Positive = market moving toward YES."""
    if len(history) < lookback + 1:
        return 0.0
    return round(history[-1]["p"] - history[-(lookback + 1)]["p"], 4)


# 7. Comparison table 

def compare_sources(city_slug: str, dates: list, locations: dict, timezones: dict):
    """
    Print a comparison table of all available sources.

    Example:
        from sources_a2 import compare_sources
        from weatherbet import LOCATIONS, TIMEZONES
        from datetime import datetime, timezone, timedelta

        today = datetime.now(timezone.utc)
        dates = [(today + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(4)]
        compare_sources("seoul", dates, LOCATIONS, TIMEZONES)
        compare_sources("beijing", dates, LOCATIONS, TIMEZONES)
    """
    print(f"\n{'='*72}")
    print(f"  Source comparison: {city_slug.upper()}")
    print(f"{'='*72}")
    print(f"  {'Date':<12} {'ECMWF':>7} {'JMA':>7} {'CMA':>7} {'GFS_μ':>7} {'GFS_σ':>7} {'Δmax':>7}")
    print(f"  {'-'*68}")

    try:
        from weatherbet import get_ecmwf
        ecmwf = get_ecmwf(city_slug, dates)
    except ImportError:
        ecmwf = {}

    jma = get_jma(city_slug, dates, locations, timezones)
    cma = get_cma(city_slug, dates, locations, timezones)
    ens = get_gfs_ensemble(city_slug, dates, locations, timezones)

    for date in dates:
        ev = ecmwf.get(date)
        jv = jma.get(date)
        cv = cma.get(date)
        gv = ens.get(date, {})
        gmu = gv.get("mean")
        gsi = gv.get("sigma")

        vals = [v for v in [ev, jv, cv, gmu] if v is not None]
        delta = round(max(vals) - min(vals), 1) if len(vals) > 1 else None

        fmt = lambda v: f"{v:>7.1f}" if v is not None else f"{'—':>7}"
        warn = "  ← SKIP" if (delta is not None and delta > 2.0) else ""
        print(f"  {date:<12} {fmt(ev)} {fmt(jv)} {fmt(cv)} {fmt(gmu)} {fmt(gsi)} {fmt(delta)}{warn}")

    print()
    print("  Interpretation:")
    print("  GFS_σ  = ensemble spread → use as sigma in bucket_prob()")
    print("  Δmax   = max source disagreement → skip trade if >2.0°C")
    print("  KMA    = SUSPENDED on Open-Meteo (KIM migration, May 2026)")
    print()


# Self-test


if __name__ == "__main__":
    TEST_LOCATIONS = {
        "seoul":   {"lat": 37.4691, "lon": 126.4505, "unit": "C", "region": "asia"},  # Incheon RKSI
        "beijing": {"lat": 40.080, "lon": 116.585, "unit": "C", "region": "asia"},
    }
    TEST_TIMEZONES = {"seoul": "Asia/Seoul", "beijing": "Asia/Shanghai"}

    today = datetime.now(timezone.utc)
    dates = [(today + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(4)]

    for city in ["seoul", "beijing"]:
        compare_sources(city, dates, TEST_LOCATIONS, TEST_TIMEZONES)
