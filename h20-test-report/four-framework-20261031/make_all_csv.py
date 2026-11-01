"""Merge every matrix CSV into one file: data/all.csv.

Inputs (same 30-column schema, straight from the harness's offline_summary.csv):
    gr-168.csv  sglang-168.csv  sidgr-168.csv  vllm-a.csv  vllm-b.csv (optional)
Output: all.csv = source + the original columns, sorted by (backend, batch, beam, length).
Re-run after a new run lands; it rewrites the file from scratch.
"""

import csv
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

SOURCES = [
    ("gr-168", "gr-168.csv"),
    ("sglang-168", "sglang-168.csv"),
    ("sidgr-168", "sidgr-168.csv"),
    ("vllm-a", "vllm-a.csv"),
    ("vllm-b", "vllm-b.csv"),
]

rows, header = [], None
for tag, name in SOURCES:
    path = os.path.join(DATA, name)
    if not os.path.exists(path):
        print("skip (missing):", name)
        continue
    with open(path, encoding="utf-8", newline="") as fh:
        rd = csv.DictReader(fh)
        if header is None:
            header = rd.fieldnames
        elif rd.fieldnames != header:
            raise SystemExit(f"schema mismatch in {name}")
        n = 0
        for r in rd:
            r["source"] = tag
            rows.append(r)
            n += 1
        print(f"{name}: {n} rows")

rows.sort(
    key=lambda r: (
        r["backend"],
        int(r["batch"]),
        int(r["beam"]),
        int(r["length"]),
        r["source"],
    )
)

out = os.path.join(DATA, "all.csv")
tmp = out + ".tmp"
with open(tmp, "w", encoding="utf-8", newline="") as fh:
    wr = csv.DictWriter(fh, fieldnames=["source"] + header)
    wr.writeheader()
    wr.writerows(rows)

# Excel / editors hold a deny-write handle on Windows; swap in atomically when possible.
try:
    os.replace(tmp, out)
except PermissionError:
    fallback = out + ".new"
    os.replace(tmp, fallback)
    print(f"LOCKED {out} (open in Excel?) -> wrote {fallback} instead")
    raise SystemExit(2)

print(f"wrote {out}: {len(rows)} rows")
