"""Typed schema for mapping the Moroccan CGNC trial balance to DGI liasse cells.

The official DGI cell identifiers are intentionally supplied by configuration.
This keeps the accounting classification reusable without hardcoding unverified
form coordinates or labels into the engine.
"""
from __future__ import annotations

from enum import Enum, IntEnum
from decimal import Decimal
from typing import Iterable

from pydantic import BaseModel, Field, field_validator, model_validator


class LiasseTable(str, Enum):
    BILAN_ACTIF = "TABLEAU_1_BILAN_ACTIF"
    BILAN_PASSIF = "TABLEAU_2_BILAN_PASSIF"
    CPC = "TABLEAU_3_CPC"
    ESG = "TABLEAU_5_ESG"
    TABLEAU_FINANCEMENT = "TABLEAU_6_FINANCEMENT"
    ETIC = "TABLEAU_7_ETIC"


class CgncClass(IntEnum):
    CAPITAUX_PROPRES = 1
    IMMOBILISATIONS = 2
    ACTIF_CIRCULANT = 3
    PASSIF_CIRCULANT = 4
    TRESORERIE = 5
    CHARGES = 6
    PRODUITS = 7


class BalanceAmountBasis(str, Enum):
    SOLDE_DEBITEUR = "solde_debiteur"
    SOLDE_CREDITEUR = "solde_crediteur"
    MOUVEMENT_DEBIT = "mouvement_debit"
    MOUVEMENT_CREDIT = "mouvement_credit"


class BalanceLine(BaseModel):
    """One row from the six-column CGNC trial balance."""

    account_code: str = Field(..., alias="accountCode")
    label: str = ""
    opening_debit: Decimal = Field(0, alias="openingDebit")
    opening_credit: Decimal = Field(0, alias="openingCredit")
    movement_debit: Decimal = Field(0, alias="movementDebit")
    movement_credit: Decimal = Field(0, alias="movementCredit")

    model_config = {"populate_by_name": True}

    @property
    def debit_balance(self) -> Decimal:
        return max(Decimal("0"), self.opening_debit + self.movement_debit - self.opening_credit - self.movement_credit)

    @property
    def credit_balance(self) -> Decimal:
        return max(Decimal("0"), self.opening_credit + self.movement_credit - self.opening_debit - self.movement_debit)


class TaxAdjustment(BaseModel):
    code: str
    label: str
    amount: Decimal = Field(0, ge=0)
    direction: str = "reintegrations"

    @model_validator(mode="after")
    def valid_direction(self) -> "TaxAdjustment":
        if self.direction not in {"reintegrations", "deductions"}:
            raise ValueError("direction must be reintegrations or deductions")
        return self


class IsRateBracket(BaseModel):
    up_to: Decimal | None = Field(None, alias="upTo", gt=0)
    rate: Decimal = Field(..., ge=0, le=100)

    model_config = {"populate_by_name": True}


class LiasseComputeRequest(BaseModel):
    fiscal_year: int = Field(..., alias="fiscalYear", ge=2000, le=2100)
    identifiant_fiscal: str = Field("", alias="identifiantFiscal")
    demo_only: bool = Field(False, alias="demoOnly")
    balance: list[BalanceLine] = Field(default_factory=list)
    adjustments: list[TaxAdjustment] = Field(default_factory=list)
    credit_anterieur: Decimal = Field(0, alias="creditAnterieur", ge=0)
    acomptes_is: Decimal = Field(0, alias="acomptesIS", ge=0)
    credits_fiscaux: Decimal = Field(0, alias="creditsFiscaux", ge=0)
    cm_rate: Decimal = Field(Decimal("0.005"), alias="cmRate", ge=0, le=1)
    is_brackets: list[IsRateBracket] = Field(default_factory=lambda: [
        IsRateBracket(upTo=Decimal("300000"), rate=Decimal("10")),
        IsRateBracket(upTo=Decimal("1000000"), rate=Decimal("20")),
        IsRateBracket(upTo=None, rate=Decimal("31")),
    ], alias="isBrackets")
    mapping_rules: list[LiasseMappingRule] = Field(default_factory=list, alias="mappingRules")

    model_config = {"populate_by_name": True}


class MappingSelector(BaseModel):
    """CGNC account selector; exact codes take precedence over prefixes."""

    exact_accounts: tuple[str, ...] = Field((), alias="exactAccounts")
    account_prefixes: tuple[str, ...] = Field((), alias="accountPrefixes")
    cgnc_class: CgncClass = Field(..., alias="cgncClass")

    model_config = {"populate_by_name": True}

    @field_validator("exact_accounts", "account_prefixes")
    @classmethod
    def normalize_codes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(str(value).strip() for value in values if str(value).strip())
        if any(not value.isdigit() for value in normalized):
            raise ValueError("CGNC account codes and prefixes must contain digits only")
        return normalized

    @model_validator(mode="after")
    def require_selector(self) -> "MappingSelector":
        if not self.exact_accounts and not self.account_prefixes:
            raise ValueError("A mapping selector needs an exact account or account prefix")
        return self

    def matches(self, account_code: str) -> bool:
        code = str(account_code).strip()
        return code in self.exact_accounts or any(code.startswith(prefix) for prefix in self.account_prefixes)


class LiasseMappingRule(BaseModel):
    """One DGI cell rule and the CGNC accounts contributing to it."""

    dgi_cell_code: str = Field(..., min_length=1, alias="dgiCellCode", description="Official DGI cell identifier")
    label: str = Field(..., min_length=1)
    table: LiasseTable
    selector: MappingSelector
    amount_basis: BalanceAmountBasis = Field(..., alias="amountBasis")
    order: int = Field(..., ge=1)
    invert_sign: bool = False

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def validate_table_for_class(self) -> "LiasseMappingRule":
        expected = {
            CgncClass.IMMOBILISATIONS: {LiasseTable.BILAN_ACTIF, LiasseTable.ETIC},
            CgncClass.ACTIF_CIRCULANT: {LiasseTable.BILAN_ACTIF, LiasseTable.ETIC},
            CgncClass.CAPITAUX_PROPRES: {LiasseTable.BILAN_PASSIF, LiasseTable.TABLEAU_FINANCEMENT},
            CgncClass.PASSIF_CIRCULANT: {LiasseTable.BILAN_PASSIF, LiasseTable.ETIC},
            CgncClass.CHARGES: {LiasseTable.CPC, LiasseTable.ESG},
            CgncClass.PRODUITS: {LiasseTable.CPC, LiasseTable.ESG},
        }
        expected_tables = expected.get(self.selector.cgnc_class)
        if expected_tables is None:
            raise ValueError("Class 5 treasury accounts are not assigned to the requested three tables")
        if self.table not in expected_tables:
            raise ValueError(
                f"CGNC class {self.selector.cgnc_class.value} is not valid for {self.table.value}; "
                f"expected one of {', '.join(sorted(table.value for table in expected_tables))}"
            )
        return self


class LiasseMappingCatalog(BaseModel):
    """Validated collection of DGI-to-CGNC rules used by the Liasse engine."""

    rules: tuple[LiasseMappingRule, ...] = ()

    @model_validator(mode="after")
    def validate_unique_cells_and_orders(self) -> "LiasseMappingCatalog":
        cells = [rule.dgi_cell_code for rule in self.rules]
        if len(cells) != len(set(cells)):
            raise ValueError("Each DGI cell code must appear only once in a mapping catalog")
        order_keys = [(rule.table, rule.order) for rule in self.rules]
        if len(order_keys) != len(set(order_keys)):
            raise ValueError("Rule order must be unique within each liasse table")
        return self

    def rules_for_table(self, table: LiasseTable) -> tuple[LiasseMappingRule, ...]:
        return tuple(rule for rule in self.rules if rule.table == table)

    def match_account(self, account_code: str) -> tuple[LiasseMappingRule, ...]:
        return tuple(rule for rule in self.rules if rule.selector.matches(account_code))

    @classmethod
    def from_rules(cls, rules: Iterable[LiasseMappingRule]) -> "LiasseMappingCatalog":
        return cls(rules=tuple(rules))


# These scopes document the expected coverage before official DGI cell codes
# are loaded from the versioned mapping configuration.
LIASSE_CLASS_SCOPE: dict[LiasseTable, tuple[CgncClass, ...]] = {
    LiasseTable.BILAN_ACTIF: (CgncClass.IMMOBILISATIONS, CgncClass.ACTIF_CIRCULANT),
    LiasseTable.BILAN_PASSIF: (CgncClass.CAPITAUX_PROPRES, CgncClass.PASSIF_CIRCULANT),
    LiasseTable.CPC: (CgncClass.CHARGES, CgncClass.PRODUITS),
    LiasseTable.ESG: (CgncClass.CHARGES, CgncClass.PRODUITS),
    LiasseTable.TABLEAU_FINANCEMENT: (CgncClass.CAPITAUX_PROPRES, CgncClass.IMMOBILISATIONS),
    LiasseTable.ETIC: (CgncClass.IMMOBILISATIONS, CgncClass.ACTIF_CIRCULANT, CgncClass.PASSIF_CIRCULANT),
}
