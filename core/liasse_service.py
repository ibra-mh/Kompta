"""CGNC Balance to Liasse Fiscale calculation and SIMPL-IS serialization."""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable
from xml.etree.ElementTree import Element, SubElement, fromstring, tostring

from .cgnc import CHARGES_CLASS, PRODUCTS_CLASS
from .liasse_models import (
    BalanceAmountBasis, BalanceLine, IsRateBracket, LiasseComputeRequest, LiasseMappingCatalog,
    LiasseTable,
)


def _money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def balance_amount(line: BalanceLine, basis: BalanceAmountBasis) -> Decimal:
    return {
        BalanceAmountBasis.SOLDE_DEBITEUR: line.debit_balance,
        BalanceAmountBasis.SOLDE_CREDITEUR: line.credit_balance,
        BalanceAmountBasis.MOUVEMENT_DEBIT: line.movement_debit,
        BalanceAmountBasis.MOUVEMENT_CREDIT: line.movement_credit,
    }[basis]


def calculate_progressive_is(taxable_profit: Decimal, brackets: Iterable[IsRateBracket]) -> Decimal:
    remaining = max(Decimal("0"), taxable_profit)
    previous_limit = Decimal("0")
    tax = Decimal("0")
    for bracket in sorted(brackets, key=lambda item: item.up_to or Decimal("1E30")):
        limit = bracket.up_to or (previous_limit + remaining)
        taxable_slice = min(remaining, max(Decimal("0"), limit - previous_limit))
        tax += taxable_slice * bracket.rate / 100
        remaining -= taxable_slice
        previous_limit = limit
        if remaining <= 0:
            break
    return _money(tax)


def compute_liasse(request: LiasseComputeRequest, catalog: LiasseMappingCatalog | None = None) -> dict[str, Any]:
    catalog = catalog or LiasseMappingCatalog(rules=tuple(request.mapping_rules))
    mapped = {table.value: {} for table in LiasseTable}
    for rule in catalog.rules:
        amount = sum(
            (
                balance_amount(line, rule.amount_basis)
                for line in request.balance
                if rule.selector.matches(line.account_code)
            ),
            Decimal("0"),
        )
        if rule.invert_sign:
            amount = -amount
        mapped[rule.table.value][rule.dgi_cell_code] = {
            "label": rule.label, "amount": _money(amount), "table": rule.table.value,
        }

    charges = sum((line.movement_debit for line in request.balance if line.account_code.startswith(CHARGES_CLASS)), Decimal("0"))
    products = sum((line.movement_credit for line in request.balance if line.account_code.startswith(PRODUCTS_CLASS)), Decimal("0"))
    accounting_result = _money(products - charges)
    reintegrations = _money(sum(item.amount for item in request.adjustments if item.direction == "reintegrations"))
    deductions = _money(sum(item.amount for item in request.adjustments if item.direction == "deductions"))
    fiscal_result = _money(accounting_result + reintegrations - deductions)
    taxable_profit = max(Decimal("0"), fiscal_result)
    is_amount = calculate_progressive_is(taxable_profit, request.is_brackets)
    revenue_base = products
    minimum_tax = _money(revenue_base * request.cm_rate)
    tax_before_credits = max(is_amount, minimum_tax)
    tax_due = _money(max(Decimal("0"), tax_before_credits - request.acomptes_is - request.credits_fiscaux))
    return {
        "fiscal_year": request.fiscal_year,
        "identifiant_fiscal": request.identifiant_fiscal,
        "tables": mapped,
        "tax_adjustments": {
            "accounting_result": accounting_result, "reintegrations": reintegrations,
            "deductions": deductions, "fiscal_result": fiscal_result,
        },
        "tax": {
            "is": is_amount, "cotisation_minimale": minimum_tax,
            "tax_before_credits": tax_before_credits, "tax_due": tax_due,
            "acomptes_is": request.acomptes_is, "credits_fiscaux": request.credits_fiscaux,
        },
    }


def build_simpl_is_xml(result: dict[str, Any]) -> bytes:
    root = Element("simplIS", {"version": "1.0", "fiscalYear": str(result["fiscal_year"])})
    SubElement(root, "IdentifiantFiscal").text = result["identifiant_fiscal"]
    tables = SubElement(root, "ValeursTableau")
    for table_code, cells in result["tables"].items():
        table = SubElement(tables, "Tableau")
        SubElement(table, "CodeTableau").text = table_code
        for cell_code, cell in cells.items():
            node = SubElement(table, "Valeur")
            SubElement(node, "CodeCellule").text = cell_code
            SubElement(node, "ValeurCellule").text = str(cell["amount"])
    adjustments = SubElement(root, "Tableau03")
    for key, value in result["tax_adjustments"].items():
        SubElement(adjustments, key).text = str(value)
    tax = SubElement(root, "Tableau04")
    for key, value in result["tax"].items():
        SubElement(tax, key).text = str(value)
    return tostring(root, encoding="utf-8", xml_declaration=True)


def validate_simpl_is_xml(xml_bytes: bytes) -> tuple[bool, list[str]]:
    try:
        root = fromstring(xml_bytes)
    except Exception as error:
        return False, [f"XML malformé: {error}"]
    required = ["IdentifiantFiscal", "ValeursTableau", "Tableau03", "Tableau04"]
    missing = [tag for tag in required if root.find(tag) is None]
    for table in root.findall("./ValeursTableau/Tableau"):
        if table.findtext("CodeTableau") is None:
            missing.append("CodeTableau")
        for value in table.findall("Valeur"):
            if value.findtext("CodeCellule") is None:
                missing.append("CodeCellule")
    return not missing, missing