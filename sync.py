"""Weekly sync entry point.

  python sync.py --formulary F.xlsx --pricing P.csv            # dry run: build + diff + report
  python sync.py --formulary F.xlsx --pricing P.csv --push     # also send to Shopify

State (last week's snapshot) lives in --state. It is only replaced after a
push with no failed chunks, or in a dry run with --assume-applied (demo).
"""
import argparse
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from formulary_sync import pipeline as P
from formulary_sync.report import write_report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--formulary", required=True)
    ap.add_argument("--pricing", required=True)
    ap.add_argument("--config", default="config")
    ap.add_argument("--state", default="state")
    ap.add_argument("--out", default="out")
    ap.add_argument("--label", default="")
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--assume-applied", action="store_true")
    a = ap.parse_args()

    t0 = time.perf_counter()
    out, state = Path(a.out), Path(a.state)
    out.mkdir(parents=True, exist_ok=True); state.mkdir(parents=True, exist_ok=True)
    cfg = P.load_config(a.config)
    timings = {}

    t = time.perf_counter()
    f, p = P.read_table(a.formulary), P.read_table(a.pricing)
    timings["load"] = time.perf_counter() - t

    t = time.perf_counter()
    res = P.build(f, p, cfg)
    timings["join_price_map"] = time.perf_counter() - t

    snap_path = state / "snapshot.csv.gz"
    prev = pd.read_csv(snap_path, dtype={"sku": str}, keep_default_na=False) if snap_path.exists() else None
    if prev is not None:
        for c in ("price", "cost", "weight_lb"):
            prev[c] = pd.to_numeric(prev[c])
    t = time.perf_counter()
    held = set(res.counts.pop("held"))
    d = P.diff(res.products, prev, cfg["settings"], held)
    timings["diff"] = time.perf_counter() - t

    todo = d[~d["action"].isin(["skip", "hold"])]
    t = time.perf_counter()
    jl = out / "product_set.jsonl"
    n_lines = P.write_jsonl(todo, jl, cfg["settings"]["metafield_namespace"])
    timings["jsonl"] = time.perf_counter() - t
    d[~d["action"].isin(["skip"])].drop(columns=[c for c in d if c.startswith("h_")]).to_csv(out / "changes.csv", index=False)
    pd.DataFrame([(k, s) for k, v in res.issues.items() for s in v], columns=["issue", "sku"]).to_csv(out / "issues.csv", index=False)

    push_summary = None
    if a.push:
        from formulary_sync.shopify_bulk import push
        push_summary = push(jl, out)

    applied = (a.push and not push_summary["failed_chunks"]) or (not a.push and a.assume_applied)
    if applied:
        keep = d[d["action"] != "skip"].copy()
        new_snap = res.products.copy()
        hashes = d.set_index("sku")[[c for c in d if c.startswith("h_")]]
        new_snap = new_snap.join(hashes, on="sku")
        carried = keep[keep["changed"].isin(["missing_from_file", "price_issue"])]
        if not carried.empty:
            new_snap = pd.concat([new_snap, carried[new_snap.columns]], ignore_index=True)
        if snap_path.exists():
            shutil.copy(snap_path, state / f"snapshot.prev.csv.gz")
        new_snap.to_csv(snap_path, index=False)

    timings["total"] = time.perf_counter() - t0
    meta = {
        "label": a.label, "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "formulary": Path(a.formulary).name, "pricing": Path(a.pricing).name,
        "mode": "push" if a.push else "dry-run", "snapshot_updated": bool(applied),
        "had_previous_snapshot": prev is not None, "jsonl_lines": n_lines,
        "jsonl_mb": round(jl.stat().st_size / 1e6, 2), "timings_s": {k: round(v, 2) for k, v in timings.items()},
        "counts": res.counts, "push": push_summary,
    }
    (out / "run.json").write_text(json.dumps(meta, indent=2))
    write_report(d, res, meta, cfg, out / "report.html")
    acts = d["action"].value_counts().to_dict()
    print(json.dumps({"actions": acts, "issues": {k: len(v) for k, v in res.issues.items()}, **{k: meta[k] for k in ("jsonl_lines", "jsonl_mb", "timings_s")}}, indent=1))


if __name__ == "__main__":
    main()
