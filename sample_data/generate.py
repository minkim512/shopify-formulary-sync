"""Generate realistic synthetic distributor files for two consecutive weeks.

week1: ~42,500 SKUs. week2: the same catalog with the kind of churn a
national med-surg distributor formulary shows week to week.

    python sample_data/generate.py --out sample_data
"""
import argparse
import random
from pathlib import Path

import pandas as pd

SEED = 20261123

DEPTS = {
    "Wound Care": ["Gauze & Sponges", "Bandages", "Dressings", "Tapes"],
    "Gloves": ["Nitrile Exam Gloves", "Vinyl Gloves", "Surgical Gloves"],
    "Incontinence": ["Adult Briefs", "Underpads", "Protective Underwear"],
    "Diagnostics": ["Thermometers", "Blood Pressure", "Glucose Monitoring", "Test Strips"],
    "Respiratory": ["Nebulizers", "Oxygen Accessories", "Masks"],
    "Mobility": ["Walkers", "Wheelchairs", "Canes"],
    "Personal Care": ["Skin Care", "Oral Care", "Bathing"],
    "Infection Control": ["Disinfectants", "Hand Sanitizer", "Face Masks"],
    "Nutrition": ["Oral Supplements", "Enteral Feeding"],
    "Exam Room": ["Table Paper", "Sharps Containers", "Specimen Collection"],
}
BRANDS = ["Medline", "McKesson", "Cardinal Health", "Dynarex", "3M", "Kimberly-Clark",
          "Covidien", "Hollister", "Coloplast", "Drive Medical", "Omron", "Abbott",
          "BD", "Halyard", "Smith & Nephew", "Convatec", "Sage", "PDI", "Ansell", "Graham-Field"]
UOMS = [("Box", [10, 12, 25, 50, 100, 200, 230]), ("Case", [4, 6, 10, 12, 24, 48]),
        ("Each", [1]), ("Pack", [2, 5, 10, 20]), ("Bag", [25, 50, 100])]
SIZES = {
    "Wound Care": ["2 x 2 in", "4 x 4 in", "3 in x 5 yd", "6 x 6 in"],
    "Gloves": ["Small", "Medium", "Large", "X-Large"],
    "Incontinence": ["Small", "Medium", "Large", "X-Large", "Regular"],
    "Diagnostics": ["Adult", "Standard", "50 Count", "Digital"],
    "Respiratory": ["Adult", "Pediatric", "Standard"],
    "Mobility": ["Standard", "Folding", "Bariatric", "Adjustable"],
    "Personal Care": ["4 oz", "8 oz", "16 oz"],
    "Infection Control": ["8 oz", "32 oz", "1 Gallon", "Standard"],
    "Nutrition": ["8 oz Vanilla", "8 oz Chocolate", "8 oz Strawberry", "1000 mL"],
    "Exam Room": ["Standard", "1 Quart", "2 Gallon", "18 in x 225 ft"],
}
STOCK = ["In Stock"] * 88 + ["Backordered"] * 7 + ["Out of Stock"] * 5
BULLETS = ["Latex-free construction", "Single use, sterile", "Ambidextrous fit",
           "Textured fingertips for grip", "Highly absorbent core", "Hypoallergenic adhesive",
           "Individually wrapped", "Easy-tear perforations", "Odor control technology",
           "Breathable backing", "Clinically tested", "Fragrance free",
           "Non-sterile, for general use", "Powder-free", "Reusable and washable"]


def upc(rng):
    d = [rng.randint(0, 9) for _ in range(11)]
    chk = (10 - (sum(d[0::2]) * 3 + sum(d[1::2])) % 10) % 10
    return "".join(map(str, d)) + str(chk)


def make_item(rng, n):
    l1 = rng.choice(list(DEPTS))
    l2 = rng.choice(DEPTS[l1])
    brand = rng.choice(BRANDS)
    uom, packs = rng.choice(UOMS)
    pack = rng.choice(packs)
    if l1 == "Mobility":
        uom, pack = "Each", 1
    size = rng.choice(SIZES[l1])
    title = f"{brand} {l2[:-1] if l2.endswith('s') else l2} {size}, {uom} of {pack}" if pack > 1 else f"{brand} {l2} {size}"
    bullets = rng.sample(BULLETS, rng.randint(2, 5))
    if rng.random() < 0.012:  # distributor sometimes ships FSA/HSA marketing copy
        bullets.append(rng.choice(["FSA/HSA eligible", "HSA & FSA approved item", "Eligible for FSA reimbursement"]))
    cost = round(rng.lognormvariate(2.6, 0.9), 2)
    item = {
        "Item Number": f"{rng.choice(['MDS', 'MCK', 'DYN', 'CAR', 'NON'])}{n:06d}",
        "Retail Title": title,
        "Manufacturer": brand,
        "Brand": brand,
        "Category Level 1": l1,
        "Category Level 2": l2,
        "Category Level 3": size if l1 in ("Gloves", "Incontinence") else "",
        "UPC": upc(rng),
        "GTIN": "",
        "Shipping Weight (lb)": round(rng.uniform(0.1, 35), 2),
        "Unit of Measure": uom,
        "Pack Quantity": pack,
        "Item Status": "Active",
        "Restricted": "N",
        "Stock Status": rng.choice(STOCK),
        "Image URL 1": f"https://images.salsify.example/{n:06d}_1.jpg" if rng.random() > 0.03 else "",
        "Image URL 2": f"https://images.salsify.example/{n:06d}_2.jpg" if rng.random() > 0.5 else "",
    }
    item["GTIN"] = "00" + item["UPC"]
    for i in range(5):
        item[f"Feature Bullet {i + 1}"] = bullets[i] if i < len(bullets) else ""
    return item, cost


def pricing_key(sku, rng):
    # contract-pricing exports often lose formatting: lowercase, stray spaces
    r = rng.random()
    if r < 0.15:
        return sku.lower()
    if r < 0.25:
        return f" {sku} "
    return sku


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="sample_data")
    ap.add_argument("--n", type=int, default=42500)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)

    items, costs = [], {}
    for n in range(1, a.n + 1):
        it, cost = make_item(rng, n)
        items.append(it)
        costs[it["Item Number"]] = cost
    w1 = pd.DataFrame(items)
    # list price: fixed multiplier per SKU (about 60% of SKUs carry one); moves only when cost moves
    lp_mult = {s: (rng.uniform(1.6, 2.4) if rng.random() > 0.4 else None) for s in w1["Item Number"]}

    def lp(s, cost_map):
        m = lp_mult.get(s)
        return round(cost_map[s] * m, 2) if m else ""

    p1 = pd.DataFrame({"ITEM_NO": [pricing_key(s, rng) for s in w1["Item Number"]],
                       "CONTRACT_COST": [costs[s] for s in w1["Item Number"]],
                       "LIST_PRICE": [lp(s, costs) for s in w1["Item Number"]]})

    # ---- week 2 churn ----
    w2 = w1.copy()
    c2 = dict(costs)
    idx = list(range(len(w2)))
    rng.shuffle(idx)
    cur = 0

    def take(k):
        nonlocal cur
        s = idx[cur:cur + k]
        cur += k
        return s

    dropped = take(420)                       # vanish from the file
    for i in take(120):
        w2.at[i, "Item Status"] = "Discontinued"
    for i in take(60):
        w2.at[i, "Restricted"] = "Y"           # e.g. Rx-only / state restricted
    for i in take(1500):
        w2.at[i, "Stock Status"] = rng.choice([s for s in set(STOCK) if s != w2.at[i, "Stock Status"]])
    for i in take(200):
        w2.at[i, "Retail Title"] = w2.at[i, "Retail Title"] + " - New Packaging"
    for i in take(1100):
        sku = w2.at[i, "Item Number"]
        c2[sku] = round(c2[sku] * rng.choice([0.96, 0.98, 1.02, 1.03, 1.05, 1.08]), 2)
    no_price = [w2.at[i, "Item Number"] for i in take(30)]   # in formulary, missing in pricing
    w2 = w2.drop(index=dropped).reset_index(drop=True)
    new_items = []
    for n in range(a.n + 1, a.n + 351):
        it, cost = make_item(rng, n)
        new_items.append(it)
        c2[it["Item Number"]] = cost
        lp_mult[it["Item Number"]] = rng.uniform(1.6, 2.4) if rng.random() > 0.4 else None
    w2 = pd.concat([w2, pd.DataFrame(new_items)], ignore_index=True)
    keep = [s for s in w2["Item Number"] if s not in set(no_price)]
    p2 = pd.DataFrame({"ITEM_NO": [pricing_key(s, rng) for s in keep],
                       "CONTRACT_COST": [c2[s] for s in keep],
                       "LIST_PRICE": [lp(s, c2) for s in keep]})
    # pricing-only orphans and a few bad cost cells
    orphans = pd.DataFrame({"ITEM_NO": [f"ZZZ{n:06d}" for n in range(15)], "CONTRACT_COST": 9.99, "LIST_PRICE": ""})
    p2 = pd.concat([p2, orphans], ignore_index=True)
    p2["CONTRACT_COST"] = p2["CONTRACT_COST"].astype(object)
    for i in rng.sample(range(len(p2) - 15), 8):
        p2.at[i, "CONTRACT_COST"] = 0 if rng.random() < .5 else ""

    w1.to_excel(out / "formulary_week1.xlsx", index=False)
    p1.to_csv(out / "contract_pricing_week1.csv", index=False)
    w2.to_excel(out / "formulary_week2.xlsx", index=False)
    p2.to_csv(out / "contract_pricing_week2.csv", index=False)
    print(f"week1 {len(w1):,} rows · week2 {len(w2):,} rows · pricing2 {len(p2):,} rows")


if __name__ == "__main__":
    main()
