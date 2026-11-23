"""Weekly formulary + contract-pricing -> Shopify sync.

Steps (each is a plain function so it can be tested on its own):
  load -> normalise SKUs -> join -> price -> map to Shopify -> diff vs last
  week's snapshot -> write bulk JSONL + report.
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------- loading

def read_table(path: str | Path) -> pd.DataFrame:
    p = Path(path)
    if p.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        df = pd.read_excel(p, dtype=str)
    else:
        df = pd.read_csv(p, dtype=str, keep_default_na=False)
    return df.fillna("")


def load_config(cfg_dir: str | Path) -> dict:
    d = Path(cfg_dir)
    rules = pd.read_csv(d / "markup_rules.csv")
    rules = rules.sort_values("priority").reset_index(drop=True)
    return {
        "rules": rules,
        "settings": json.loads((d / "settings.json").read_text()),
        "map": json.loads((d / "field_map.json").read_text()),
    }

# ---------------------------------------------------------------- helpers

_ws = re.compile(r"\s+")


def norm_sku(s: str) -> str:
    """Join key: trim, collapse spaces, upper-case. Distributor and pricing
    exports rarely agree on formatting."""
    return _ws.sub("", str(s)).upper()


def to_float(v) -> float | None:
    try:
        f = float(str(v).replace(",", "").replace("$", "").strip())
        return f if math.isfinite(f) else None
    except ValueError:
        return None


def handleize(sku: str, title: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", f"{title}-{sku}".lower()).strip("-")
    return base[:240]


def strip_banned(text: str, banned: list[str]) -> tuple[str, bool]:
    """Remove FSA/HSA marketing copy. Returns (clean_text, was_changed)."""
    if not text:
        return text, False
    if any(re.search(rf"\b{re.escape(b)}\b", text, re.I) for b in banned):
        return "", True
    return text, False

# ---------------------------------------------------------------- pricing

def apply_markup(cost: float, dept: str, rules: pd.DataFrame, st: dict, list_price: float | None):
    """First matching rule by priority wins. '*' matches any department."""
    for r in rules.itertuples():
        if r.category_l1 not in ("*", dept):
            continue
        if not (r.cost_min <= cost < r.cost_max):
            continue
        raw = cost * (1 + r.markup_pct)
        floor = cost * (1 + st["min_margin_pct"])
        price = max(raw, floor)
        end = st["price_ending"]
        price = math.floor(price) + end if price - math.floor(price) <= end else math.ceil(price) + end
        if price < floor:  # rounding must never break the margin floor
            price += 1
        compare = None
        if st.get("use_list_price_as_compare_at") and list_price and list_price > price:
            compare = round(list_price, 2)
        elif r.compare_at_pct > 0:
            compare = math.ceil(price * (1 + r.compare_at_pct)) - 0.01
        return round(price, 2), compare, int(r.priority)
    raise ValueError(f"no markup rule for dept={dept} cost={cost}")

# ---------------------------------------------------------------- main build

@dataclass
class Result:
    products: pd.DataFrame                 # full mapped catalogue for this week
    issues: dict = field(default_factory=dict)
    counts: dict = field(default_factory=dict)


def build(formulary: pd.DataFrame, pricing: pd.DataFrame, cfg: dict) -> Result:
    fm, pm, st, rules = cfg["map"]["formulary"], cfg["map"]["pricing"], cfg["settings"], cfg["rules"]
    issues: dict[str, list] = {k: [] for k in (
        "no_price", "pricing_only", "bad_cost", "duplicate_sku", "banned_text_removed",
        "missing_image", "missing_upc")}

    f = formulary.copy()
    f["_sku"] = f[fm["sku"]].map(norm_sku)
    dup = f[f["_sku"].duplicated(keep="first")]
    issues["duplicate_sku"] = dup[fm["sku"]].tolist()
    f = f.drop_duplicates("_sku", keep="first")

    p = pricing.copy()
    p["_sku"] = p[pm["sku"]].map(norm_sku)
    p = p.drop_duplicates("_sku", keep="last")
    p["_cost"] = p[pm["cost"]].map(to_float)
    p["_list"] = p[pm["list_price"]].map(to_float) if pm.get("list_price") in p else None

    j = f.merge(p[["_sku", "_cost", "_list"]], on="_sku", how="left", indicator=True)
    issues["no_price"] = j.loc[j["_merge"] == "left_only", fm["sku"]].tolist()
    issues["pricing_only"] = sorted(set(p["_sku"]) - set(f["_sku"]))
    bad = j["_merge"].eq("both") & ~(j["_cost"].fillna(0) > 0)
    issues["bad_cost"] = j.loc[bad, fm["sku"]].tolist()
    j = j[j["_merge"].eq("both") & ~bad].copy()

    disc_vals = {v.lower() for v in st["discontinued_status_values"]}
    restr_vals = {v.lower() for v in st["restricted_flag_values"]}
    rows = []
    for r in j.to_dict("records"):
        sku = r["_sku"]
        dept = r[fm["category"][0]]
        price, compare, rule = apply_markup(r["_cost"], dept, rules, st, r["_list"])
        bullets, removed = [], False
        for col in fm["bullets"]:
            t, ch = strip_banned(r.get(col, ""), st["banned_phrases"])
            removed |= ch
            if t:
                bullets.append(t)
        title, ch = r[fm["title"]], False
        if any(re.search(rf"\b{b}\b", title, re.I) for b in st["banned_phrases"]):
            title = re.sub(r"\b(FSA|HSA)\b[^,;]*", "", title, flags=re.I).strip(" ,;-")
            ch = True
        if removed or ch:
            issues["banned_text_removed"].append(r[fm["sku"]])
        images = [r[c] for c in fm["images"] if r.get(c)]
        if not images:
            issues["missing_image"].append(r[fm["sku"]])
        upc = (r.get(fm["upc"]) or "").strip()
        if not upc:
            issues["missing_upc"].append(r[fm["sku"]])
        uom = r[fm["uom"]].strip().title()
        pack = int(to_float(r[fm["pack_qty"]]) or 1)
        status_raw = r[fm["status"]].strip().lower()
        restricted = r[fm["restricted"]].strip().lower() in restr_vals
        if status_raw in disc_vals:
            shop_status, reason = st["archive_as"], "discontinued"
        elif restricted:
            shop_status, reason = st["archive_as"], "restricted"
        else:
            shop_status, reason = "ACTIVE", ""
        cats = [r[c] for c in fm["category"] if r.get(c)]
        tags = [f"dept:{dept}", f"uom:{uom}"]
        if dept in st["autoship_departments"]:
            tags.append("autoship-eligible")
        rows.append({
            "sku": sku,
            "handle": handleize(sku, title),
            "title": title,
            "body_html": "<ul>" + "".join(f"<li>{html.escape(b)}</li>" for b in bullets) + "</ul>" if bullets else "",
            "vendor": r[fm["vendor"]],
            "product_type": cats[1] if len(cats) > 1 else dept,
            "category_path": " > ".join(cats),
            "tags": ",".join(tags),
            "barcode": upc or r.get(fm["gtin"], ""),
            "gtin": r.get(fm["gtin"], ""),
            "weight_lb": to_float(r[fm["weight_lb"]]) or 0.0,
            "cost": round(r["_cost"], 2),
            "price": price,
            "compare_at_price": compare,
            "margin_pct": round((price - r["_cost"]) / price, 4),
            "rule": rule,
            "uom": uom,
            "pack_qty": pack,
            "uom_label": f"{uom} of {pack}" if pack > 1 else uom,
            "stock_status": r[fm["stock_status"]],
            "images": "|".join(images),
            "status": shop_status,
            "status_reason": reason,
        })
    out = pd.DataFrame(rows)
    counts = {"formulary_rows": len(formulary), "pricing_rows": len(pricing), "built": len(out)}
    counts["held"] = sorted({norm_sku(x) for x in issues["no_price"] + issues["bad_cost"]})
    return Result(out, issues, counts)

# ---------------------------------------------------------------- delta

PRICE_COLS = ["price", "compare_at_price", "cost"]
STOCK_COLS = ["stock_status"]
CONTENT_COLS = ["title", "body_html", "vendor", "product_type", "tags", "barcode", "weight_lb",
                "uom_label", "pack_qty", "images"]


def _h(row, cols):
    return hashlib.md5("\x1f".join(str(row[c]) for c in cols).encode()).hexdigest()


def diff(cur: pd.DataFrame, prev: pd.DataFrame | None, st: dict, held: set | None = None) -> pd.DataFrame:
    """Return one row per SKU with action + what changed.

    held: SKUs still in the formulary but skipped this week (no / bad price).
    They keep last week's state in Shopify instead of being archived."""
    held = held or set()
    c = cur.copy()
    for name, cols in (("h_price", PRICE_COLS), ("h_stock", STOCK_COLS), ("h_content", CONTENT_COLS), ("h_status", ["status"])):
        c[name] = c.apply(_h, axis=1, cols=cols)
    if prev is None or prev.empty:
        c["action"] = "create"
        c["changed"] = "new"
        c["prev_price"], c["prev_stock"] = None, ""
        return c
    pv = prev.set_index("sku")
    # the handle is the upsert key in Shopify: never let a title edit change it
    known = c["sku"].isin(pv.index)
    c.loc[known, "handle"] = c.loc[known, "sku"].map(pv["handle"])
    acts, chg, old_p, old_s = [], [], [], []
    for r in c.itertuples():
        if r.sku not in pv.index:
            acts.append("create"); chg.append("new"); old_p.append(None); old_s.append("")
            continue
        o = pv.loc[r.sku]
        old_p.append(float(o["price"])); old_s.append(o["stock_status"])
        what = [k for k in ("price", "stock", "content", "status") if getattr(r, f"h_{k}") != o[f"h_{k}"]]
        if not what:
            acts.append("skip"); chg.append("")
        elif "status" in what and r.status != "ACTIVE":
            acts.append("archive"); chg.append(r.status_reason)
        else:
            acts.append("update"); chg.append("+".join(what))
    c["action"], c["changed"], c["prev_price"], c["prev_stock"] = acts, chg, old_p, old_s
    # SKUs that vanished from the distributor file
    hold = sorted((set(pv.index) & held) - set(c["sku"]))
    if hold:
        h = pv.loc[hold].reset_index()
        h["prev_price"], h["prev_stock"], h["action"], h["changed"] = h["price"], h["stock_status"], "hold", "price_issue"
        c = pd.concat([c, h], ignore_index=True)
    gone = sorted(set(pv.index) - set(c["sku"]))
    if gone and st["missing_from_file_means_discontinued"]:
        g = pv.loc[gone].reset_index()
        g = g[g["status"] == "ACTIVE"].copy()   # already archived last week -> nothing to do
        g["prev_price"], g["prev_stock"] = g["price"], g["stock_status"]
        g["status"], g["status_reason"], g["action"], g["changed"] = st["archive_as"], "missing_from_file", "archive", "missing_from_file"
        c = pd.concat([c, g], ignore_index=True)
    return c

# ---------------------------------------------------------------- Shopify payloads

def product_set_input(r: dict, ns: str) -> dict:
    """ProductSetInput for one SKU (single-variant product).
    Archive rows only carry status so content isn't rewritten."""
    if r["action"] == "archive":
        return {"identifier": {"handle": r["handle"]}, "input": {"status": r["status"]}}
    variant = {
        "optionValues": [{"optionName": "Title", "name": "Default Title"}],
        "sku": r["sku"],
        "price": f'{r["price"]:.2f}',
        "barcode": r["barcode"] or None,
        "inventoryItem": {"cost": f'{r["cost"]:.2f}', "measurement": {"weight": {"value": float(r["weight_lb"]), "unit": "POUNDS"}}},
    }
    if r["compare_at_price"] and not (isinstance(r["compare_at_price"], float) and math.isnan(r["compare_at_price"])):
        variant["compareAtPrice"] = f'{float(r["compare_at_price"]):.2f}'
    inp = {
        "handle": r["handle"],
        "title": r["title"],
        "descriptionHtml": r["body_html"],
        "vendor": r["vendor"],
        "productType": r["product_type"],
        "tags": r["tags"].split(","),
        "status": r["status"],
        "productOptions": [{"name": "Title", "values": [{"name": "Default Title"}]}],
        "variants": [variant],
        "metafields": [
            {"namespace": ns, "key": "uom", "type": "single_line_text_field", "value": r["uom_label"]},
            {"namespace": ns, "key": "pack_qty", "type": "number_integer", "value": str(int(r["pack_qty"]))},
            {"namespace": ns, "key": "stock_status", "type": "single_line_text_field", "value": r["stock_status"]},
            {"namespace": ns, "key": "category_path", "type": "single_line_text_field", "value": r["category_path"]},
        ],
    }
    if r["action"] == "create" and r["images"]:
        inp["files"] = [{"originalSource": u, "contentType": "IMAGE"} for u in r["images"].split("|")]
    return {"identifier": {"handle": r["handle"]}, "input": inp}


def write_jsonl(rows: pd.DataFrame, path: Path, ns: str) -> int:
    n = 0
    with path.open("w") as fh:
        for r in rows.to_dict("records"):
            fh.write(json.dumps(product_set_input(r, ns), ensure_ascii=False) + "\n")
            n += 1
    return n
