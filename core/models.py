"""
Domain models for the DGI SIMPL EDI/XML Export Engine.

Covers:
  - SIMPL-TVA (Relevé des Déductions)
  - SIMPL-IR  (État 9421 - Traitements et Salaires)

These are plain Pydantic models used both for validation (pre-flight
checks) and as the data contract fed into the XML builders.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# --------------------------------------------------------------------------
# Shared enums
# --------------------------------------------------------------------------

class ModePaiement(str, Enum):
    ESPECES = "ESPECES"
    CHEQUE = "CHEQUE"
    VIREMENT = "VIREMENT"
    EFFET = "EFFET"
    COMPENSATION = "COMPENSATION"
    CARTE_BANCAIRE = "CARTE_BANCAIRE"


class CategorieSalarie(str, Enum):
    PERMANENT = "PERMANENT"
    OCCASIONNEL = "OCCASIONNEL"
    STAGIAIRE = "STAGIAIRE"
    SALARIE_CFC = "SALARIE_CFC"


# --------------------------------------------------------------------------
# SIMPL-TVA : Relevé des Déductions
# --------------------------------------------------------------------------

class DeductionLineData(BaseModel):
    """Deduction line fields without SIMPL-TVA checks; used for internal Excel workbooks."""

    ord: int = Field(..., ge=1, description="Sequential line order number")
    num_facture: str = Field(..., min_length=1, max_length=50, alias="numFacture")
    designation: str = Field(..., min_length=1, max_length=255)
    fournisseur: Optional[str] = Field(None, max_length=255)
    montant_ht: Decimal = Field(..., alias="montantHT")
    taux_tva: Decimal = Field(..., alias="tauxTVA", description="e.g. 20, 14, 10, 7")
    montant_tva: Decimal = Field(..., alias="montantTVA")
    montant_ttc: Decimal = Field(..., alias="montantTTC")
    identifiant_fiscal: Optional[str] = Field(None, alias="identifiantFiscal", description="IF - 8 digits")
    ice: Optional[str] = Field(None, description="ICE - 15 digits")
    mode_paiement: ModePaiement = Field(..., alias="modePaiement")
    date_paiement: date = Field(..., alias="datePaiement")
    date_facture: date = Field(..., alias="dateFacture")
    prorata: Decimal = Field(default=Decimal("100"), ge=0, le=100)

    model_config = {"populate_by_name": True}


class DeductionLine(DeductionLineData):
    """A single line of the Relevé des Déductions (TVA deductible)."""

    @field_validator("ice")
    @classmethod
    def validate_ice_format(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not v.isdigit() or len(v) != 15:
            raise ValueError(f"ICE must be exactly 15 digits, got '{v}'")
        return v

    @field_validator("identifiant_fiscal")
    @classmethod
    def validate_if_format(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not v.isdigit() or len(v) != 8:
            raise ValueError(f"IF must be exactly 8 digits, got '{v}'")
        return v

    @model_validator(mode="after")
    def validate_identifier_present(self) -> "DeductionLine":
        # Rule: mandatory ICE (15 digits) OR IF (8 digits) on every line.
        if not self.ice and not self.identifiant_fiscal:
            raise ValueError(
                f"Line ord={self.ord}: either ICE (15 digits) or IF (8 digits) is mandatory"
            )
        return self

    @model_validator(mode="after")
    def validate_arithmetic(self) -> "DeductionLine":
        # Rule: round(HT + TVA, 2) == round(TTC, 2)
        computed = (self.montant_ht + self.montant_tva).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        ttc_rounded = self.montant_ttc.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if computed != ttc_rounded:
            raise ValueError(
                f"Line ord={self.ord}: arithmetic mismatch - "
                f"HT({self.montant_ht}) + TVA({self.montant_tva}) = {computed} "
                f"!= TTC({ttc_rounded})"
            )
        return self

    def duplicate_key(self) -> tuple[str, str]:
        """Key used for duplicate detection: numFacture + ice (or IF as fallback)."""
        identifier = self.ice or self.identifiant_fiscal or ""
        return (self.num_facture.strip().upper(), identifier)


class ReleveDeductionsData(BaseModel):
    """Deduction statement without SIMPL-TVA checks; used for internal Excel workbooks."""

    ice_declarant: str = Field("", description="ICE of the declaring entity")
    if_declarant: str = Field("", description="IF of the declaring entity")
    periode: str = Field(..., description="e.g. '2026-08' or 'T3-2026'")
    demo_only: bool = Field(False, alias="demoOnly")
    lines: list[DeductionLineData] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class ReleveDeductions(ReleveDeductionsData):
    """The full SIMPL-TVA deduction statement for one declaration period."""

    ice_declarant: str = Field(..., description="ICE of the declaring entity")
    if_declarant: str = Field(..., description="IF of the declaring entity")
    lines: list[DeductionLine] = Field(default_factory=list)


class SalesLine(BaseModel):
    """A single sales invoice line for the collected VAT statement."""

    ord: int = Field(..., ge=1)
    num_facture: str = Field(..., min_length=1, max_length=50, alias="numFacture")
    date_facture: date = Field(..., alias="dateFacture")
    client: Optional[str] = None
    identifiant_fiscal: Optional[str] = Field(None, alias="identifiantFiscal")
    ice: Optional[str] = None
    montant_ht: Decimal = Field(..., alias="montantHT")
    taux_tva: Decimal = Field(..., alias="tauxTVA")
    montant_tva: Decimal = Field(..., alias="montantTVA")
    montant_ttc: Decimal = Field(..., alias="montantTTC")

    model_config = {"populate_by_name": True}


class ExcelExportRequest(BaseModel):
    """Metadata and TVA totals used to build the accountant's workbook."""

    dossier_id: str = Field(..., min_length=1, alias="dossierId")
    company_name: str = Field(..., min_length=1, alias="companyName")
    regime: str = Field(..., min_length=1)
    tva_collectee: Decimal = Field(default=Decimal("0"), alias="tvaCollectee", ge=0)
    ventes: list[SalesLine] = Field(default_factory=list)
    releve: ReleveDeductionsData

    model_config = {"populate_by_name": True}


class PortfolioPurchaseLine(BaseModel):
    company_name: str = Field(..., alias="companyName")
    ord: int = 1
    num_facture: str = Field(..., alias="numFacture")
    designation: str = ""
    fournisseur: str = ""
    identifiant_fiscal: str = Field("", alias="identifiantFiscal")
    ice: str = ""
    montant_ht: Decimal = Field(0, alias="montantHT")
    taux_tva: Decimal = Field(0, alias="tauxTVA")
    montant_tva: Decimal = Field(0, alias="montantTVA")
    montant_ttc: Decimal = Field(0, alias="montantTTC")
    mode_paiement: str = Field("VIREMENT", alias="modePaiement")
    date_facture: date = Field(..., alias="dateFacture")
    date_paiement: date = Field(..., alias="datePaiement")
    prorata: Decimal = 100

    model_config = {"populate_by_name": True}


class PortfolioClient(BaseModel):
    company_name: str = Field(..., alias="companyName")
    if_client: str = Field("", alias="ifClient")
    ice_client: str = Field("", alias="iceClient")
    regime: str = "Débit"
    credit_anterieur: Decimal = Field(0, alias="creditAnterieur")
    ventes: list[SalesLine] = Field(default_factory=list)
    achats: list[PortfolioPurchaseLine] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class PortfolioExcelRequest(BaseModel):
    clients: list[PortfolioClient] = Field(default_factory=list)


# --------------------------------------------------------------------------
# SIMPL-IR : État 9421 - Traitements et Salaires
# --------------------------------------------------------------------------

class SalarieLine(BaseModel):
    """A single employee record in the annual wage withholding statement (État 9421)."""

    ord: int = Field(..., ge=1)
    matricule_cnss: Optional[str] = Field(None, description="CNSS registration number")
    nom: str = Field(..., min_length=1)
    prenom: str = Field(..., min_length=1)
    cin: Optional[str] = Field(None, description="National ID, for salaried individuals")
    categorie: CategorieSalarie
    date_entree: Optional[date] = None
    date_sortie: Optional[date] = None
    brut_imposable: Decimal = Field(..., ge=0)
    ir_retenu: Decimal = Field(..., ge=0, description="Total IR withheld for the year")
    net_paye: Decimal = Field(..., ge=0)

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def validate_cfc_or_permanent_needs_cin(self) -> "SalarieLine":
        if self.categorie in (CategorieSalarie.PERMANENT, CategorieSalarie.SALARIE_CFC) and not self.cin:
            raise ValueError(
                f"Line ord={self.ord}: CIN is mandatory for category {self.categorie.value}"
            )
        return self


class Etat9421(BaseModel):
    """The full SIMPL-IR annual declaration for one employer / fiscal year."""

    ice_employeur: str
    if_employeur: str
    exercice: int = Field(..., ge=2000, le=2100)
    demo_only: bool = Field(False, alias="demoOnly")
    lines: list[SalarieLine] = Field(default_factory=list)

    model_config = {"populate_by_name": True}
