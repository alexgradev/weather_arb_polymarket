#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
execution.py — Polymarket CLOB execution layer
==============================================
PURPOSE
-------
execution.py is the component that actually PLACES A TRADE on Polymarket. Given
one decided trade — an `execution` block: which token, buy or sell, limit price,
size — it builds a CLOB limit order, signs it, and submits it to the live
Polymarket order book. This is the only place in the project where real money
can move.

execution.py  vs  tracking.py (forward tracking)
------------------------------------------------
Two opposite halves — do not confuse them:

    tracking.py (forward tracking)      execution.py (this file)
    ------------------------------      ------------------------
    READS / OBSERVES                    WRITES / ACTS
    aggregates what the paper engine    sends a real order to the
    already did into metrics            live CLOB order book
    "is the strategy any good?"         "place this specific trade"
    never sends anything anywhere       can move real USDC (live mode)
    always read-only, always safe       guarded: dry-run unless armed

Forward tracking MEASURES the strategy on paper; execution EXECUTES a chosen
trade for real. The dashboard's per-row "Execute" button is the bridge: it hands
one execution block from the read-only tracking view to this module. In paper
mode nothing is ever sent.

SAFE BY DESIGN
--------------
- Defaults to DRY-RUN: it builds and logs the exact order it *would* submit, but
  sends nothing. A real order requires BOTH live credentials AND an explicit
  confirm=True on the call. There is no path that auto-fires.
- Credentials are read from environment variables only — never hard-coded.

GOING LIVE — wallet setup (required before --live / confirm=True)
-----------------------------------------------------------------
There is no one-click path; live trading needs an on-chain wallet. Steps:

  1. pip install py-clob-client

  2. Have a Polygon wallet funded with USDC. If you already trade manually on
     polymarket.com, the wallet is funded AND the on-chain allowances are already
     set — skip to step 4.

  3. (First time only) Set the USDC / CTF allowances for the Polymarket exchange.
     The simplest way is to place one manual trade in the polymarket.com UI; that
     sets the allowances for you.

  4. Set environment variables:
        POLY_PK             wallet private key (hex). Use a DEDICATED low-balance
                            wallet, never your main one; never commit this value.
        POLY_SIG_TYPE       0 = EOA | 1 = email/magic proxy | 2 = browser proxy
        POLY_FUNDER         proxy / funder wallet address (for sig types 1 and 2)
        POLY_API_KEY        \  optional CLOB API creds. If omitted, they are
        POLY_API_SECRET      >  derived automatically from POLY_PK.
        POLY_API_PASSPHRASE /

  5. Place ONE minimal (~$1) test order first and confirm it appears in the order
     book before trusting the path with real size. The live branch follows the
     documented py-clob-client API; proxy signature_type and allowances are the
     usual failure points, so validate end-to-end with the test order.

Usage
-----
    # dry-run a single order from a JSON execution block
    python execution.py '{"token_id": "7150...", "side": "BUY", "limit_price": 0.34, "size_usd": 20}'

    # in code
    from execution import PolymarketExecutor
    ex = PolymarketExecutor(dry_run=True)
    ex.place_order(execution_block)              # logs, sends nothing
    ex.place_order(execution_block, confirm=True)  # live only if creds present
"""

import os
import sys
import json

CLOB_HOST = "https://clob.polymarket.com"
GAMMA     = "https://gamma-api.polymarket.com"
CHAIN_ID  = 137   # Polygon mainnet

try:
    import requests
except ImportError:
    requests = None


class PolymarketExecutor:
    def __init__(self, dry_run: bool = True):
        self.dry_run  = dry_run
        self.pk       = os.environ.get("POLY_PK")
        self.funder   = os.environ.get("POLY_FUNDER")
        self.sig_type = int(os.environ.get("POLY_SIG_TYPE", "0"))
        self.api_creds = {
            "key":        os.environ.get("POLY_API_KEY"),
            "secret":     os.environ.get("POLY_API_SECRET"),
            "passphrase": os.environ.get("POLY_API_PASSPHRASE"),
        }
        self._client = None
        # Live only if the signing library is importable AND a key is configured.
        self.can_go_live = bool(self.pk) and self._load_client()

    # --- client -----------------------------------------------------------
    def _load_client(self):
        """Lazily construct a py-clob-client. Returns True if available + keyed."""
        if self._client is not None:
            return True
        if not self.pk:
            return False
        try:
            from py_clob_client.client import ClobClient
        except ImportError:
            return False
        try:
            client = ClobClient(
                CLOB_HOST, key=self.pk, chain_id=CHAIN_ID,
                signature_type=self.sig_type,
                funder=self.funder or None,
            )
            # Use provided API creds, or derive them from the wallet key.
            if all(self.api_creds.values()):
                from py_clob_client.clob_types import ApiCreds
                client.set_api_creds(ApiCreds(
                    api_key=self.api_creds["key"],
                    api_secret=self.api_creds["secret"],
                    api_passphrase=self.api_creds["passphrase"],
                ))
            else:
                client.set_api_creds(client.create_or_derive_api_creds())
            self._client = client
            return True
        except Exception as e:
            print(f"  [EXEC] client init failed: {e}")
            return False

    # --- helpers ----------------------------------------------------------
    @staticmethod
    def resolve_yes_token(market_id):
        """Resolve a gamma market id to its YES clob token id."""
        if requests is None:
            return None
        try:
            d = requests.get(f"{GAMMA}/markets/{market_id}", timeout=(5, 8)).json()
            ids = json.loads(d.get("clobTokenIds", "[]"))
            return ids[0] if ids else None
        except Exception as e:
            print(f"  [EXEC] resolve token failed: {e}")
            return None

    @staticmethod
    def _normalize(execution_block):
        """Fill token_id from market_id if needed; compute share size."""
        b = dict(execution_block)
        if not b.get("token_id") and b.get("market_id"):
            b["token_id"] = PolymarketExecutor.resolve_yes_token(b["market_id"])
        price = float(b.get("limit_price") or 0)
        size_usd = float(b.get("size_usd") or 0)
        b["shares"] = round(size_usd / price, 2) if price > 0 else 0.0
        return b

    # --- the one entry point ---------------------------------------------
    def place_order(self, execution_block: dict, confirm: bool = False) -> dict:
        """
        Build a CLOB limit order from an execution block and (optionally) submit it.

        Returns a result dict with a `status`:
          "dry_run"   — built but not sent (default, or no live creds/confirm)
          "submitted" — sent to the CLOB
          "error"     — validation/submission problem
        """
        b = self._normalize(execution_block)
        token_id = b.get("token_id")
        side     = (b.get("side") or "BUY").upper()
        price    = float(b.get("limit_price") or 0)
        shares   = b.get("shares", 0)

        if not token_id or price <= 0 or shares <= 0:
            return {"status": "error", "reason": "missing token_id / price / size", "order": b}

        order = {"token_id": token_id, "side": side, "price": price, "size": shares}

        live = self.can_go_live and confirm and not self.dry_run
        if not live:
            why = ("dry_run flag" if self.dry_run else
                   "no confirm" if not confirm else
                   "no live credentials")
            print(f"  [EXEC/DRY] {side} {shares} @ {price}  token {str(token_id)[:10]}...  ({why})")
            return {"status": "dry_run", "order": order, "reason": why}

        # --- live submission (py-clob-client) ---
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY, SELL
            args = OrderArgs(token_id=token_id, price=price, size=shares,
                             side=BUY if side == "BUY" else SELL)
            signed = self._client.create_order(args)
            resp   = self._client.post_order(signed, OrderType.GTC)
            print(f"  [EXEC/LIVE] submitted {side} {shares} @ {price} -> {resp}")
            return {"status": "submitted", "order": order, "response": resp}
        except Exception as e:
            print(f"  [EXEC] submit failed: {e}")
            return {"status": "error", "reason": str(e), "order": order}


def main():
    args   = sys.argv[1:]
    live   = "--live" in args
    blocks = [a for a in args if not a.startswith("--")]
    if not blocks:
        print("usage: python execution.py '<execution-block-json>'  [--live]")
        print("example: python execution.py '{\"token_id\":\"7150...\",\"side\":\"BUY\","
              "\"limit_price\":0.34,\"size_usd\":20}'")
        return
    try:
        block = json.loads(blocks[0])
    except json.JSONDecodeError as e:
        print(f"  [EXEC] invalid JSON execution block: {e}")
        return
    ex = PolymarketExecutor(dry_run=not live)
    print(f"  live-capable: {ex.can_go_live} | mode: {'LIVE' if live else 'DRY-RUN'}")
    result = ex.place_order(block, confirm=live)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
