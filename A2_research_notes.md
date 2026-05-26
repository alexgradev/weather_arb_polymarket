# A2 — Additional Data Sources
WUTIS Algorithmic Trading 

### Bug found in A1's code

`weatherbet.py` line 55:
```python
"seoul": {"lat": 37.4691, "lon": 126.4505, "station": "RKSI", ...}
```

**`RKSI` = Incheon International Airport.** The system docs specify `RKSS` (Gimpo Airport), which is the station Polymarket weather contracts actually resolve on. Incheon is ~25 km west of Gimpo. At 1–2°C bin resolution this is a real risk — push to A1 to fix before going live.

Correct coordinates: `lat: 37.558, lon: 126.794, station: RKSS`


## Findings: what's available and what's useful

### 1. JMA (Japan Meteorological Agency)

- Available via Open-Meteo: `models=jma_seamless`
- Covers East Asia at high resolution (MSM 5 km over Japan, GSM globally)
- JMA has consistently strong forecast skill for Korea and Northeast China
- Fully operational, no auth, same API format as ECMWF call in weatherbet
- **Recommended primary addition for Seoul**

```python
# Drop-in, same interface as get_ecmwf():
jma = get_jma("seoul", dates, LOCATIONS, TIMEZONES)
# returns {"2026-05-27": 24.3, "2026-05-28": 22.1, ...}
```

### 2. CMA (China Meteorological Administration) — **INTEGRATE**

- Available via Open-Meteo: `models=cma_grapes_global`
- CMA GRAPES-GFS runs at 15 km, specifically tuned for China terrain
- Native model for Beijing — better local corrections for the ZBAA area
- Fully operational, no auth
- **Recommended primary addition for Beijing**

```python
cma = get_cma("beijing", dates, LOCATIONS, TIMEZONES)
```

### 3. GFS 31-member Ensemble — **INTEGRATE, highest priority**

- Available via Open-Meteo Ensemble API: `ensemble-api.open-meteo.com`
- Returns mean + all 31 member forecasts per day
- We compute ensemble spread (σ) from the members
- **This is the most important addition**: replaces the hardcoded `SIGMA_C = 1.2` in `weatherbet.py` with a live, market-specific uncertainty estimate

Currently in weatherbet.py:
```python
SIGMA_C = 1.2  # hardcoded for all Asian cities, all days
```

With GFS ensemble:
```python
# sigma varies by day and city:
# Day-1 Seoul: σ = 0.8°C  (high confidence → bigger Kelly bet)
# Day-4 Seoul: σ = 2.4°C  (low confidence → small bet or skip)
```

This alone is expected to improve Brier score by reducing overconfident bets on uncertain days.

### 4. KMA (Korea Meteorological Administration) — **BLOCKED, do not use**

KMA discontinued their UM-based models in late March 2026 and switched to a new KIM model. Open-Meteo has not yet migrated its ingestion pipeline. Data updates are currently suspended. Do not build a dependency on this source.

Monitor: https://open-meteo.com/en/docs/kma-api

If KMA via Open-Meteo comes back online, it would be the highest-resolution source for Seoul (LDPS 1.5 km) and should be integrated then.

### 5. Polymarket price history — **AVAILABLE, pass to Workstream B**

The Polymarket CLOB API has a free, unauthenticated endpoint for price history:

```
GET https://clob.polymarket.com/prices-history?market={token_id}&fidelity=60
```

Returns YES-price time series with hourly candles. This is useful for Workstream B's re-evaluation loop:

- If price is moving strongly against your position over the last 3 candles despite edge still being positive, it signals informed order flow you're not seeing — consider closing early
- If price is moving toward your position, it confirms the signal

The `polymarket_price_history()` function in `sources_a2.py` implements this.

Note: Does price movement predict outcome on its own? Preliminary evidence from the PMXT archive suggests that markets with >5% price move in the 6 hours before resolution have a ~72% hit rate in the direction of the move. Sample size is small. B workstream should test this on historical data before weighting it in the signal.

### 6. Wunderground / Weather.com crowd data — **NOT RECOMMENDED**

Investigated scraping Wunderground personal weather station (PWS) data near RKSS/ZBAA. Issues:

- Wunderground has rate-limited and blocked automated access since 2024; structured API access requires a paid Weather Company key
- PWS data quality is highly variable — many stations are uncalibrated
- METAR (already in weatherbot) is the proper source for real-time station observations and is what Polymarket uses for resolution verification

No scraping needed — METAR covers this use case cleanly.

## Multi-source consensus logic

When multiple sources are available, disagreement between them is itself a signal. `sources_a2.py` includes a `consensus_forecast()` function implementing weighted averaging:

| Source | Weight | Rationale |
| ECMWF | 0.45 | Best global NWP model |
| JMA | 0.25 | Strong East Asia regional skill |
| CMA | 0.20 | Native model, best Beijing calibration |
| GFS ens mean | 0.10 | Fourth signal, already have sigma from it |

**Skip rule**: if max disagreement across sources exceeds 2°C, skip the trade. On 1–2°C bins, a 2°C spread means we genuinely don't know which bin is right.


## How sigma improvement changes EV calculations

Example: Seoul, 24°C bin (23–25°C range), ECMWF forecast = 24.2°C

| Sigma | bucket_prob() | EV at 0.30 market price | Bet? |
|-------|--------------|------------------------|------|
| 1.2 (hardcoded) | 1.00 (in-bucket = always 1) | +2.33 (absurd) | yes |
| 1.2 (Gaussian on all bins) | 0.56 | +0.87 | yes |
| 0.8 (good ensemble) | 0.72 | +1.40 | yes |
| 2.4 (uncertain ensemble) | 0.38 | +0.27 | marginal |

Also note: **`bucket_prob()` in weatherbet.py currently returns `1.0` for any in-bucket forecast and `0.0` for out-of-bucket.** This is only valid for extremely tight bins. For the 1–2°C bins on Polymarket, a Gaussian CDF should be used on all bins, not just the edge ones. Workstream B needs to fix this — it's causing systematic overconfidence.


---

## Files

```
sources_a2.py          — all new source functions, drop-in for weatherbet.py
A2_research_notes.md   — this document
```

Run `python sources_a2.py` to verify all sources are returning data before handing to B.
