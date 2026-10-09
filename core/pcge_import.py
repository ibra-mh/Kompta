"""Source-backed import of the general-business chart from the official CGNC dataset.

`cgnc_standard_accounts.json` is the source of
labels and codes. It covers classes 1-8; classes 0 and 9 are special/analytical
accounts and stay outside this import. Every dataset entry is a standard account.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .cgnc import STANDARD_DATASET, chart_of_accounts, read_dataset


def resolve_pcge_source(path: str | Path | None = None) -> Path:
    source = Path(path or STANDARD_DATASET).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"CGNC source dataset not found: {source}")
    return source


def extract_pcge_general_accounts(path: str | Path | None = None) -> list[dict[str, Any]]:
    resolve_pcge_source(path)
    records = read_dataset(path, "cgnc_standard") if path else list(chart_of_accounts())
    accounts = [
        {"code": record["code"], "label": record["label"], "class": record["class"], "source": record["source"]}
        for record in records
    ]
    return sorted(accounts, key=lambda item: (item["class"], item["code"]))


def account_parent(code: str, codes: set[str]) -> str | None:
    parents = [candidate for candidate in codes if candidate != code and code.startswith(candidate)]
    return max(parents, key=len) if parents else None


def preview_import(existing: list[dict[str, Any]], source_path: str | Path | None = None) -> dict[str, Any]:
    source_accounts = extract_pcge_general_accounts(source_path)
    existing_by_code = {str(item.get("code", "")).strip(): item for item in existing if item.get("code")}
    source_codes = {item["code"] for item in source_accounts}
    added, present = [], []
    for account in source_accounts:
        current = existing_by_code.get(account["code"])
        if current:
            present.append({"code": account["code"], "sourceLabel": account["label"], "currentLabel": current.get("label") or current.get("libelle", "")})
        else:
            added.append({**account, "parent": account_parent(account["code"], source_codes)})
    return {
        "source": str(resolve_pcge_source(source_path)),
        "scope": "general_business_classes_1_to_8",
        "sourceCount": len(source_accounts),
        "added": added,
        "alreadyPresent": present,
        "excludedClasses": [0, 9],
    }
