"""
XML builders for SIMPL-TVA (Relevé des Déductions) and SIMPL-IR (État 9421).

IMPORTANT: The tag names, ordering, and namespace below follow the
structure described in the Kompta feature spec (mirroring the public
description of the DGI "cahier des charges"). Anthropic/Claude does not
have access to the real, current DGI XSD files, so before this goes to
production you MUST:
  1. Obtain the official XSD from the DGI portal for the exact version
     you are targeting (SIMPL-TVA v2026.1 / V4.0, SIMPL-IR v5.1).
  2. Diff the tag names/order/namespace against what's generated here.
  3. Load the real XSD into core/xsd_validator.py's schema path.

The builders are deliberately isolated behind a small interface
(`build_releve_deductions_xml`, `build_etat_9421_xml`) so swapping the
tag layout later is a localized change.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom import minidom

from .models import DeductionLine, Etat9421, ReleveDeductions, SalarieLine


def _fmt_amount(d: Decimal) -> str:
    return str(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _pretty(elem: Element) -> bytes:
    rough = tostring(elem, encoding="utf-8")
    return minidom.parseString(rough).toprettyxml(indent="  ", encoding="utf-8")


# --------------------------------------------------------------------------
# SIMPL-TVA : Relevé des Déductions
# --------------------------------------------------------------------------

def _build_deduction_line_element(parent: Element, line: DeductionLine) -> None:
    ligne = SubElement(parent, "ligneDeduction")
    SubElement(ligne, "ord").text = str(line.ord)
    SubElement(ligne, "numFacture").text = line.num_facture
    SubElement(ligne, "designation").text = line.designation
    SubElement(ligne, "montantHT").text = _fmt_amount(line.montant_ht)
    SubElement(ligne, "tauxTVA").text = _fmt_amount(line.taux_tva)
    SubElement(ligne, "montantTVA").text = _fmt_amount(line.montant_tva)
    SubElement(ligne, "montantTTC").text = _fmt_amount(line.montant_ttc)
    if line.identifiant_fiscal:
        SubElement(ligne, "identifiantFiscal").text = line.identifiant_fiscal
    if line.ice:
        SubElement(ligne, "ice").text = line.ice
    SubElement(ligne, "modePaiement").text = line.mode_paiement.value
    SubElement(ligne, "datePaiement").text = line.date_paiement.isoformat()
    SubElement(ligne, "dateFacture").text = line.date_facture.isoformat()
    SubElement(ligne, "prorata").text = _fmt_amount(line.prorata)


def build_releve_deductions_xml(releve: ReleveDeductions) -> bytes:
    """
    Build the SIMPL-TVA "Relevé des Déductions" XML document.
    Caller is responsible for having already run validate_releve_deductions()
    and confirming report.is_blocked is False.
    """
    root = Element("releveDeductions", attrib={"version": "V4.0"})
    entete = SubElement(root, "entete")
    SubElement(entete, "iceDeclarant").text = releve.ice_declarant
    SubElement(entete, "ifDeclarant").text = releve.if_declarant
    SubElement(entete, "periode").text = releve.periode
    SubElement(entete, "nombreLignes").text = str(len(releve.lines))
    total_ttc = sum((l.montant_ttc for l in releve.lines), Decimal("0"))
    SubElement(entete, "montantTotalTTC").text = _fmt_amount(total_ttc)

    lignes = SubElement(root, "lignes")
    for line in releve.lines:
        _build_deduction_line_element(lignes, line)

    return _pretty(root)


# --------------------------------------------------------------------------
# SIMPL-IR : État 9421
# --------------------------------------------------------------------------

def _build_salarie_line_element(parent: Element, line: SalarieLine) -> None:
    ligne = SubElement(parent, "salarie")
    SubElement(ligne, "ord").text = str(line.ord)
    if line.matricule_cnss:
        SubElement(ligne, "matriculeCNSS").text = line.matricule_cnss
    SubElement(ligne, "nom").text = line.nom
    SubElement(ligne, "prenom").text = line.prenom
    if line.cin:
        SubElement(ligne, "cin").text = line.cin
    SubElement(ligne, "categorie").text = line.categorie.value
    if line.date_entree:
        SubElement(ligne, "dateEntree").text = line.date_entree.isoformat()
    if line.date_sortie:
        SubElement(ligne, "dateSortie").text = line.date_sortie.isoformat()
    SubElement(ligne, "brutImposable").text = _fmt_amount(line.brut_imposable)
    SubElement(ligne, "irRetenu").text = _fmt_amount(line.ir_retenu)
    SubElement(ligne, "netPaye").text = _fmt_amount(line.net_paye)


def build_etat_9421_xml(etat: Etat9421) -> bytes:
    """Build the SIMPL-IR "État 9421" annual wage withholding XML document."""
    root = Element("etat9421", attrib={"version": "5.1"})
    entete = SubElement(root, "entete")
    SubElement(entete, "iceEmployeur").text = etat.ice_employeur
    SubElement(entete, "ifEmployeur").text = etat.if_employeur
    SubElement(entete, "exercice").text = str(etat.exercice)
    SubElement(entete, "nombreSalaries").text = str(len(etat.lines))
    total_ir = sum((l.ir_retenu for l in etat.lines), Decimal("0"))
    SubElement(entete, "montantTotalIR").text = _fmt_amount(total_ir)

    salaries = SubElement(root, "salaries")
    for line in etat.lines:
        _build_salarie_line_element(salaries, line)

    return _pretty(root)
