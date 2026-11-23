"""Push a JSONL of ProductSet variables through Shopify's Bulk Operations API.

Flow per chunk: stagedUploadsCreate -> upload JSONL -> bulkOperationRunMutation
-> poll until COMPLETED -> download per-line results and collect userErrors.

Needs SHOPIFY_STORE (xxx.myshopify.com) and SHOPIFY_ADMIN_TOKEN with
write_products. Nothing here runs in --dry-run mode.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2025-07")
MAX_CHUNK_BYTES = 18 * 1024 * 1024  # stay under the bulk mutation file size limit

PRODUCT_SET = """mutation productSet($identifier: ProductSetIdentifiers, $input: ProductSetInput!) {
  productSet(identifier: $identifier, input: $input) {
    product { id handle status }
    userErrors { field message code }
  }
}"""


class Shopify:
    def __init__(self, store: str, token: str):
        self.url = f"https://{store}/admin/api/{API_VERSION}/graphql.json"
        self.h = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}

    def gql(self, query: str, variables: dict | None = None) -> dict:
        for attempt in range(5):
            r = requests.post(self.url, headers=self.h, json={"query": query, "variables": variables or {}}, timeout=60)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            body = r.json()
            if body.get("errors"):
                raise RuntimeError(body["errors"])
            return body["data"]
        raise RuntimeError("Shopify API kept failing after retries")

    def staged_upload(self, path: Path) -> str:
        d = self.gql("""mutation($input:[StagedUploadInput!]!){ stagedUploadsCreate(input:$input){
            stagedTargets{ url resourceUrl parameters{ name value } } userErrors{ field message } } }""",
                     {"input": [{"resource": "BULK_MUTATION_VARIABLES", "filename": path.name,
                                 "mimeType": "text/jsonl", "httpMethod": "POST"}]})["stagedUploadsCreate"]
        if d["userErrors"]:
            raise RuntimeError(d["userErrors"])
        t = d["stagedTargets"][0]
        form = {p["name"]: p["value"] for p in t["parameters"]}
        with path.open("rb") as fh:
            up = requests.post(t["url"], data=form, files={"file": (path.name, fh, "text/jsonl")}, timeout=300)
        up.raise_for_status()
        return form["key"]

    def run(self, path: Path, poll_s: int = 10) -> dict:
        key = self.staged_upload(path)
        d = self.gql("""mutation($m:String!,$p:String!){ bulkOperationRunMutation(mutation:$m, stagedUploadPath:$p){
            bulkOperation{ id status } userErrors{ field message } } }""", {"m": PRODUCT_SET, "p": key})["bulkOperationRunMutation"]
        if d["userErrors"]:
            raise RuntimeError(d["userErrors"])
        op_id = d["bulkOperation"]["id"]
        while True:
            op = self.gql("""query($id:ID!){ node(id:$id){ ... on BulkOperation {
                id status errorCode objectCount url partialDataUrl } } }""", {"id": op_id})["node"]
            if op["status"] in ("COMPLETED", "FAILED", "CANCELED", "EXPIRED"):
                return op
            time.sleep(poll_s)


def split_jsonl(path: Path) -> list[Path]:
    parts, buf, size, n = [], [], 0, 0
    for line in path.open("rb"):
        if size + len(line) > MAX_CHUNK_BYTES and buf:
            p = path.with_name(f"{path.stem}.part{n}.jsonl"); p.write_bytes(b"".join(buf)); parts.append(p)
            buf, size, n = [], 0, n + 1
        buf.append(line); size += len(line)
    if buf:
        p = path.with_name(f"{path.stem}.part{n}.jsonl"); p.write_bytes(b"".join(buf)); parts.append(p)
    return parts


def push(path: Path, out_dir: Path) -> dict:
    shop = Shopify(os.environ["SHOPIFY_STORE"], os.environ["SHOPIFY_ADMIN_TOKEN"])
    summary = {"chunks": 0, "objects": 0, "user_errors": 0, "failed_chunks": []}
    errors_path = out_dir / "shopify_user_errors.jsonl"
    with errors_path.open("w") as err:
        for part in split_jsonl(path):
            op = shop.run(part)
            summary["chunks"] += 1
            summary["objects"] += int(op.get("objectCount") or 0)
            if op["status"] != "COMPLETED":
                summary["failed_chunks"].append({"file": part.name, "status": op["status"], "error": op.get("errorCode")})
            url = op.get("url") or op.get("partialDataUrl")
            if url:
                for line in requests.get(url, timeout=300).iter_lines():
                    row = json.loads(line)
                    ue = (row.get("data") or {}).get("productSet", {}).get("userErrors") or []
                    if ue:
                        summary["user_errors"] += 1
                        err.write(json.dumps(row) + "\n")
    return summary
