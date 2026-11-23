"""One-page HTML run report the store owner can read after every weekly sync."""
from __future__ import annotations

import html
from pathlib import Path

import pandas as pd

ISSUE_TEXT = {
    "no_price": ("In formulary, no contract price", "Not repriced; stays at last week's Shopify price. New SKUs wait."),
    "pricing_only": ("In pricing file, not in formulary", "Ignored; usually items the distributor dropped."),
    "bad_cost": ("Contract cost is 0 or blank", "Not repriced, so nothing sells below cost."),
    "duplicate_sku": ("Duplicate SKU rows in formulary", "First row kept."),
    "banned_text_removed": ("FSA/HSA wording found in title or bullets", "Wording removed before publishing."),
    "missing_image": ("No image URL", "Published; flagged for the photo backlog."),
    "missing_upc": ("No UPC", "Published; GTIN used where present."),
}

CSS = """
*{box-sizing:border-box}body{margin:0;background:#F2F5F8;font:14px/1.55 Inter,-apple-system,'Segoe UI',sans-serif;color:#2F3640}
.wrap{max-width:1080px;margin:0 auto;padding:28px 24px 48px}
.top{background:#0F1C2E;color:#fff;padding:22px 26px;border-bottom:4px solid #1F8FC4}
.top .k{font:600 11px Inter,sans-serif;letter-spacing:.16em;text-transform:uppercase;color:#5BB8E3}
.top h1{font:600 26px/1.2 Inter,sans-serif;margin:6px 0 6px}.top p{margin:0;color:#BCCADA}
.meta{display:grid;grid-template-columns:repeat(4,1fr);gap:3px;margin:12px 0 16px}
.meta div{background:#E6EDF4;padding:9px 12px;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:#2E4A6B}
.meta b{display:block;font-size:13.5px;letter-spacing:0;text-transform:none;color:#0F1C2E;margin-top:2px;word-break:break-all}
.figs{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:0 0 20px}
.fig{background:#fff;padding:14px 16px;border:1px solid #D9DFE6;border-top:3px solid #1F8FC4}
.fig .n{font:600 28px/1.05 Inter,sans-serif;color:#0F1C2E;font-variant-numeric:tabular-nums}
.fig .l{font-size:12px;color:#66707D;margin-top:6px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.card{background:#fff;border:1px solid #D9DFE6;padding:16px 18px;margin-bottom:16px}
.sec{font:600 11px Inter,sans-serif;letter-spacing:.16em;text-transform:uppercase;color:#1F8FC4}
h2{font:600 18px/1.3 Inter,sans-serif;color:#0F1C2E;margin:2px 0 12px}
table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
th{font:600 10.5px Inter,sans-serif;letter-spacing:.08em;text-transform:uppercase;background:#0F1C2E;color:#fff;text-align:left;padding:7px 9px}
td{padding:7px 9px;border-bottom:1px solid #D9DFE6;vertical-align:top}tbody tr:nth-child(even) td{background:#F2F5F8}
.num{text-align:right}.ok{color:#157A6E;font-weight:600}.mono{font-family:'DejaVu Sans Mono',Menlo,monospace;font-size:12px}
.muted{color:#66707D;font-size:12px}.bar{height:8px;background:#E6EDF4}.bar i{display:block;height:8px;background:#1F8FC4}
.tw{overflow-x:auto}.grid>*{min-width:0}
@media(max-width:760px){.meta,.figs{grid-template-columns:1fr 1fr}.grid{grid-template-columns:1fr}.top h1{font-size:21px}}
.note{background:#E6F2F9;border-left:4px solid #1F8FC4;padding:12px 16px;margin:0 0 16px}
"""


def _fmt(n):
    return f"{n:,}"


def write_report(d: pd.DataFrame, res, meta: dict, cfg: dict, path: Path):
    acts = d["action"].value_counts()
    total = len(res.products)
    writes = int((~d["action"].isin(["skip", "hold"])).sum())
    upd = d[d["action"] == "update"]
    kinds = {}
    for v in upd["changed"]:
        for k in v.split("+"):
            kinds[k] = kinds.get(k, 0) + 1
    arch = d[d["action"] == "archive"]["changed"].value_counts().to_dict()

    priced = upd[upd["changed"].str.contains("price") & upd["prev_price"].notna()].copy()
    if not priced.empty:
        priced["pct"] = (priced["price"] / priced["prev_price"].astype(float) - 1)
        up, down = int((priced["pct"] > 0).sum()), int((priced["pct"] < 0).sum())
        med = priced["pct"].abs().median()
    else:
        up = down = 0; med = 0
    floor = cfg["settings"]["min_margin_pct"]
    below = int((res.products["margin_pct"] < floor - 1e-9).sum())
    savings = int(res.products["compare_at_price"].notna().sum())

    first = not meta["had_previous_snapshot"]
    lead = ("First load: every SKU is created once. From next week only the differences are sent." if first else
            f"{_fmt(writes)} of {_fmt(total)} SKUs changed this week, so only those are sent to Shopify "
            f"({writes / max(total, 1):.1%} of the catalogue).")

    rows_act = [("Create (new SKUs)", int(acts.get("create", 0))),
                ("Update: price or cost", kinds.get("price", 0)), ("Update: stock status", kinds.get("stock", 0)),
                ("Update: content (title, bullets, images…)", kinds.get("content", 0)),
                ("Archive: discontinued", arch.get("discontinued", 0)), ("Archive: restricted", arch.get("restricted", 0)),
                ("Archive: no longer in distributor file", arch.get("missing_from_file", 0)),
                ("Held at last week's price (price problem this week)", int(acts.get("hold", 0))),
                ("No change (not sent)", int(acts.get("skip", 0)))]
    act_html = "".join(f"<tr><td>{html.escape(k)}</td><td class='num'>{_fmt(v)}</td></tr>" for k, v in rows_act)

    iss_html = ""
    for k, (label, what) in ISSUE_TEXT.items():
        v = res.issues.get(k, [])
        ex = ", ".join(html.escape(str(s)) for s in v[:4]) + (" …" if len(v) > 4 else "")
        iss_html += f"<tr><td>{label}<div class='muted'>{what}</div></td><td class='num'>{_fmt(len(v))}</td><td class='mono'>{ex or '—'}</td></tr>"

    ru = res.products.groupby("rule").agg(n=("sku", "size"), m=("margin_pct", "mean")).reset_index()
    rules = cfg["rules"].set_index("priority")
    rule_html = "".join(
        f"<tr><td>{int(r.rule)}</td><td>{html.escape(str(rules.loc[r.rule,'category_l1']))}</td>"
        f"<td class='num'>${rules.loc[r.rule,'cost_min']:,.0f}–{'∞' if rules.loc[r.rule,'cost_max']>=999999 else f'${rules.loc[r.rule,'cost_max']:,.0f}'}</td>"
        f"<td class='num'>{rules.loc[r.rule,'markup_pct']:.0%}</td><td class='num'>{_fmt(int(r.n))}</td><td class='num'>{r.m:.1%}</td></tr>"
        for r in ru.itertuples())

    sample = pd.concat([
        upd[upd["changed"].str.contains("price") & (upd["price"] != upd["prev_price"].astype(float))].head(4),
        upd[upd["changed"].str.contains("stock")].head(3),
        d[d["action"] == "archive"].head(3),
        d[d["action"] == "create"].head(2)])
    samp_html = ""
    for r in sample.to_dict("records"):
        old = f"${r['prev_price']:,.2f}" if r.get("prev_price") == r.get("prev_price") and r.get("prev_price") is not None else "—"
        chg = r["changed"] or r["action"]
        detail = f"{old} → ${r['price']:,.2f}" if "price" in chg else (
            f"{html.escape(str(r.get('prev_stock','')))} → {html.escape(str(r['stock_status']))}" if "stock" in chg else
            ("status → ARCHIVED" if r["action"] == "archive" else f"new · ${r['price']:,.2f}"))
        samp_html += (f"<tr><td class='mono'>{html.escape(r['sku'])}</td><td>{html.escape(str(r['title'])[:60])}</td>"
                      f"<td>{html.escape(r['action'])}: {html.escape(chg)}</td><td class='num'>{detail}</td></tr>")

    tm = meta["timings_s"]
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Weekly formulary sync — {html.escape(meta['label'] or 'run report')}</title><style>{CSS}</style></head><body>
<div class="top"><div class="wrap" style="padding:0"><div class="k">Weekly formulary sync · run report{(' · ' + html.escape(meta['label'])) if meta['label'] else ''}</div>
<h1>{_fmt(total)} SKUs priced and mapped in {tm['total']:.0f} seconds</h1><p>{lead}</p></div></div>
<div class="wrap">
<div class="meta"><div>Formulary<b>{html.escape(meta['formulary'])}</b></div><div>Contract pricing<b>{html.escape(meta['pricing'])}</b></div>
<div>Mode<b>{meta['mode']}{' · snapshot updated' if meta['snapshot_updated'] else ''}</b></div><div>Run (UTC)<b>{meta['run_at'].replace('T',' ')[:16]}</b></div></div>
<div class="figs">
<div class="fig"><div class="n">{_fmt(meta['counts']['formulary_rows'])}</div><div class="l">rows in distributor formulary</div></div>
<div class="fig"><div class="n">{_fmt(writes)}</div><div class="l">SKUs sent to Shopify this run · {meta['jsonl_mb']} MB bulk file</div></div>
<div class="fig"><div class="n">{below}</div><div class="l">SKUs below the {floor:.0%} margin floor</div></div>
<div class="fig"><div class="n">{_fmt(savings)}</div><div class="l">SKUs with a savings badge (distributor list price above our price)</div></div></div>
<div class="grid">
<div class="card"><div class="sec">What changes in Shopify</div><h2>Actions this run</h2><div class="tw"><table><thead><tr><th>Action</th><th class="num">SKUs</th></tr></thead><tbody>{act_html}</tbody></table></div>
{'' if first else f"<p class='muted' style='margin:10px 0 0'>Price moves: {up:,} up · {down:,} down · median change {med:.1%}.</p>"}</div>
<div class="card"><div class="sec">Pricing</div><h2>Markup rules applied</h2><div class="tw"><table><thead><tr><th>Rule</th><th>Dept</th><th class="num">Cost band</th><th class="num">Markup</th><th class="num">SKUs</th><th class="num">Avg margin</th></tr></thead><tbody>{rule_html}</tbody></table></div>
<p class="muted" style="margin:10px 0 0">Rules live in <span class="mono">config/markup_rules.csv</span>; edit in Excel, next run picks them up. Prices end in .99 and never drop below the margin floor after rounding. Compare-at uses the distributor list price only; no invented "was" prices.</p></div></div>
<div class="card"><div class="sec">Data quality</div><h2>What the pipeline caught before it reached the store</h2>
<div class="tw"><table><thead><tr><th>Check</th><th class="num">SKUs</th><th>Examples</th></tr></thead><tbody>{iss_html}</tbody></table></div></div>
<div class="card"><div class="sec">Sample</div><h2>Examples from this run</h2><div class="tw"><table><thead><tr><th>SKU</th><th>Title</th><th>Action</th><th class="num">Detail</th></tr></thead><tbody>{samp_html}</tbody></table></div></div>
<p class="muted">Timings: load {tm['load']}s · join, price, map {tm['join_price_map']}s · diff {tm['diff']}s · bulk file {tm['jsonl']}s. Files: changes.csv, issues.csv, product_set.jsonl, run.json.</p>
</div></body></html>"""
    path.write_text(doc)
