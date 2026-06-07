# WUTIS Weather Arbitrage — Workstream A2 & B Integration

## What this is

A bot that bets on Polymarket temperature contracts for Seoul and Beijing. Polymarket lets you bet on questions like "will the highest temperature in Seoul on June 8 be exactly 28°C?" — retail traders price these badly, we price them better using real weather forecast models.

The edge: retail traders don't update every 6 hours when new model runs come in. We do.


## Files

| File | What it does |
|------|-------------|
| `weatherbet.py` | Main bot — fetches forecasts, finds mispriced bins, opens/closes positions |
| `sources_a2.py` | Additional forecast sources (JMA, CMA, GFS ensemble) — drop-in module |
| `backtest_v2.py` | Backtest — compares sources and sigma values across multiple time periods |
| `config.json` | All tunable parameters |
| `backtest.py` | Original simple backtest — 90 days, ECMWF only |



## How to run

```bash
python3 weatherbet.py          # start the bot
python3 weatherbet.py status   # balance and open positions
python3 weatherbet.py report   # full trade history
python3 sources_a2.py          # check current source disagreement
python3 backtest_v2.py         # full backtest across 3 years and all sources
python3 backtest_v2.py --quick # backtest current period only, faster
```

All three files — `weatherbet.py`, `sources_a2.py`, `backtest_v2.py` — must be in the same folder.



## What A2 added

The original bot only used ECMWF for Seoul and Beijing. HRRR (the second source) only works for US cities, so Asian cities were always running on one model alone.

A2 added three things:

**JMA (Japan Meteorological Agency)** — strong forecast skill over East Asia, good for Seoul. Available free via Open-Meteo, no API key needed.

**CMA (Chinese Meteorological Administration)** — China's native model, best available for Beijing. Uses the GRAPES-GFS model at 15km resolution. Also free via Open-Meteo.

**GFS 31-member ensemble** — instead of one temperature value, this gives 31 parallel model runs. We compute the spread between them. A tight spread (0.8°C) means the models agree and we can be confident. A wide spread (2.5°C) means the models disagree and we should size down or skip.

**Consensus logic** — when all sources are available, the bot takes a weighted average: ECMWF 45%, JMA 25%, CMA 20%, GFS mean 10%. If the sources disagree by more than 2°C, the trade is skipped entirely. This is the `[SKIP/DISAGREE]` message you see in the terminal.

**Dynamic sigma** — the original bot used a hardcoded `SIGMA_C = 1.2` for all Asian cities on all days. This controls how wide the probability distribution is over the temperature bins. Now sigma comes from the GFS ensemble spread, so it adapts per day: confident forecast = tight sigma = sharper probabilities = bigger edge. Fallback is `SIGMA_C = 2.0` based on backtest results.

**Bug fixed** — the original bot used `RKSI` (Incheon Airport) as the Seoul station. Polymarket contracts resolve on `RKSS` (Gimpo Airport), which is 25km away. At 1-2°C bin resolution that's a guaranteed mis-calibration. Fixed in both `weatherbet.py` and `backtest_v2.py`.



## What B upgraded

**`bucket_prob()` fixed** — the original function returned `1.0` for any in-bucket forecast and `0.0` for out-of-bucket. This made EV calculations meaningless. Now it uses a proper Gaussian CDF for all bins, not just the edge ones.

**Re-evaluation loop** — every 6 hours on open positions:
- edge still > 8%: hold
- edge < 2%: close, thesis played out (`edge_collapsed`)
- EV went negative: close immediately, forecast reversed (`forecast_flip`)
- less than 2 hours to resolution: hard close regardless of signal (`expiry_cutoff`)

**`min_ev` raised** — from 0.05 to 0.08 in config. Cleaner signals, less noise.



## Why the bot shows 0 trades right now

The `[SKIP/DISAGREE]` messages mean the forecast sources disagree by 3-5°C on the upcoming dates. The bot is working correctly — it's protecting against placing bets when the models don't agree. This disagreement is normal for D+2 and D+3 contracts. As the dates get closer, the models converge and trades open automatically.

To check current disagreement:
```bash
python3 sources_a2.py
```

If `Δmax` is below 2.0 for any date, the bot will trade that day.

---

## Config parameters

```json
{
  "min_ev": 0.08,        // minimum edge to enter a trade (8%)
  "kelly_fraction": 0.25, // fractional Kelly — 25% of full Kelly bet
  "max_price": 0.45,     // skip bins priced above 45 cents
  "max_slippage": 0.03,  // skip markets with bid-ask spread > 3 cents
  "min_hours": 2.0,      // don't enter trades within 2 hours of resolution
  "max_hours": 72.0,     // don't enter trades more than 72 hours out
  "max_bet": 20.0,       // hard cap per trade in EUR
  "balance": 10000.0     // starting balance (demo mode)
}
```

---

## Backtest results

```
Period: Recent 90d (2026)

Seoul:
  ECMWF  σ=2.0   Brier 0.1038   → best config
  JMA    σ=2.0   Brier 0.1041
  CMA    σ=2.0   Brier 0.1055

Beijing:
  ECMWF  σ=2.0   Brier 0.1046
  CMA    σ=2.0   Brier 0.1039   → best config
  JMA    σ=2.0   Brier 0.1051

Overall Brier score target for go-live: < 0.20
Current baseline: ~0.11 — well within target
```

Brier score ranges from 0 (perfect) to 1 (worst). Random guessing gives ~0.25. Below 0.20 is the project's go-live threshold. Current baseline of ~0.11 is strong.

---

## Go-live criteria (from project doc)

Do not switch to real EUR 100 until all five are hit across 50+ resolved contracts:

| Metric | Target |
|--------|--------|
| Brier Score | < 0.20 |
| Win Rate | > 55% |
| Average Edge at Entry | > 8% |
| Max Drawdown | < 30% |
| Sample Size | ≥ 50 contracts |

---

## Known limitations

- KMA (Korea Met Agency) is suspended on Open-Meteo as of May 2026 — they switched to a new model (KIM) and Open-Meteo hasn't migrated yet. Would be the best Seoul source if available. Monitor: https://open-meteo.com/en/docs/kma-api
- Backtest uses yesterday's actual temperature as a proxy for the forecast, not real model archive runs. This overestimates forecast accuracy. `backtest_v2.py` uses real historical model runs via the Open-Meteo Historical Forecast API which is more honest.
- Polymarket price history analysis (does price movement before resolution predict outcome?) is implemented in `sources_a2.py` as `polymarket_price_history()` but not yet wired into the re-evaluation loop. Next step for B workstream.
