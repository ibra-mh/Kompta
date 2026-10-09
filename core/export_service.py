"""
Top-level export orchestration for SIMPL-TVA and SIMPL-IR.

Pipeline for both exports:
  1. Blocking pre-validation (business rules: ICE/IF, arithmetic, duplicates)
  2. XML generation
  3. XSD schema validation (structural correctness)
  4. ZIP packaging

If step 1 or step 3 fails, no zip is produced and a structured error
report is returned instead - the caller (API layer) is responsible for
surfacing that to the UI with line numbers.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from .export_service_types import ExportOutcome
from .models import Etat9421, ReleveDeductions
from .packaging import zip_xml_payload
from .validators import ValidationReport, validate_etat_9421, validate_releve_deductions
from .xml_builders import build_etat_9421_xml, build_releve_deductions_xml
from .xsd_validator import validate_against_schema

SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"


def _run_export_pipeline(
    business_report: ValidationReport,
    build_xml: Callable[[], bytes],
    xsd_path: str | Path,
    file_stem: str,
) -> ExportOutcome:
    if business_report.is_blocked:
        return ExportOutcome(success=False, business_report=business_report)

    xml_bytes = build_xml()

    xsd_result = validate_against_schema(xml_bytes, xsd_path)
    if not xsd_result.valid:
        return ExportOutcome(
            success=False,
            business_report=business_report,
            xsd_result=xsd_result,
            xml_bytes=xml_bytes,
        )

    zip_bytes = zip_xml_payload(xml_bytes, inner_filename=f"{file_stem}.xml")
    return ExportOutcome(
        success=True,
        business_report=business_report,
        xsd_result=xsd_result,
        xml_bytes=xml_bytes,
        zip_bytes=zip_bytes,
        zip_filename=f"{file_stem}.zip",
    )


def export_releve_deductions(
    releve: ReleveDeductions,
    xsd_path: str | Path = SCHEMAS_DIR / "releve_deductions.xsd",
) -> ExportOutcome:
    return _run_export_pipeline(
        validate_releve_deductions(releve),
        lambda: build_releve_deductions_xml(releve),
        xsd_path,
        f"SIMPL_TVA_{releve.periode}",
    )


def export_etat_9421(
    etat: Etat9421,
    xsd_path: str | Path = SCHEMAS_DIR / "etat_9421.xsd",
) -> ExportOutcome:
    return _run_export_pipeline(
        validate_etat_9421(etat),
        lambda: build_etat_9421_xml(etat),
        xsd_path,
        f"ETAT_9421_{etat.exercice}",
    )
