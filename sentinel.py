#!/usr/bin/env python3
"""Agent-mint sentinel v1 — runs on GitHub Actions.

Polls public, no-auth sources for NEW BSC tokens/projects that look like
AI-agent free mints / Agent ID claims, and appends fresh findings to
findings.jsonl (deduped via seen.json). A watcher on the Muse side polls
findings.jsonl and wakes Muse to gate + act. This script never signs,
buys, or mints anything; it only observes public data.
"""
import gzip
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
FINDINGS = os.path.join(ROOT, "findings.jsonl")
SEEN = os.path.join(ROOT, "seen.json")

UA = {"User-Agent": "agent-mint-sentinel/1.0 (+https://github.com/kira1752/agent-mint-sentinel)"}

PHRASES = [
    "agent id", "agentid", "agentic", "ai agent", "agent mint", "free mint",
    "mint agent", "binance agent", "agent claim", "claim agent",
]


def fetch_json(url, timeout=25):
    headers = dict(UA)
    headers["Accept-Encoding"] = "gzip"
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8"))
        except Exception as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise last


def looks_like_agent(text):
    t = (text or "").lower()
    if any(p in t for p in PHRASES):
        return True
    # bare "agent" only counts with an action word nearby
    return "agent" in t and any(w in t for w in ("mint", "claim", "free", " ai ", "ai-"))


def load_seen():
    try:
        with open(SEEN, "r", encoding="utf-8") as f:
            return set(json.load(f).get("ids", []))
    except Exception:
        return set()


def save_seen(seen):
    ids = sorted(seen)[-2000:]
    with open(SEEN, "w", encoding="utf-8") as f:
        json.dump({"ids": ids}, f, indent=1)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def from_dexscreener_profiles(out):
    try:
        data = fetch_json("https://api.dexscreener.com/token-profiles/latest/v1")
    except Exception as e:
        print("dexscreener profiles failed:", e)
        return
    if not isinstance(data, list):
        return
    for it in data:
        if (it.get("chainId") or "") != "bsc":
            continue
        desc = it.get("description") or ""
        if not looks_like_agent(desc):
            continue
        token = (it.get("tokenAddress") or "").lower()
        if not token:
            continue
        links = {}
        for ln in it.get("links") or []:
            if isinstance(ln, dict) and ln.get("url"):
                links[ln.get("label") or "link"] = ln["url"]
        out.append({
            "id": "dexprof:" + token,
            "ts_utc": now_iso(),
            "source": "dexscreener_profiles",
            "chain": "bsc",
            "token": token,
            "name": "",
            "url": it.get("url") or "",
            "links": links,
            "blurb": desc[:400],
        })


def from_geckoterminal_new_pools(out):
    try:
        data = fetch_json("https://api.geckoterminal.com/api/v2/networks/bsc/new_pools?page=1")
    except Exception as e:
        print("geckoterminal new_pools failed:", e)
        return
    for pool in (data.get("data") or []):
        attrs = pool.get("attributes") or {}
        name = attrs.get("name") or ""
        base = name.split("/")[0].strip()
        if not looks_like_agent(base) and "agent" not in base.lower():
            continue
        out.append({
            "id": "geckopool:" + (pool.get("id") or attrs.get("address") or name),
            "ts_utc": now_iso(),
            "source": "geckoterminal_new_pools",
            "chain": "bsc",
            "token": "",
            "name": name,
            "pool": attrs.get("address") or "",
            "pool_created_at": attrs.get("pool_created_at") or "",
            "url": "https://www.geckoterminal.com/bsc/pools/" + (attrs.get("address") or ""),
            "links": {},
            "blurb": ("reserve $" + str(attrs.get("reserve_in_usd") or "?")),
        })


def main():
    seen = load_seen()
    candidates = []
    from_dexscreener_profiles(candidates)
    from_geckoterminal_new_pools(candidates)
    fresh = []
    for c in candidates:
        if c["id"] in seen:
            continue
        seen.add(c["id"])
        fresh.append(c)
    if fresh:
        with open(FINDINGS, "a", encoding="utf-8") as f:
            for c in fresh:
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
        print(f"NEW findings: {len(fresh)}")
        for c in fresh:
            print(" -", c["source"], c.get("token") or c.get("name"), c.get("url"))
    else:
        print("no new findings")
    save_seen(seen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
