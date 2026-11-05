#!/usr/bin/env python3
"""
KOL quality rating — v0.1 (research mode, approved by Indra 2026-11-04).

Rates each seed KOL from OBSERVED behavior + outcomes, not from follower
counts or self-reported PnL. Three evidence streams:

1. Roundtrips (from trades.jsonl, FIFO per wallet+token using token_amount):
   closed roundtrips, win rate, realized PnL (SOL), median hold time,
   flip ratio (share closed < 5 min — scalpers & dump-on-followers).
2. Post-buy price outcome: for each buy, entry price = usd / token_amount.
   Every run of this script snapshots the CURRENT DexScreener price for
   buys aged >= 1h into outcomes.json and tracks peak/last return.
   dump_rate = share of tracked buys now <= -50% vs entry.
3. Behavior: buy-size consistency + flip ratio penalty.

Composite 0-100, Bayesian-shrunk toward 50 by evidence count
(same spirit as the dev-wallet tracker's prior). Evidence < 5 = tier
"data kurang" — score shown but must not be acted on.

Outputs: ratings.json + report-kol-latest.md
"""
import json, os, statistics, time, urllib.request
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
os.makedirs(DATA, exist_ok=True)
FLIP_SEC = 300
OUTCOME_MIN_AGE = 3600
OUTCOME_MAX_AGE = 48 * 3600
MIN_ENTRY_SOL = 0.05     # buys below this are dust/artifacts: never open a position
MAX_PLAUSIBLE_MULT = 20  # value-mode roundtrip with proceeds > 20x cost = unmatched, not PnL

def load_json(path, default):
    if os.path.exists(path):
        try:
            return json.load(open(path))
        except Exception:
            return default
    return default

def dex_prices(mints):
    """Current USD price per mint from DexScreener (best-liquidity Solana pair)."""
    out = {}
    mints = sorted(set(mints))
    for i in range(0, len(mints), 30):
        batch = mints[i:i + 30]
        url = "https://api.dexscreener.com/latest/dex/tokens/" + ",".join(batch)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "kol-radar-rate/0.1"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.load(r)
        except Exception:
            continue
        best = {}
        for p in data.get("pairs") or []:
            if p.get("chainId") != "solana":
                continue
            mint = (p.get("baseToken") or {}).get("address")
            liq = ((p.get("liquidity") or {}).get("usd") or 0)
            price = float(p.get("priceUsd") or 0)
            if mint and price > 0 and liq > best.get(mint, (0, 0))[0]:
                best[mint] = (liq, price)
        for mint, (_, price) in best.items():
            out[mint] = price
    return out

def main():
    seed = {r["wallet"]: r for r in load_json(os.path.join(HERE, "seed_kols.json"), [])}
    trades = []
    tpath = os.path.join(DATA, "kol_trades.jsonl")
    if os.path.exists(tpath):
        for line in open(tpath):
            line = line.strip()
            if line:
                try:
                    trades.append(json.loads(line))
                except Exception:
                    pass
    trades.sort(key=lambda t: t["t"])
    now = time.time()

    # ---------- roundtrips (per wallet+token; exact by token_amount when
    # present, otherwise SOL-value approximation). PnL is finalized at close:
    # pnl = total sell proceeds - total buy cost of the position. ----------
    open_pos = {}   # (wallet, token) -> position dict
    stats = defaultdict(lambda: {
        "buys": 0, "sells": 0, "buy_usd": [], "closed": 0, "wins": 0,
        "pnl_sol": 0.0, "holds": [], "flips": 0, "first": None, "last": None,
        "tokens": set(), "first_sell_gaps": [], "dust_buys": 0,
        "buy_sol_total": 0.0, "sell_sol_total": 0.0, "unmatched_sells": 0,
    })
    buys_for_outcome = []   # (tx, wallet, token, t, entry_price)
    for tr in trades:
        w = tr["wallet"]
        s = stats[w]
        s["first"] = tr["t"] if s["first"] is None else min(s["first"], tr["t"])
        s["last"] = tr["t"] if s["last"] is None else max(s["last"], tr["t"])
        s["tokens"].add(tr["token"])
        key = (w, tr["token"])
        amt = tr.get("token_amount")
        sol = tr.get("sol") or 0.0
        if tr["is_buy"]:
            s["buys"] += 1
            s["buy_sol_total"] += sol
            if tr.get("usd"):
                s["buy_usd"].append(tr["usd"])
            if amt and tr.get("usd"):
                buys_for_outcome.append((tr["tx"], w, tr["token"], tr["t"], tr["usd"] / amt))
            if sol < MIN_ENTRY_SOL:
                s["dust_buys"] += 1
                continue
            pos = open_pos.setdefault(key, {"amount": 0.0, "cost_total": 0.0, "cost_left": 0.0,
                                            "proceeds": 0.0, "t0": tr["t"], "exact": True,
                                            "sold_frac": 0.0, "gap_done": False})
            if amt:
                pos["amount"] += amt
            else:
                pos["exact"] = False
            pos["cost_total"] += sol
            pos["cost_left"] += sol
        else:
            s["sells"] += 1
            s["sell_sol_total"] += sol
            pos = open_pos.get(key)
            if not pos:
                s["unmatched_sells"] += 1
                continue
            if not pos["gap_done"]:
                s["first_sell_gaps"].append(tr["t"] - pos["t0"])
                pos["gap_done"] = True
            pos["proceeds"] += sol
            closed_now = False
            if pos["exact"] and amt and pos["amount"] > 0:
                close_amt = min(amt, pos["amount"])
                pos["amount"] -= close_amt
                if pos["amount"] <= max(1e-9, 0.001 * close_amt):
                    closed_now = True
            else:
                pos["exact"] = False
                frac = min(1.0, sol / pos["cost_left"]) if pos["cost_left"] > 0 else 1.0
                pos["sold_frac"] += frac
                pos["cost_left"] *= (1 - frac)
                if pos["sold_frac"] >= 0.99 or pos["cost_left"] <= 1e-9:
                    closed_now = True
            if closed_now:
                pnl = pos["proceeds"] - pos["cost_total"]
                plausible = pos["exact"] or (pos["cost_total"] >= MIN_ENTRY_SOL
                                             and pos["proceeds"] <= MAX_PLAUSIBLE_MULT * max(pos["cost_total"], 1e-9))
                if not plausible:
                    s["unmatched_sells"] += 1
                    del open_pos[key]
                    continue
                s["closed"] += 1
                s["pnl_sol"] += pnl
                if pnl > 0:
                    s["wins"] += 1
                hold = tr["t"] - pos["t0"]
                s["holds"].append(hold)
                if hold < FLIP_SEC:
                    s["flips"] += 1
                del open_pos[key]

    # ---------- outcome snapshots ----------
    outcomes = load_json(os.path.join(DATA, "kol_outcomes.json"), {})
    aged = {}
    for tx, w, token, t, entry in buys_for_outcome:
        if tx and OUTCOME_MIN_AGE <= now - t <= OUTCOME_MAX_AGE:
            aged[tx] = (w, token, t, entry)
    if aged:
        prices = dex_prices([v[1] for v in aged.values()])
        for tx, (w, token, t, entry) in aged.items():
            price = prices.get(token)
            if not price or not entry:
                continue
            rec = outcomes.setdefault(tx, {"wallet": w, "token": token, "t": t, "entry": entry, "checks": []})
            if not rec["checks"] or now - rec["checks"][-1]["t"] >= 1800:
                rec["checks"].append({"t": now, "price": price})
            rets = [c["price"] / entry - 1 for c in rec["checks"]]
            rec["peak_ret"] = max(rets)
            rec["last_ret"] = rets[-1]
        json.dump(outcomes, open(os.path.join(DATA, "kol_outcomes.json"), "w"), indent=1)
    kol_outcomes = defaultdict(list)
    for rec in outcomes.values():
        if rec.get("checks"):
            kol_outcomes[rec["wallet"]].append(rec)

    # ---------- scoring ----------
    ratings = []
    for w, s in stats.items():
        info = seed.get(w, {})
        closed = s["closed"]
        win_rate = (s["wins"] / closed) if closed else None
        flip_ratio = (s["flips"] / closed) if closed else None
        med_hold = statistics.median(s["holds"]) if s["holds"] else None
        outs = kol_outcomes.get(w, [])
        med_peak = statistics.median([o["peak_ret"] for o in outs]) if outs else None
        med_last = statistics.median([o["last_ret"] for o in outs]) if outs else None
        dump_rate = (sum(1 for o in outs if o["last_ret"] <= -0.5) / len(outs)) if outs else None

        pnl_part = 20.0
        if win_rate is not None:
            pnl_part = 40.0 * win_rate
            if s["pnl_sol"] < 0:
                pnl_part *= 0.7
        out_part = 20.0
        if med_last is not None:
            out_part = max(0.0, min(40.0, 20.0 + 40.0 * med_last))
            if dump_rate is not None:
                out_part *= (1 - 0.5 * dump_rate)
        beh_part = 10.0
        if flip_ratio is not None:
            beh_part = 20.0 * (1 - flip_ratio)
        raw = pnl_part + out_part + beh_part
        evidence = closed + len(outs)
        score = (raw * evidence + 50.0 * 3) / (evidence + 3) if evidence else 50.0
        if evidence < 5:
            tier = "data kurang"
        elif score >= 75:
            tier = "kelas atas (sementara)"
        elif score >= 55:
            tier = "menengah"
        elif score >= 40:
            tier = "lemah"
        else:
            tier = "hindari"
        sb_ratio = (s["sell_sol_total"] / s["buy_sol_total"]) if s["buy_sol_total"] > 0 else None
        flags = []
        if sb_ratio is not None and sb_ratio > 3 and s["sells"] > s["buys"]:
            flags.append("kemungkinan distributor (jual >> beli)")
        if flip_ratio is not None and flip_ratio >= 0.8 and closed >= 3:
            flags.append("scalper/flipper (sinyal tak bisa diikuti)")
        if s["buys"] and s["dust_buys"] > s["buys"] / 2:
            flags.append("dust-heavy (banyak beli $0)")
        ratings.append({
            "wallet": w, "name": info.get("name"), "twitter": info.get("twitter"),
            "flags": flags, "sell_buy_value_ratio": round(sb_ratio, 2) if sb_ratio is not None else None,
            "dust_buys": s["dust_buys"], "unmatched_sells": s["unmatched_sells"],
            "score": round(score, 1), "tier": tier, "evidence": evidence,
            "buys": s["buys"], "sells": s["sells"], "closed_roundtrips": closed,
            "win_rate": win_rate, "pnl_sol": round(s["pnl_sol"], 4),
            "median_hold_sec": med_hold, "flip_ratio": flip_ratio,
            "median_buy_usd": round(statistics.median(s["buy_usd"]), 2) if s["buy_usd"] else None,
            "median_first_sell_sec": statistics.median(s["first_sell_gaps"]) if s["first_sell_gaps"] else None,
            "outcomes": len(outs), "median_peak_ret": med_peak, "median_last_ret": med_last,
            "dump_rate": dump_rate, "distinct_tokens": len(s["tokens"]),
        })
    ratings.sort(key=lambda r: (-r["evidence"], -r["score"]))
    json.dump({"generated_at": now, "ratings": ratings},
              open(os.path.join(DATA, "kol_ratings.json"), "w"), indent=1)

    # ---------- report ----------
    def pct(x):
        return "—" if x is None else f"{x*100:.0f}%"
    def ret(x):
        return "—" if x is None else f"{x*100:+.0f}%"
    lines = [f"# Rating kualitas KOL — snapshot {time.strftime('%Y-%m-%d %H:%M', time.localtime(now))} WITA",
             "",
             f"Dasar data: {len(trades)} trade stream, {len(stats)} KOL aktif teramati, "
             f"{sum(r['closed_roundtrips'] for r in ratings)} roundtrip tertutup, "
             f"{sum(r['outcomes'] for r in ratings)} outcome harga terlacak.",
             "Skor 0–100 (menyusut ke 50 bila bukti sedikit). Tier 'data kurang' = bukti < 5, JANGAN dipakai.",
             "",
             "| KOL | Skor | Tier | Bukti | Roundtrip | Win | PnL (SOL) | Tahan median | Jual-pertama | Flip | Outcome: peak/akhir | Dump |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in ratings[:25]:
        hold = "—" if r["median_hold_sec"] is None else (f"{r['median_hold_sec']:.0f}s" if r["median_hold_sec"] < 3600 else f"{r['median_hold_sec']/3600:.1f}j")
        fsell = "—" if r["median_first_sell_sec"] is None else (f"{r['median_first_sell_sec']:.0f}s" if r["median_first_sell_sec"] < 3600 else f"{r['median_first_sell_sec']/3600:.1f}j")
        lines.append(f"| {r['name']} | {r['score']} | {r['tier']} | {r['evidence']} | {r['closed_roundtrips']} | "
                     f"{pct(r['win_rate'])} | {r['pnl_sol']} | {hold} | {fsell} | {pct(r['flip_ratio'])} | "
                     f"{ret(r['median_peak_ret'])}/{ret(r['median_last_ret'])} | {pct(r['dump_rate'])} |")
    flagged = [r for r in ratings if r["flags"]]
    if flagged:
        lines += ["", "Flag perilaku:"]
        for r in flagged:
            lines.append(f"- **{r['name']}**: {'; '.join(r['flags'])} (rasio nilai jual/beli {r['sell_buy_value_ratio']}, dust {r['dust_buys']}, jual tak berpasangan {r['unmatched_sells']})")
    lines += ["", "Metode v0.1: skor = PnL roundtrip (40) + outcome harga pasca-beli (40) + perilaku anti-flip (20), "
              "lalu disusutkan Bayesian ke 50 dengan bobot bukti/(bukti+3). Outcome memakai snapshot DexScreener "
              "untuk beli berumur ≥1 jam (bukan checkpoint terjadwal penuh — disempurnakan sambil data menumpuk)."]
    open(os.path.join(DATA, "kol_ratings.md"), "w").write("\n".join(lines) + "\n")
    print(f"[rate] trades={len(trades)} kols_active={len(stats)} rated={len(ratings)} "
          f"with_evidence>=5: {sum(1 for r in ratings if r['evidence'] >= 5)} outcomes_tracked={len(outcomes)}")
    for r in ratings[:8]:
        print(f"  {r['name']}: score={r['score']} tier={r['tier']} ev={r['evidence']} closed={r['closed_roundtrips']} "
              f"win={pct(r['win_rate'])} pnl={r['pnl_sol']} SOL flip={pct(r['flip_ratio'])}")

if __name__ == "__main__":
    main()
