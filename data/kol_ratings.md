# Rating kualitas KOL — snapshot 2026-11-05 14:18 WITA

Dasar data: 0 trade stream, 0 KOL aktif teramati, 0 roundtrip tertutup, 0 outcome harga terlacak.
Skor 0–100 (menyusut ke 50 bila bukti sedikit). Tier 'data kurang' = bukti < 5, JANGAN dipakai.

| KOL | Skor | Tier | Bukti | Roundtrip | Win | PnL (SOL) | Tahan median | Jual-pertama | Flip | Outcome: peak/akhir | Dump |
|---|---|---|---|---|---|---|---|---|---|---|---|

Metode v0.1: skor = PnL roundtrip (40) + outcome harga pasca-beli (40) + perilaku anti-flip (20), lalu disusutkan Bayesian ke 50 dengan bobot bukti/(bukti+3). Outcome memakai snapshot DexScreener untuk beli berumur ≥1 jam (bukan checkpoint terjadwal penuh — disempurnakan sambil data menumpuk).
