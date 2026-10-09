import os
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api import accounts_router, clients_router, export_portfolio_tva_excel, export_tva_excel, ocr_router, router as exports_router
from api import compute_liasse_endpoint, export_simpl_is, list_journal_entries, post_journal_entry, reverse_journal_entry

app = FastAPI(title="Kompta - DGI SIMPL Export Engine")
app.add_api_route("/api/journal-entries", list_journal_entries, methods=["GET"])


def local_development_origins() -> list[str]:
    configured = os.environ.get("KOMPTA_DEV_CORS_ORIGINS", "")
    origins = [origin.strip() for origin in configured.split(",") if origin.strip()]
    for origin in origins:
        parsed = urlsplit(origin)
        try:
            port = parsed.port
        except ValueError as error:
            raise ValueError("Invalid local development CORS origin") from error
        if (
            origin == "*"
            or parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "KOMPTA_DEV_CORS_ORIGINS accepts only exact http://localhost or "
                "http://127.0.0.1 origins with an explicit port"
            )
    return list(dict.fromkeys(origins))

# The standalone index.html is opened from file:// and sends Origin: null.
# This local CORS policy is not user authentication.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["null", *local_development_origins()],
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=[
        "Content-Type", "X-Kompta-Client-Id", "X-Kompta-Year",
        "X-Kompta-Filename", "X-Kompta-Client-Ice",
    ],
    expose_headers=["Content-Disposition", "X-Kompta-Export-Status"],
)

app.include_router(exports_router)
app.include_router(clients_router)
app.include_router(accounts_router)
app.include_router(ocr_router)
# Public short route used by the browser Excel export action.
app.add_api_route("/export-tva-excel", export_tva_excel, methods=["POST"], tags=["dgi-exports"])
app.add_api_route("/api/export-tva-excel", export_tva_excel, methods=["POST"], tags=["dgi-exports"])
app.add_api_route("/export-portfolio-tva-excel", export_portfolio_tva_excel, methods=["POST"], tags=["dgi-exports"])
app.add_api_route("/api/liasse/compute", compute_liasse_endpoint, methods=["POST"], tags=["liasse"])
app.add_api_route("/api/liasse/simpl-is.xml", export_simpl_is, methods=["POST"], tags=["liasse"])
app.add_api_route("/api/journal-entries", post_journal_entry, methods=["POST"], tags=["journal"])
app.add_api_route("/api/journal-entries/{entry_id}/reversal", reverse_journal_entry, methods=["POST"], tags=["journal"])


@app.get("/health")
def health():
    return {"status": "ok"}
