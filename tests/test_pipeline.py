import math
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from formulary_sync import pipeline as P  # noqa: E402

CFG = P.load_config(ROOT / "config")


def test_sku_normalisation_joins_messy_keys():
    assert P.norm_sku(" mds000123 ") == P.norm_sku("MDS000123") == "MDS000123"


def test_markup_rounds_to_99_and_respects_floor():
    st, rules = CFG["settings"], CFG["rules"]
    for cost in (0.37, 4.2, 9.99, 19.5, 49.99, 120, 830.4):
        for dept in ("Gloves", "Mobility", "Wound Care"):
            price, compare, _ = P.apply_markup(cost, dept, rules, st, None)
            assert math.isclose(price * 100 % 100, 99, abs_tol=0.01)
            assert price >= cost * (1 + st["min_margin_pct"])
            if compare:
                assert compare > price


def test_list_price_used_as_compare_at_only_when_higher():
    st, rules = CFG["settings"], CFG["rules"]
    p, c, _ = P.apply_markup(10, "Wound Care", rules, st, 30.0)
    assert c == 30.0
    p, c, _ = P.apply_markup(10, "Wound Care", rules, st, 5.0)
    assert c is None  # no invented "was" price when the real list price is lower


def test_fsa_hsa_copy_is_removed():
    assert P.strip_banned("FSA/HSA eligible", ["FSA", "HSA"]) == ("", True)
    assert P.strip_banned("Latex-free", ["FSA", "HSA"]) == ("Latex-free", False)


def _mini():
    f = pd.DataFrame([{
        "Item Number": s, "Retail Title": f"Glove {s}", "Manufacturer": "Acme", "Brand": "Acme",
        "Category Level 1": "Gloves", "Category Level 2": "Nitrile Exam Gloves", "Category Level 3": "",
        "UPC": "012345678905", "GTIN": "", "Shipping Weight (lb)": "1.2", "Unit of Measure": "box",
        "Pack Quantity": "100", "Item Status": st, "Restricted": rs, "Stock Status": "In Stock",
        "Image URL 1": "https://x/1.jpg", "Image URL 2": "", **{f"Feature Bullet {i}": "" for i in range(1, 6)}}
        for s, st, rs in (("A1", "Active", "N"), ("A2", "Discontinued", "N"), ("A3", "Active", "Y"), ("A4", "Active", "N"))])
    p = pd.DataFrame({"ITEM_NO": ["a1 ", "A2", "A3", "ZZ9"], "CONTRACT_COST": ["8", "8", "8", "1"], "LIST_PRICE": ["", "", "", ""]})
    return f, p


def test_build_flags_and_statuses():
    f, p = _mini()
    r = P.build(f, p, CFG)
    by = r.products.set_index("sku")
    assert by.loc["A1", "status"] == "ACTIVE" and by.loc["A1", "uom_label"] == "Box of 100"
    assert by.loc["A2", "status"] == "ARCHIVED" and by.loc["A3", "status_reason"] == "restricted"
    assert r.issues["no_price"] == ["A4"] and r.issues["pricing_only"] == ["ZZ9"]


def test_second_identical_run_sends_nothing():
    f, p = _mini()
    r = P.build(f, p, CFG)
    first = P.diff(r.products, None, CFG["settings"])
    snap = r.products.join(first.set_index("sku")[[c for c in first if c.startswith("h_")]], on="sku")
    again = P.diff(P.build(f, p, CFG).products, snap, CFG["settings"])
    assert set(again["action"]) == {"skip"}


def test_sku_missing_next_week_is_archived_but_price_problem_is_held():
    f, p = _mini()
    r = P.build(f, p, CFG)
    first = P.diff(r.products, None, CFG["settings"])
    snap = r.products.join(first.set_index("sku")[[c for c in first if c.startswith("h_")]], on="sku")
    f2 = f[f["Item Number"] != "A1"]
    p2 = p.copy(); p2.loc[p2["ITEM_NO"] == "A3", "CONTRACT_COST"] = "0"
    f2 = f2.copy(); f2.loc[f2["Item Number"] == "A3", "Restricted"] = "N"
    r2 = P.build(f2, p2, CFG)
    held = {P.norm_sku(x) for x in r2.issues["no_price"] + r2.issues["bad_cost"]}
    d = P.diff(r2.products, snap, CFG["settings"], held).set_index("sku")
    assert d.loc["A1", "action"] == "archive" and d.loc["A1", "changed"] == "missing_from_file"
    assert d.loc["A3", "action"] == "hold"


def test_payload_shape():
    f, p = _mini()
    r = P.build(f, p, CFG)
    d = P.diff(r.products, None, CFG["settings"])
    row = d[d["sku"] == "A1"].iloc[0].to_dict()
    v = P.product_set_input(row, "custom")
    assert v["identifier"]["handle"] == row["handle"]
    var = v["input"]["variants"][0]
    assert var["sku"] == "A1" and var["price"].endswith(".99")
    assert {m["key"] for m in v["input"]["metafields"]} == {"uom", "pack_qty", "stock_status", "category_path"}


def test_title_edit_keeps_handle():
    f, p = _mini()
    r = P.build(f, p, CFG)
    first = P.diff(r.products, None, CFG["settings"])
    snap = r.products.join(first.set_index("sku")[[c for c in first if c.startswith("h_")]], on="sku")
    f2 = f.copy(); f2.loc[f2["Item Number"] == "A1", "Retail Title"] = "Glove A1 - New Packaging"
    d = P.diff(P.build(f2, p, CFG).products, snap, CFG["settings"]).set_index("sku")
    assert d.loc["A1", "action"] == "update" and d.loc["A1", "changed"] == "content"
    assert d.loc["A1", "handle"] == snap.set_index("sku").loc["A1", "handle"]
