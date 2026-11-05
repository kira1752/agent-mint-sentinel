#!/usr/bin/env python3
"""Memecoin data collector v2 — runs on GitHub Actions.

Polls GeckoTerminal new_pools for Robinhood Chain + BSC and appends every
previously-unseen token (first pool per token, mirroring the local RH
tracker's discovery) to data/memecoins.jsonl in this repo. Batch-resolution
backbone: survives outages of the local daemons and fills calibration gaps.
Dedupe state lives in data/seen_pools.json (committed back by the workflow).
Stdlib only.
"""
import gzip
import io
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
OUT_PATH = os.path.join(DATA_DIR, "memecoins.jsonl")
SEEN_PATH = os.path.join(DATA_DIR, "seen_pools.json")
HEARTBEAT_PATH = os.path.join(DATA_DIR, "heartbeat.json")

UA = {"User-Agent": "agent-mint-sentinel-collector/0.1", "Accept": "application/json",
      "Accept-Encoding": "gzip"}
PAGES = 3  # new_pools pages per network per run

# base tokens that are the chain's majors/stables, never the meme side
SKIP = {
    "robinhood": {
        "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        "0x0bd7d308f8e1639fab988df18a8011f41eacad73",  # WETH
        "0x5fc5360d0400a0fd4f2af552add042d71697816c",
        "0x1f7d6fb3f13ca7de07335a47635f4935c244e3a8",
    },
    "bsc": {
        "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",  # WBNB
        "0x55d398326f99059ff775485246999027b3197955",  # USDT
        "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",  # USDC
        "0xe9e7cea3dedca5984780bafc599bd69add087d56",  # BUSD
        "0x2170ed0880ac9a755fd29b2688956bd959f933f8",  # ETH
    },
}


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_json(url, tries=2):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=25) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                return json.loads(raw.decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001
            last = e
            if i + 1 < tries:
                time.sleep(3)
    print(f"fetch failed {url}: {last}", file=sys.stderr)
    return None


def pool_candidate(network, pool, token_names):
    a = pool.get("attributes") or {}
    rel = pool.get("relationships") or {}
    base = ((rel.get("base_token") or {}).get("data") or {}).get("id")
    if not base:
        return None
    token = base.split("_", 1)[-1].lower()
    if token in SKIP.get(network, set()):
        return None
    full_name = token_names.get(token, "")
    if network == "robinhood" and ("robinhood token" in full_name.lower()
                                   or "tokenized stock" in full_name.lower()):
        return None
    price = float(a.get("base_token_price_usd") or 0)
    if price <= 0:
        return None
    pair_name = a.get("name") or ""
    tx = (a.get("transactions") or {}).get("h24") or {}
    return {
        "network": network,
        "token": token,
        "pool": (a.get("address") or "").lower(),
        "dex": ((rel.get("dex") or {}).get("data") or {}).get("id"),
        "symbol": pair_name.split("/")[0].strip(),
        "token_name": full_name,
        "pool_created_at": a.get("pool_created_at"),
        "price_usd": price,
        "reserve_usd": float(a.get("reserve_in_usd") or 0),
        "fdv_usd": float(a.get("fdv_usd") or 0),
        "volume_h24": float((a.get("volume_usd") or {}).get("h24") or 0),
        "txns_h24": {"buys": tx.get("buys", 0), "sells": tx.get("sells", 0),
                     "buyers": tx.get("buyers", 0), "sellers": tx.get("sellers", 0)},
    }


def collect_network(network, seen):
    added = []
    for page in range(1, PAGES + 1):
        j = fetch_json(f"https://api.geckoterminal.com/api/v2/networks/{network}/new_pools?page={page}")
        if not j or not isinstance(j.get("data"), list):
            continue
        token_names = {}
        for inc in (j.get("included") or []):
            if inc.get("type") == "token" and inc.get("id"):
                token_names[inc["id"].split("_", 1)[-1].lower()] = (inc.get("attributes") or {}).get("name", "")
        for pool in j["data"]:
            c = pool_candidate(network, pool, token_names)
            if not c or not c["pool"]:
                continue
            key = f"{network}:{c['token']}"
            if key in seen:
                continue
            seen.add(key)
            c["seen_at"] = now_iso()
            c["src"] = "geckoterminal_new_pools"
            added.append(c)
    return added


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    seen = set()
    if os.path.exists(SEEN_PATH):
        try:
            seen = set(json.load(open(SEEN_PATH)))
        except Exception:  # noqa: BLE001
            seen = set()
    all_new = []
    per_network = {}
    for network in ("robinhood", "bsc"):
        added = collect_network(network, seen)
        per_network[network] = len(added)
        all_new.extend(added)
    if all_new:
        with open(OUT_PATH, "a") as f:
            for rec in all_new:
                f.write(json.dumps(rec) + "\n")
    with open(SEEN_PATH, "w") as f:
        json.dump(sorted(seen), f)
    with open(HEARTBEAT_PATH, "w") as f:
        json.dump({"last_run_utc": now_iso(), "new_this_run": per_network,
                   "total_seen": len(seen), "cadence_min": 30,
                   "resolution": "batch_poll"}, f, indent=1)
    print(f"new pools: {per_network} (total seen {len(seen)})")


if __name__ == "__main__":
    main()
