"""CGNC (Code Général de Normalisation Comptable) chart of accounts and shared account roots.

`cgnc_standard_accounts.json` is the single source of truth for the chart. An account
code is valid when it has 4 to 8 digits and its first 4 digits are a listed CGNC
account (e.g. 44110002 under 4411, 3455220 under 3455).
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STANDARD_DATASET = PROJECT_ROOT / "cgnc_standard_accounts.json"
ACCOUNT_CODE = re.compile(r"^\d{4,8}$")

CLIENTS_ROOT = "3421"
SUPPLIERS_ROOT = "4411"
CHARGES_CLASS = "6"
PRODUCTS_CLASS = "7"

TIER_ROOT_TYPES = {CLIENTS_ROOT: "Client", SUPPLIERS_ROOT: "Fournisseur"}
TIER_ROOTS = tuple(TIER_ROOT_TYPES)


def read_dataset(path: str | Path, source: str) -> list[dict[str, Any]]:
    """Read a CGNC JSON dataset into normalized account records, rejecting malformed entries."""
    accounts = []
    for item in json.loads(Path(path).read_text(encoding="utf-8")):
        code = str(item["account_code"]).strip()
        if not ACCOUNT_CODE.fullmatch(code) or str(item["class"]) != code[0]:
            raise ValueError(f"Invalid CGNC account record in {Path(path).name}: {item!r}")
        accounts.append({
            "code": code,
            "label": str(item["label_fr"]).strip(),
            "class": int(item["class"]),
            "status": str(item.get("status", "")),
            "source": source,
        })
    codes = [account["code"] for account in accounts]
    duplicates = sorted({code for code in codes if codes.count(code) > 1})
    if duplicates:
        raise ValueError(f"Duplicate CGNC account codes in {Path(path).name}: {', '.join(duplicates)}")
    orphans = sorted(code for code in codes if len(code) > 4 and code[:4] not in codes)
    if orphans:
        raise ValueError(f"CGNC sub-accounts without a 4-digit parent in {Path(path).name}: {', '.join(orphans)}")
    return accounts


@lru_cache(maxsize=1)
def chart_of_accounts() -> tuple[dict[str, Any], ...]:
    return tuple(sorted(read_dataset(STANDARD_DATASET, "cgnc_standard"), key=lambda account: account["code"]))


@lru_cache(maxsize=1)
def _labels() -> dict[str, str]:
    return {account["code"]: account["label"] for account in chart_of_accounts()}


def official_label(code: str) -> str | None:
    return _labels().get(str(code).strip())


def cgnc_parent(code: str) -> str | None:
    """4-digit CGNC account that a valid 4–8 digit code belongs to."""
    value = str(code).strip()
    if not ACCOUNT_CODE.fullmatch(value) or value[:4] not in _labels():
        return None
    return value[:4]


def cgnc_root(code: str) -> str | None:
    """Most specific listed CGNC code that a valid code equals or extends."""
    if cgnc_parent(code) is None:
        return None
    value = str(code).strip()
    labels = _labels()
    return next(value[:length] for length in range(len(value), 3, -1) if value[:length] in labels)


def is_cgnc_account(code: str) -> bool:
    return cgnc_parent(code) is not None
