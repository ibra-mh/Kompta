"""CGNC (Code Général de Normalisation Comptable) chart of accounts and shared account roots.

`cgnc_standard_accounts.json` is the single source of truth for the chart. The small
`cgnc_supplement_accounts.json` only adds roots missing from that dataset that the app
posts to. An account is valid when its code is listed, or extends a listed code
(e.g. 44110002 under 4411, 3455220 under 34552).
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STANDARD_DATASET = PROJECT_ROOT / "cgnc_standard_accounts.json"
SUPPLEMENT_DATASET = PROJECT_ROOT / "cgnc_supplement_accounts.json"

CLIENTS_ROOT = "3421"
SUPPLIERS_ROOT = "4411"
CHARGES_CLASS = "6"
PRODUCTS_CLASS = "7"

TIER_ROOT_TYPES = {CLIENTS_ROOT: "Client", SUPPLIERS_ROOT: "Fournisseur"}
TIER_ROOTS = tuple(TIER_ROOT_TYPES)


def read_dataset(path: str | Path, source: str) -> list[dict[str, Any]]:
    """Read one CGNC JSON dataset into normalized account records."""
    accounts = []
    for item in json.loads(Path(path).read_text(encoding="utf-8")):
        code = str(item["account_code"]).strip()
        if not code.isdigit() or str(item["class"]) != code[0]:
            raise ValueError(f"Invalid CGNC account record in {Path(path).name}: {item!r}")
        accounts.append({
            "code": code,
            "label": str(item["label_fr"]).strip(),
            "class": int(item["class"]),
            "status": str(item.get("status", "")),
            "source": source,
        })
    return accounts


@lru_cache(maxsize=1)
def chart_of_accounts() -> tuple[dict[str, Any], ...]:
    accounts = read_dataset(STANDARD_DATASET, "cgnc_standard") + read_dataset(SUPPLEMENT_DATASET, "cgnc_supplement")
    codes = [account["code"] for account in accounts]
    duplicates = sorted({code for code in codes if codes.count(code) > 1})
    if duplicates:
        raise ValueError(f"Duplicate CGNC account codes: {', '.join(duplicates)}")
    return tuple(sorted(accounts, key=lambda account: account["code"]))


@lru_cache(maxsize=1)
def _labels() -> dict[str, str]:
    return {account["code"]: account["label"] for account in chart_of_accounts()}


def official_label(code: str) -> str | None:
    return _labels().get(str(code).strip())


def cgnc_root(code: str) -> str | None:
    """Longest listed CGNC code that `code` equals or extends."""
    value = str(code).strip()
    if not value.isdigit():
        return None
    labels = _labels()
    for length in range(len(value), 0, -1):
        if value[:length] in labels:
            return value[:length]
    return None


def is_cgnc_account(code: str) -> bool:
    return cgnc_root(code) is not None
