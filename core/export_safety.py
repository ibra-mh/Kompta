"""Safety policy for unverified DGI-facing export endpoints."""
from __future__ import annotations

from fastapi import HTTPException


EXPORT_SAFETY_LABEL = "TEST ONLY — NOT VERIFIED BY DGI"
EXPORT_SAFETY_HEADER = "TEST_ONLY_NOT_VERIFIED_BY_DGI"


def require_demo_export(demo_only: bool) -> None:
    """Prevent unmarked data from reaching an unverified SIMPL export path."""
    if not demo_only:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "SIMPL_EXPORT_TEST_ONLY",
                "message": EXPORT_SAFETY_LABEL,
                "reason": "Set demoOnly=true to generate a fictitious test export.",
            },
        )


def safety_download_headers(filename: str) -> dict[str, str]:
    return {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "X-Kompta-Export-Status": EXPORT_SAFETY_HEADER,
    }
