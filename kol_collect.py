#!/usr/bin/env python3
"""KOL trade collector v3 — runs on GitHub Actions.

Opens the Kolscan SSE stream for a bounded window (default 3000s), keeps
only trades by the seed wallets (seed_kols.json, copied from the local KOL
radar), dedupes by tx signature, and appends them to data/kol_trades.jsonl.
This is a SAMPLED stream backbone (window / cadence coverage < 100%),
labelled as such; the local radar remains the high-resolution layer.
Record format is identical to the local radar's trades.jsonl.
Stdlib only. Ported from ~/workspace/kol-radar/radar.py (normalize kept
verbatim; cluster alerting intentionally dropped — attention layer is local).
"""
import argparse
import json
import os
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
TRADES_PATH = os.path.join(DATA_DIR, "kol_trades.jsonl")
SEEN_PATH = os.path.join(DATA_DIR, "kol_seen_tx.json")
STREAM_URL = "https://kolscan-api.pump.fun/api/v1/stream"


def load_seed():
    with open(os.path.join(ROOT, "seed_kols.json")) as f:
        rows = json.load(f)
    return {r["wallet"]: r for r in rows}


def normalize(evt, seed):
    if not isinstance(evt, dict):
        return None
    wallet = evt.get("wallet_address")
    if not wallet or wallet not in seed:
        return None
    is_buy = (evt.get("spl_direction") == "Buy")
    token = evt.get("out_token_address") if is_buy else evt.get("in_token_address")
    symbol = evt.get("out_token_symbol") if is_buy else evt.get("in_token_symbol")
    token_amount = evt.get("out_amount") if is_buy else evt.get("in_amount")
    if not token:
        return None
    return {
        "t": evt.get("timestamp") or time.time(),
        "wallet": wallet,
        "kol": seed[wallet].get("name"),
        "twitter": seed[wallet].get("twitter"),
        "token": token,
        "symbol": symbol,
        "is_buy": is_buy,
        "token_amount": token_amount,
        "sol": evt.get("sol_change"),
        "usd": evt.get("usd_change"),
        "dex": evt.get("dex"),
        "tx": evt.get("signature"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=3000)
    args = ap.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    seed = load_seed()
    print(f"[kol-collect] seed wallets: {len(seed)} | window {args.seconds}s", flush=True)

    seen = set()
    if os.path.exists(SEEN_PATH):
        try:
            seen = set(json.load(open(SEEN_PATH)))
        except Exception:  # noqa: BLE001
            seen = set()

    trades_f = open(TRADES_PATH, "a")
    deadline = time.time() + args.seconds
    n_events = n_trades = 0
    req = urllib.request.Request(STREAM_URL, headers={"Accept": "text/event-stream",
                                                      "User-Agent": "kol-collect-gh/0.1"})
    try:
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    buf = ""
                    while time.time() < deadline:
                        try:
                            chunk = resp.read(4096)
                        except Exception:  # noqa: BLE001
                            break
                        if not chunk:
                            break
                        buf += chunk.decode("utf-8", "ignore")
                        while "\n" in buf:
                            line, buf = buf.split("\n", 1)
                            line = line.strip()
                            if not line.startswith("data:"):
                                continue
                            payload = line[5:].strip()
                            if not payload or payload == "[DONE]":
                                continue
                            try:
                                evt = json.loads(payload)
                            except Exception:  # noqa: BLE001
                                continue
                            n_events += 1
                            tr = normalize(evt, seed)
                            if not tr:
                                continue
                            if tr["tx"] and tr["tx"] in seen:
                                continue
                            if tr["tx"]:
                                seen.add(tr["tx"])
                            n_trades += 1
                            tr["src"] = "kolscan_stream_sampled"
                            trades_f.write(json.dumps(tr) + "\n")
                            trades_f.flush()
            except Exception:  # noqa: BLE001
                if time.time() >= deadline:
                    break
                time.sleep(3)
    finally:
        json.dump(sorted(seen)[-50000:], open(SEEN_PATH, "w"))
        trades_f.close()
        print(f"[kol-collect] done: stream_events={n_events} seed_trades={n_trades}", flush=True)


if __name__ == "__main__":
    main()
