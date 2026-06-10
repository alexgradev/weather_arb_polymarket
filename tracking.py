#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tracking.py — Dashboard data aggregator (read-only)
===================================================
Reads the engine's state + per-market files and writes ONE consolidated feed,
data/dashboard.json, that the dashboard (dashboard.html) renders.

This is deliberately a separate, read-only layer:
  engine  →  data/markets/*.json + data/state.json   (written by weatherbet.py)
  tracking.py  →  data/dashboard.json                 (aggregated view + metrics)
  dashboard.html  →  reads data/dashboard.json        (presentation only)

The "execution seam" for a future click-to-trade dashboard:
each open position and each actionable signal carries an `execution` block with
exactly the fields a real Polymarket CLOB order would need (market_id, side,
limit_price, size_usd) plus status="paper". To go live you implement ONE
backend endpoint that turns such a block into a signed CLOB order — nothing in
the data model or the dashboard needs to change shape.

Usage:
    python tracking.py            # regenerate data/dashboard.json once
    python tracking.py --watch    # regenerate every 30s
"""

import sys
import json
import time
from pathlib import Path
from datetime import datetime, timezone

DATA_DIR    = Path("data")
STATE_FILE  = DATA_DIR / "state.json"
MARKETS_DIR = DATA_DIR / "markets"
OUT_FILE    = DATA_DIR / "dashboard.json"
META_FILE   = DATA_DIR / "tracking_meta.json"   # persists the tracking start time

# Go-live criteria (from README_A2 integration.md — "from project doc").
# NOTE: the Brier target 0.20 is a weak baseline (see climatology skill in
# backtest_v2.py). Kept here only to mirror the stated project gate.
TARGETS = {
    "brier":        {"op": "<",  "value": 0.20, "label": "Brier Score"},
    "win_rate":     {"op": ">",  "value": 0.55, "label": "Win Rate"},
    "avg_edge":     {"op": ">",  "value": 0.08, "label": "Avg Edge @ Entry"},
    "max_drawdown": {"op": "<",  "value": 0.30, "label": "Max Drawdown"},
    "sample":       {"op": ">=", "value": 50,   "label": "Sample Size"},
}


def _load_json(p, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


def tracking_started_at():
    """First time tracking ran — written once, then stable across restarts."""
    meta = _load_json(META_FILE, {}) or {}
    if not meta.get("started_at"):
        meta["started_at"] = datetime.now(timezone.utc).isoformat()
        META_FILE.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta["started_at"]


def load_state():
    return _load_json(STATE_FILE, {
        "balance": 0.0, "starting_balance": 0.0, "wins": 0, "losses": 0,
        "total_trades": 0, "peak_balance": 0.0,
    })


def load_markets():
    out = []
    if MARKETS_DIR.exists():
        for f in sorted(MARKETS_DIR.glob("*.json")):
            m = _load_json(f)
            if m:
                out.append(m)
    return out


def _current_price(mkt, market_id):
    """Latest cached bid for the position's bucket (sell side)."""
    for o in mkt.get("all_outcomes", []):
        if o.get("market_id") == market_id:
            return o.get("bid", o.get("price"))
    return None


def _execution_block(mkt, pos, status):
    """The seam: everything a future real-order layer needs. No-op in paper mode."""
    return {
        "market_id":   pos.get("market_id"),
        "token_id":    None,            # resolved by the execution layer (clobTokenIds → YES)
        "side":        "BUY",           # we only buy YES on the chosen bucket
        "limit_price": pos.get("entry_price"),
        "size_usd":    pos.get("cost"),
        "status":      status,          # "paper" now; "pending"/"submitted"/"filled" later
    }


def build():
    state   = load_state()
    markets = load_markets()

    open_positions, resolved_trades = [], []

    for m in markets:
        pos = m.get("position")
        unit = "F" if m.get("unit") == "F" else "C"
        if pos and pos.get("status") == "open":
            cur = _current_price(m, pos["market_id"])
            entry = pos["entry_price"]
            unreal = round((cur - entry) * pos["shares"], 2) if cur is not None else None
            open_positions.append({
                "city":        m.get("city_name", m.get("city")),
                "date":        m.get("date"),
                "bucket":      f"{pos.get('bucket_low')}-{pos.get('bucket_high')}{unit}",
                "entry_price": entry,
                "current_price": cur,
                "shares":      pos.get("shares"),
                "cost":        pos.get("cost"),
                "unrealized":  unreal,
                "ev_at_entry": pos.get("ev"),
                "p_model":     pos.get("p"),
                "forecast_src": (pos.get("forecast_src") or "").upper(),
                "slippage":    pos.get("slippage"),     # VWAP fill − top-of-book ask
                "fill_frac":   pos.get("fill_frac"),    # share of intended size actually filled
                "execution":   _execution_block(m, pos, "paper"),
            })
        elif m.get("status") == "resolved" and m.get("pnl") is not None:
            resolved_trades.append({
                "city":     m.get("city_name", m.get("city")),
                "date":     m.get("date"),
                "bucket":   f"{pos.get('bucket_low')}-{pos.get('bucket_high')}{unit}" if pos else "-",
                "entry":    pos.get("entry_price") if pos else None,
                "exit":     pos.get("exit_price") if pos else None,
                "pnl":      m.get("pnl"),
                "outcome":  m.get("resolved_outcome"),
                "p_model":  pos.get("p") if pos else None,
                "actual_temp": m.get("actual_temp"),
            })

    metrics = compute_metrics(state, open_positions, resolved_trades)

    feed = {
        "generated_at":       datetime.now(timezone.utc).isoformat(),
        "tracking_started_at": tracking_started_at(),
        "mode":             "paper",          # <- flips to "live" when execution is wired
        "balance":          state.get("balance"),
        "starting_balance": state.get("starting_balance"),
        "return_pct":       _ret_pct(state),
        "metrics":          metrics,
        "open_positions":   open_positions,
        "resolved_trades":  sorted(resolved_trades, key=lambda x: x["date"]),
    }
    OUT_FILE.write_text(json.dumps(feed, indent=2, ensure_ascii=False), encoding="utf-8")
    return feed


def _ret_pct(state):
    start = state.get("starting_balance") or 0
    if not start:
        return 0.0
    return round((state.get("balance", 0) - start) / start * 100, 2)


def compute_metrics(state, open_positions, resolved_trades):
    wins   = sum(1 for t in resolved_trades if t["outcome"] == "win")
    losses = sum(1 for t in resolved_trades if t["outcome"] == "loss")
    n      = len(resolved_trades)
    total_pnl = round(sum(t["pnl"] for t in resolved_trades), 2)

    win_rate = (wins / n) if n else None

    # Live Brier of the bets actually placed: model prob vs realized outcome.
    bp = [(t["p_model"], 1 if t["outcome"] == "win" else 0)
          for t in resolved_trades if t.get("p_model") is not None]
    brier = round(sum((p - o) ** 2 for p, o in bp) / len(bp), 4) if bp else None

    # Average edge (EV) at entry across all positions taken (open + resolved).
    edges = [p["ev_at_entry"] for p in open_positions if p.get("ev_at_entry") is not None]
    avg_edge = round(sum(edges) / len(edges), 4) if edges else None

    # Drawdown from peak (proxy — true max DD needs an equity time series).
    peak = state.get("peak_balance") or state.get("starting_balance") or 0
    bal  = state.get("balance") or 0
    max_dd = round((peak - bal) / peak, 4) if peak else 0.0

    raw = {"brier": brier, "win_rate": win_rate, "avg_edge": avg_edge,
           "max_drawdown": max_dd, "sample": n}

    scorecard = {}
    for key, t in TARGETS.items():
        v = raw[key]
        passed = None
        if v is not None:
            passed = (v < t["value"] if t["op"] == "<"
                      else v > t["value"] if t["op"] == ">"
                      else v >= t["value"])
        scorecard[key] = {"label": t["label"], "value": v, "target": t["value"],
                          "op": t["op"], "passed": passed}

    return {
        "resolved": n, "wins": wins, "losses": losses, "total_pnl": total_pnl,
        "scorecard": scorecard,
    }


if __name__ == "__main__":
    if "--watch" in sys.argv:
        print("tracking: regenerating data/dashboard.json every 30s (Ctrl+C to stop)")
        while True:
            f = build()
            print(f"  [{f['generated_at']}] balance ${f['balance']} | "
                  f"open {len(f['open_positions'])} | resolved {f['metrics']['resolved']}")
            time.sleep(30)
    else:
        f = build()
        print(f"Wrote {OUT_FILE} — balance ${f['balance']} | "
              f"open {len(f['open_positions'])} | resolved {f['metrics']['resolved']}")
