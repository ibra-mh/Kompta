"""Repair cgnc_standard_accounts.json against the verified PCGE extraction.

Fixes OCR artifacts and merged entries by taking labels from
tools/pcge_audit/data/official_plan_cgnc.json (same PDF, manually verified),
restores accounts the OCR pass dropped, corrects the misread code 14525 -> 44525,
applies explicit label overrides and marks every account as standard.
Idempotent: running it twice changes nothing.

    python tools/pcge_audit/scripts/clean_cgnc_dataset.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DATASET = ROOT / "cgnc_standard_accounts.json"
VERIFIED_PLAN = ROOT / "tools" / "pcge_audit" / "data" / "official_plan_cgnc.json"

CODE_FIXES = {"14525": "44525"}
LABEL_OVERRIDES = {
    "3455": "État – TVA récupérable",
    "4453": "État – impôts sur les résultats",
    "4455": "État – TVA facturée",
    "4456": "État, TVA due",
}
ARTIFACT = re.compile(r"\||\]|\{|‘|“|”|\*|\s\d{4,5}\.\s|(?<=\w)- (?=\w)|\s[kjL°É]$|\s[kjL°]\s")


def main() -> None:
    dataset = json.loads(DATASET.read_text(encoding="utf-8"))
    verified = {item["code"]: item for item in json.loads(VERIFIED_PLAN.read_text(encoding="utf-8"))}
    pdf_page_for_printed = {item["page_printed"]: item["page_pdf"] for item in dataset}

    by_code: dict[str, dict] = {}
    for item in dataset:
        code = CODE_FIXES.get(item["account_code"], item["account_code"])
        by_code[code] = {**item, "account_code": code, "class": int(code[0]), "status": "standard"}

    relabeled = added = 0
    for code, item in by_code.items():
        source = verified.get(code)
        label = LABEL_OVERRIDES.get(code) or (source["label"] if source else item["label_fr"])
        if item["label_fr"] != label:
            item["label_fr"] = label
            relabeled += 1
    for code, source in verified.items():
        if code not in by_code:
            by_code[code] = {
                "account_code": code,
                "label_fr": LABEL_OVERRIDES.get(code, source["label"]),
                "class": source["classe"],
                "page_pdf": pdf_page_for_printed[source["printed_page"]],
                "page_printed": source["printed_page"],
                "status": "standard",
            }
            added += 1

    accounts = sorted(by_code.values(), key=lambda item: (item["page_printed"], item["account_code"]))
    problems = [a["account_code"] for a in accounts if ARTIFACT.search(a["label_fr"]) or str(a["class"]) != a["account_code"][0]]
    orphans = sorted({a["account_code"] for a in accounts if len(a["account_code"]) > 4 and a["account_code"][:4] not in by_code})
    if problems or orphans:
        raise SystemExit(f"Unresolved entries: artifacts/class {problems}, missing 4-digit parents {orphans}")

    DATASET.write_text(json.dumps(accounts, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(accounts)} accounts written: {relabeled} relabeled, {added} restored, codes fixed {CODE_FIXES}")


if __name__ == "__main__":
    main()
