# Archive — do not use

`weatherbet_legacy_pre-a2.py` is the **old** engine (pre-A2). It is kept only for
git history / reference. **Do not run or patch it.**

The canonical engine is `../weatherbet.py` (formerly `weatherbet(2).py`), which
includes the A2 sources (JMA, CMA, GFS ensemble), the consensus logic and
dynamic sigma.

Differences in this legacy file vs canonical:
- No A2 sources / no consensus — ECMWF only.
- `SIGMA_C = 1.2` hardcoded.

Note on Seoul station: Polymarket resolves the Seoul market on **Incheon (RKSI)**
per the market rules (source: Wunderground RKSI). An earlier A2 note claimed it
was Gimpo (RKSS) — that was wrong. Both files use the correct RKSI coordinates
(lat 37.4691, lon 126.4505).

If you need to port anything from here, port it INTO `../weatherbet.py`.
