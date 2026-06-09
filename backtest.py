"""
Brier score backtest for weatherbet
// 90 days - uses yesterday's actual as proxy for forecast - good enough as baseline
"""

import math
import requests
from datetime import datetime, timedelta, timezone

#---

def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def bucket_prob(forecast, t_low, t_high, sigma=1.2):
    fc = float(forecast)
    if t_low == -999:
        return norm_cdf((t_high + 0.5 - fc) / sigma)
    if t_high == 999:
        return 1.0 - norm_cdf((t_low - 0.5 - fc) / sigma)
    return norm_cdf((t_high + 0.5 - fc) / sigma) - norm_cdf((t_low - 0.5 - fc) / sigma)

def calc_ev(p, price):
    if price <= 0 or price >= 1: return 0.0
    return round(p * (1.0 / price - 1.0) - (1.0 - p), 4)

def calc_kelly(p, price):
    if price <= 0 or price >= 1: return 0.0
    b = 1.0 / price - 1.0
    f = (p * b - (1.0 - p)) / b
    return round(min(max(0.0, f) * 0.25, 1.0), 4)

def bet_size(kelly, balance):
    raw = kelly * balance
    return round(min(raw, 30), 2)

#---

CITIES = {
    "seoul":   {"lat": 37.4691, "lon": 126.4505, "name": "Seoul"},
    "beijing": {"lat": 40.0801, "lon": 116.585,  "name": "Beijing"},
}

#---

def fetch_historical(lat, lon, start, end):
    """Returns dict of {date_str: actual_max_temp} and {date_str: ecmwf_forecast}"""
    url = (
        f"https://archive-api.open-meteo.com/v1/archive"
        f"?latitude={lat}&longitude={lon}"
        f"&start_date={start}&end_date={end}"
        f"&daily=temperature_2m_max"
        f"&temperature_unit=celsius"
        f"&timezone=Asia/Seoul"  #close enough for daily max
    )
    r = requests.get(url, timeout=(10, 15)).json()
    dates  = r["daily"]["time"]
    temps  = r["daily"]["temperature_2m_max"]
    return {d: round(t, 1) for d, t in zip(dates, temps) if t is not None}

#---

def make_bins(center_temp):
    base = round(center_temp)
    bins = []
    bins.append((-999, base - 4))        #x or below bin
    for v in range(base - 3, base + 4):
        bins.append((v, v))              #exact point bins, +-0,5 applied inside
    bins.append((base + 4, 999))         #x or above edge bin
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

#---

def brier_score(predictions):
    if not predictions:
        return None
    return round(sum((p - o) ** 2 for p, o in predictions) / len(predictions), 4)

#---

def run_backtest():
    end_date   = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
    start_date = (datetime.now(timezone.utc) - timedelta(days=92)).strftime("%Y-%m-%d")
    print(f"\nBacktest period: {start_date} - {end_date}")
    print(f"{'─'*25}")

    all_predictions = []

    for city_slug, loc in CITIES.items():
        print(f"\n{loc['name']}:")
        temps = fetch_historical(loc["lat"], loc["lon"], start_date, end_date)
        if not temps:
            print("  No data")
            continue
        predictions = []
        dates = sorted(temps.keys())

        for i, date in enumerate(dates):
            #D+1 forecast
            if i == 0:
                continue  
            forecast_date = dates[i - 1]
            forecast_temp = temps[forecast_date]  # yesterday's actual as proxy for forecast
            actual_temp   = temps[date]

            bins = make_bins(forecast_temp)

            #which bin the actual temp landed in
            actual_bin = find_actual_bin(actual_temp, bins)
            if actual_bin is None:
                continue

            #predicted probability for the bin that actually happened
            p = bucket_prob(forecast_temp, actual_bin[0], actual_bin[1])
            predictions.append((p, 1))  # outcome=1 because the bin that happened

            # score all the other bins they should get low probabilities
            for b in bins:
                if b != actual_bin:
                    p_other = bucket_prob(forecast_temp, b[0], b[1])
                    predictions.append((p_other, 0))  # outcome=0 didn't happen

        bs = brier_score(predictions)
        city_preds = [p for p, o in predictions if o == 1]
        avg_p = round(sum(city_preds) / len(city_preds), 3) if city_preds else 0
        print(f"  Days tested:      {len(dates) - 1}")
        print(f"  Avg p(correct bin): {avg_p:.3f}")
        print(f"  Brier score:      {bs}")
        all_predictions.extend(predictions)

    overall = brier_score(all_predictions)
    print(f"\n{'─'*25}")
    print(f"Overall Brier score (both cities): {overall}")

if __name__ == "__main__":
    run_backtest()

"""
Results I got:

Backtest period: 2026-02-21 - 2026-05-22
─────────────────────────

Seoul:
  Days tested:      90
  Avg p(correct bin): 0.136
  Brier score:      0.1062

Beijing:
  Days tested:      90
  Avg p(correct bin): 0.084
  Brier score:      0.1179

─────────────────────────
Overall Brier score (both cities): 0.112

"""