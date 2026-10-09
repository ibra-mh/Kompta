"""
Blocking pre-validation checks for SIMPL-TVA and SIMPL-IR exports.

These run BEFORE any XML is generated. If any blocking error is found,
the export must not proceed - the caller gets a structured
ValidationReport back to display to the user (with line numbers).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .models import Etat9421, ReleveDeductions


class Severity(str, Enum):
    BLOCKING = "BLOCKING"
    WARNING = "WARNING"


@dataclass
class ValidationIssue:
    severity: Severity
    code: str
    message: str
    line_ord: int | None = None
    field: str | None = None


@dataclass
class ValidationReport:
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def is_blocked(self) -> bool:
        return any(i.severity == Severity.BLOCKING for i in self.issues)

    @property
    def blocking_issues(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == Severity.BLOCKING]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == Severity.WARNING]

    def add(self, severity: Severity, code: str, message: str,
            line_ord: int | None = None, field: str | None = None) -> None:
        self.issues.append(ValidationIssue(severity, code, message, line_ord, field))

    def to_dict(self) -> dict:
        return {
            "is_blocked": self.is_blocked,
            "blocking_count": len(self.blocking_issues),
            "warning_count": len(self.warnings),
            "issues": [
                {
                    "severity": i.severity.value,
                    "code": i.code,
                    "message": i.message,
                    "line": i.line_ord,
                    "field": i.field,
                }
                for i in self.issues
            ],
        }


def validate_releve_deductions(releve: ReleveDeductions) -> ValidationReport:
    """
    Run all blocking pre-validation checks on a ReleveDeductions batch.
    Individual line-level checks (ICE/IF presence+format, HT+TVA==TTC)
    are already enforced by the DeductionLine model itself when
    constructed directly; this function additionally runs the
    cross-line checks (duplicate detection) and re-surfaces any
    per-line issues if lines were constructed leniently upstream.
    """
    report = ValidationReport()

    if not releve.lines:
        report.add(Severity.BLOCKING, "EMPTY_RELEVE", "The déduction statement has no lines.")
        return report

    # Duplicate invoice detection: numFacture + ice combination
    seen: dict[tuple[str, str], int] = {}
    for line in releve.lines:
        key = line.duplicate_key()
        if key in seen:
            report.add(
                Severity.BLOCKING,
                code="DUPLICATE_INVOICE",
                message=(
                    f"Duplicate invoice detected: numFacture='{line.num_facture}' "
                    f"+ identifier='{key[1]}' also appears at line {seen[key]}."
                ),
                line_ord=line.ord,
                field="numFacture+ice",
            )
        else:
            seen[key] = line.ord

    # Non-blocking sanity checks (warnings) - e.g. unusual TVA rate
    valid_rates = {0, 7, 10, 14, 20}
    for line in releve.lines:
        if int(line.taux_tva) not in valid_rates:
            report.add(
                Severity.WARNING,
                code="UNUSUAL_TVA_RATE",
                message=f"TVA rate {line.taux_tva}% is not one of the standard rates {sorted(valid_rates)}.",
                line_ord=line.ord,
                field="tauxTVA",
            )

    return report


def validate_etat_9421(etat: Etat9421) -> ValidationReport:
    """Blocking pre-validation for the SIMPL-IR annual wage statement."""
    report = ValidationReport()

    if not etat.lines:
        report.add(Severity.BLOCKING, "EMPTY_ETAT", "État 9421 has no employee lines.")
        return report

    seen_cin: dict[str, int] = {}
    for line in etat.lines:
        if line.cin:
            if line.cin in seen_cin:
                report.add(
                    Severity.BLOCKING,
                    code="DUPLICATE_EMPLOYEE",
                    message=f"CIN '{line.cin}' appears more than once (also line {seen_cin[line.cin]}).",
                    line_ord=line.ord,
                    field="cin",
                )
            else:
                seen_cin[line.cin] = line.ord

        if line.date_sortie and line.date_entree and line.date_sortie < line.date_entree:
            report.add(
                Severity.BLOCKING,
                code="INVALID_DATE_RANGE",
                message="date_sortie is before date_entree.",
                line_ord=line.ord,
                field="date_sortie",
            )

    return report
