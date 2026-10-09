"""Safe document intake and text extraction for the OCR review workflow.

This module retains source documents in SQLite, extracts PDF text with pypdf or
recognizes PNG/JPEG text with French-configured RapidOCR, then parses invoice
metadata (supplier, ICE, invoice number, dates, and amounts).

Accounting safeguards:
- Extracted data is presented for user review and never posted directly to the ledger.
- Duplicate detection by SHA-256 is strictly preserved.
"""
from __future__ import annotations

import hashlib
import mimetypes
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from .invoice_extractor import (
    ExtractedInvoiceData,
    extract_text_from_pdf,
    parse_invoice_text,
)
from .storage import connect_database, resolve_database_path, utc_now_iso


MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
SUPPORTED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}
SUPPORTED_MIME_TYPES = {"application/pdf", "image/jpeg", "image/png"}
OCR_STATUSES = {
    "uploaded", "processing", "needs_review", "validation_failed",
    "duplicate_suspected", "draft_created", "confirmed", "posted",
    "rejected", "failed",
}
_IMAGE_OCR_ENGINE = None


def _decode_image(content: bytes):
    """Decode image bytes into the BGR array format expected by RapidOCR."""
    import cv2
    import numpy as np

    image = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Le fichier image ne peut pas être décodé.")
    return image


def _recognize_image(image) -> str:
    """Run local RapidOCR on a decoded image and return recognized lines."""
    global _IMAGE_OCR_ENGINE
    try:
        if _IMAGE_OCR_ENGINE is None:
            from rapidocr import RapidOCR

            _IMAGE_OCR_ENGINE = RapidOCR(
                params={"Det.lang_type": "fr", "Rec.lang_type": "fr"}
            )
        result = _IMAGE_OCR_ENGINE(image)
    except Exception as error:
        raise ValueError("Le moteur OCR n’a pas pu lire cette image.") from error

    lines = getattr(result, "txts", None) or ()
    return "\n".join(line.strip() for line in lines if line and line.strip())


def extract_text_from_image(content: bytes) -> str:
    """Decode PNG/JPEG bytes and recognize their text with local RapidOCR."""
    try:
        image = _decode_image(content)
    except Exception as error:
        raise ValueError("Le fichier image ne peut pas être décodé.") from error
    return _recognize_image(image)


def _extract_text_from_pdf_images(content: bytes) -> str:
    """Render PDF pages and recognize text for scanned or partially parsed PDFs."""
    import numpy as np
    import pypdfium2 as pdfium

    text_parts = []
    document = pdfium.PdfDocument(content)
    for page in document:
        image = page.render(scale=2).to_pil().convert("RGB")
        image_array = np.asarray(image)[:, :, ::-1].copy()
        recognized = _recognize_image(image_array)
        if recognized:
            text_parts.append(recognized)
    return "\n".join(text_parts)


def _merge_missing_fields(primary: ExtractedInvoiceData, fallback: ExtractedInvoiceData) -> ExtractedInvoiceData:
    fields = (
        "supplier", "ice", "invoice_number", "date", "ht", "vat",
        "vat_rate", "ttc", "raw_text_snippet",
    )
    updates = {
        field: getattr(fallback, field)
        for field in fields
        if getattr(primary, field) is None and getattr(fallback, field) is not None
    }
    confidence = dict(primary.confidence)
    for key, value in fallback.confidence.items():
        confidence.setdefault(key, value)
    if confidence != primary.confidence:
        updates["confidence"] = confidence
    warnings = list(dict.fromkeys([*primary.warnings, *fallback.warnings]))
    if warnings != primary.warnings:
        updates["warnings"] = warnings
    return primary.model_copy(update=updates) if updates else primary


def _load_extracted_json(raw: str | None) -> ExtractedInvoiceData | None:
    """Corrupt or legacy stored extractions are treated as absent rather than fatal."""
    if not raw:
        return None
    try:
        return ExtractedInvoiceData.model_validate_json(raw)
    except Exception:
        return None


class OcrDocument(BaseModel):
    document_id: int = Field(alias="documentId")
    client_id: str = Field(alias="clientId")
    year: int
    filename: str
    mime_type: str = Field(alias="mimeType")
    size: int
    checksum: str
    status: str
    duplicate_matches: list[int] = Field(default_factory=list, alias="duplicateMatches")
    processing_error: Optional[str] = Field(None, alias="processingError")
    extracted_data: Optional[ExtractedInvoiceData] = Field(None, alias="extractedData")

    model_config = {"populate_by_name": True}


class OcrDocumentStore:
    def __init__(self, database: str | Path | None = None) -> None:
        self.database = str(resolve_database_path(database))
        with closing(self._connect()) as db, db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS ocr_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_id TEXT NOT NULL,
                    fiscal_year INTEGER NOT NULL,
                    filename TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    checksum TEXT NOT NULL,
                    content BLOB NOT NULL,
                    status TEXT NOT NULL,
                    duplicate_matches TEXT NOT NULL DEFAULT '',
                    processing_error TEXT,
                    uploaded_at TEXT NOT NULL,
                    extracted_json TEXT,
                    UNIQUE(client_id, checksum)
                )"""
            )
            # Safe migration: ensure extracted_json column exists if table pre-dated it
            cursor = db.execute("PRAGMA table_info(ocr_documents)")
            columns = [row[1] for row in cursor.fetchall()]
            if "extracted_json" not in columns:
                db.execute("ALTER TABLE ocr_documents ADD COLUMN extracted_json TEXT")

    def _connect(self) -> sqlite3.Connection:
        return connect_database(self.database)

    @staticmethod
    def _validate_file(filename: str, mime_type: str, content: bytes) -> None:
        safe_name = Path(filename).name
        extension = Path(safe_name).suffix.lower()
        if not safe_name or safe_name != filename or extension not in SUPPORTED_EXTENSIONS:
            raise ValueError("Format de document non pris en charge")
        if len(content) == 0:
            raise ValueError("Le document est vide")
        if len(content) > MAX_DOCUMENT_BYTES:
            raise ValueError("Le document dépasse la taille maximale de 10 Mo")
        detected = (
            "application/pdf" if content.startswith(b"%PDF-") else
            "image/png" if content.startswith(b"\x89PNG\r\n\x1a\n") else
            "image/jpeg" if content.startswith(b"\xff\xd8\xff") else None
        )
        declared = mime_type or mimetypes.guess_type(filename)[0]
        if declared not in SUPPORTED_MIME_TYPES or detected != declared:
            raise ValueError("Le type réel du document ne correspond pas au format déclaré")

    def upload(
        self,
        client_id: str,
        year: int,
        filename: str,
        mime_type: str,
        content: bytes,
        active_client_ice: Optional[str] = None,
    ) -> OcrDocument:
        if not client_id.strip():
            raise ValueError("Dossier actif obligatoire")
        if year < 2000 or year > 2100:
            raise ValueError("Exercice fiscal invalide")
        self._validate_file(filename, mime_type, content)
        checksum = hashlib.sha256(content).hexdigest()

        # Prefer PDF text, supplementing it with OCR only when fields are missing.
        extracted_data: Optional[ExtractedInvoiceData] = None
        safe_name = Path(filename).name
        if mime_type == "application/pdf":
            raw_text = extract_text_from_pdf(content)
            extracted_data = parse_invoice_text(raw_text, active_client_ice=active_client_ice)
            if any(getattr(extracted_data, field) is None for field in (
                "supplier", "ice", "invoice_number", "date", "ht", "vat", "vat_rate", "ttc"
            )):
                try:
                    ocr_text = _extract_text_from_pdf_images(content)
                except Exception:
                    ocr_text = ""
                    extracted_data = extracted_data.model_copy(update={
                        "warnings": list(dict.fromkeys([
                            *extracted_data.warnings,
                            "OCR complémentaire PDF indisponible; vérifiez les dépendances et le fichier.",
                        ]))
                    })
                if ocr_text:
                    ocr_data = parse_invoice_text(ocr_text, active_client_ice=active_client_ice)
                    extracted_data = _merge_missing_fields(extracted_data, ocr_data)
        else:
            raw_text = extract_text_from_image(content)
            extracted_data = parse_invoice_text(raw_text, active_client_ice=active_client_ice)

        extracted_json = (
            extracted_data.model_dump_json(by_alias=True) if extracted_data else None
        )

        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            matches = [row[0] for row in db.execute(
                "SELECT id FROM ocr_documents WHERE client_id=? AND checksum=? ORDER BY id",
                (client_id, checksum),
            )]
            status = "duplicate_suspected" if matches else "needs_review"
            if matches:
                row = db.execute(
                    "SELECT * FROM ocr_documents WHERE id=?", (matches[0],)
                ).fetchone()
                existing_extracted = _load_extracted_json(row["extracted_json"])

                if existing_extracted is None:
                    existing_extracted = extracted_data
                elif extracted_data is not None:
                    updates = {}
                    confidence = dict(existing_extracted.confidence)
                    field_confidence = (
                        ("supplier", "supplier"), ("ice", "ice"),
                        ("invoice_number", "invoiceNumber"), ("date", "date"),
                        ("ht", "ht"), ("vat", "vat"),
                        ("vat_rate", "vatRate"), ("ttc", "ttc"),
                        ("raw_text_snippet", "rawTextSnippet"),
                    )
                    for field, confidence_key in field_confidence:
                        new_value = getattr(extracted_data, field)
                        old_value = getattr(existing_extracted, field)
                        new_confidence = extracted_data.confidence.get(confidence_key, 0)
                        old_confidence = confidence.get(confidence_key, 0)
                        if new_value is not None and (
                            old_value is None or new_confidence > old_confidence
                        ):
                            updates[field] = new_value
                            if confidence_key in extracted_data.confidence:
                                confidence[confidence_key] = new_confidence

                    warnings = list(dict.fromkeys(
                        [*existing_extracted.warnings, *extracted_data.warnings]
                    ))
                    if confidence != existing_extracted.confidence:
                        updates["confidence"] = confidence
                    if warnings != existing_extracted.warnings:
                        updates["warnings"] = warnings
                    if updates:
                        existing_extracted = existing_extracted.model_copy(update=updates)

                refreshed_json = (
                    existing_extracted.model_dump_json(by_alias=True)
                    if existing_extracted else None
                )
                if refreshed_json != row["extracted_json"]:
                    db.execute(
                        "UPDATE ocr_documents SET extracted_json=? WHERE id=?",
                        (refreshed_json, row["id"]),
                    )

                return OcrDocument(
                    documentId=row["id"], clientId=row["client_id"], year=row["fiscal_year"],
                    filename=filename, mimeType=mime_type, size=len(content),
                    checksum=checksum, status=status, duplicateMatches=matches,
                    extractedData=existing_extracted,
                )
            try:
                cursor = db.execute(
                    """INSERT INTO ocr_documents
                       (client_id, fiscal_year, filename, mime_type, size, checksum, content,
                        status, duplicate_matches, uploaded_at, extracted_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (client_id, year, safe_name, mime_type, len(content), checksum,
                     content, status, ",".join(map(str, matches)), utc_now_iso(),
                     extracted_json),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError("Le document ne peut pas être conservé") from error
            document_id = int(cursor.lastrowid)
            return OcrDocument(
                documentId=document_id, clientId=client_id, year=year,
                filename=safe_name, mimeType=mime_type, size=len(content),
                checksum=checksum, status=status, duplicateMatches=matches,
                extractedData=extracted_data,
            )

    def get(
        self,
        document_id: int,
        client_id: str | None = None,
        year: int | None = None,
    ) -> OcrDocument | None:
        with closing(self._connect()) as db, db:
            if client_id is None or year is None:
                row = db.execute(
                    "SELECT * FROM ocr_documents WHERE id=?", (document_id,)
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM ocr_documents WHERE id=? AND client_id=? AND fiscal_year=?",
                    (document_id, client_id, year),
                ).fetchone()
        if row is None:
            return None

        return OcrDocument(
            documentId=row["id"], clientId=row["client_id"], year=row["fiscal_year"],
            filename=row["filename"], mimeType=row["mime_type"], size=row["size"],
            checksum=row["checksum"], status=row["status"],
            duplicateMatches=[int(value) for value in row["duplicate_matches"].split(",") if value],
            processingError=row["processing_error"],
            extractedData=_load_extracted_json(row["extracted_json"]),
        )
