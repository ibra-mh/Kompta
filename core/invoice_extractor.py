"""Invoice text extraction and rule-based parsing engine for Kompta.

Extracts text from PDF documents using pypdf and applies robust heuristics
to detect Moroccan invoice metadata:
- Supplier name
- Moroccan ICE (15 digits)
- Invoice reference / number
- Invoice date (normalized to YYYY-MM-DD)
- Financial amounts: HT, TVA, TVA rate, TTC with arithmetic verification
"""
from __future__ import annotations

import io
import re
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field
from pypdf import PdfReader


STANDARD_MOROCCAN_VAT_RATES = {0.0, 7.0, 10.0, 14.0, 20.0}
DATE_TOKEN = r"([0-9]{1,2}[\/\-\.][0-9]{1,2}[\/\-\.][0-9]{4}|[0-9]{4}[\/\-\.][0-9]{1,2}[\/\-\.][0-9]{1,2})"
DUE_DATE_PATTERN = (
    r"(?i)(?:date\s+d['’]\s*[ée]ch[ée]ance|[ée]ch[ée]ance|date\s+limite(?:\s+de\s+(?:paiement|r[èe]glement))?"
    r"|[àa]\s+payer\s+avant\s+le|payable\s+(?:avant\s+)?le)\s*[:#\.\s]*\n?" + DATE_TOKEN
)


class ExtractedInvoiceData(BaseModel):
    supplier: Optional[str] = None
    ice: Optional[str] = None
    invoice_number: Optional[str] = Field(None, alias="invoiceNumber")
    date: Optional[str] = None  # Normalized to YYYY-MM-DD
    due_date: Optional[str] = Field(None, alias="dueDate")  # Explicit "date d'échéance" only
    ht: Optional[float] = None
    vat: Optional[float] = None
    vat_rate: Optional[float] = Field(None, alias="vatRate")
    ttc: Optional[float] = None
    raw_text_snippet: Optional[str] = Field(None, alias="rawTextSnippet")
    confidence: dict[str, float] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


def extract_text_from_pdf(content: bytes) -> str:
    """Extract all text lines from a PDF binary stream safely."""
    if not content:
        return ""
    try:
        reader = PdfReader(io.BytesIO(content))
        text_parts = []
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)
        return "\n".join(text_parts).strip()
    except Exception:
        return ""


def clean_amount(val_str: str) -> Optional[float]:
    """Parse Moroccan / French currency string into float.
    
    Examples:
    '2 500,00 MAD' -> 2500.00
    '143,00' -> 143.00
    '230.00' -> 230.00
    """
    if not val_str:
        return None
    cleaned = re.sub(r"(?i)\b(?:MAD|DHS?)\b", "", val_str).strip()
    # Remove any extra surrounding non-digit characters except decimals/thousands
    match = re.search(r"[-+]?\d[\d\s.,]*\d|\d", cleaned)
    if not match:
        return None
    num_str = re.sub(r"\s+", "", match.group(0))

    if "," in num_str and "." in num_str:
        if num_str.rfind(",") > num_str.rfind("."):
            num_str = num_str.replace(".", "").replace(",", ".")
        else:
            num_str = num_str.replace(",", "")
    elif "," in num_str:
        whole, fraction = num_str.rsplit(",", 1)
        if len(fraction) == 3 and len(whole.lstrip("+-")) <= 3:
            num_str = whole + fraction
        else:
            num_str = whole + "." + fraction
    elif "." in num_str:
        parts = num_str.split(".")
        if len(parts) > 2 or (len(parts) == 2 and len(parts[1]) == 3 and len(parts[0].lstrip("+-")) <= 3):
            num_str = "".join(parts)

    try:
        val = float(num_str)
        return round(val, 2)
    except ValueError:
        return None


def _parse_amount_cell(value: str) -> Optional[float]:
    match = re.fullmatch(
        r"\s*([-+]?\d(?:[\d\s.,]*\d)?)\s*(?:MAD|DHS?)?\s*",
        value,
        re.IGNORECASE,
    )
    return clean_amount(match.group(1)) if match else None


def _amount_after_label(lines: list[str], patterns: list[str]) -> Optional[float]:
    for pattern in patterns:
        for index, line in enumerate(lines):
            match = re.search(pattern, line, re.IGNORECASE)
            if not match:
                continue
            suffix = line[match.end():].strip().lstrip(":#|-= ").strip()
            amount = _parse_amount_cell(suffix)
            if amount is not None:
                return amount
            if not suffix and index + 1 < len(lines):
                amount = _parse_amount_cell(lines[index + 1])
                if amount is not None:
                    return amount
    return None


def parse_date(date_str: str) -> Optional[str]:
    """Normalize various date formats (DD/MM/YYYY, DD-MM-YYYY, YYYY-MM-DD) to YYYY-MM-DD."""
    if not date_str:
        return None
    date_str = date_str.strip()
    patterns = [
        ("%d/%m/%Y", r"\b\d{1,2}/\d{1,2}/\d{4}\b"),
        ("%d-%m-%Y", r"\b\d{1,2}-\d{1,2}-\d{4}\b"),
        ("%d.%m.%Y", r"\b\d{1,2}\.\d{1,2}\.\d{4}\b"),
        ("%Y-%m-%d", r"\b\d{4}-\d{1,2}-\d{1,2}\b"),
    ]
    for fmt, regex in patterns:
        m = re.search(regex, date_str)
        if m:
            matched = m.group(0)
            try:
                dt = datetime.strptime(matched, fmt)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue
    return None


def parse_invoice_text(text: str, active_client_ice: Optional[str] = None) -> ExtractedInvoiceData:
    """Parse invoice fields from raw document text using Moroccan accounting rules."""
    warnings: list[str] = []
    confidence: dict[str, float] = {}

    if not text or not text.strip():
        return ExtractedInvoiceData(
            raw_text_snippet="",
            warnings=["Aucun texte vectoriel extrait du document (document scanné ou image)."],
        )

    lines = [line.strip() for line in text.splitlines() if line.strip()]

    # 1. Supplier Extraction
    supplier: Optional[str] = None
    # Look for "Émetteur" / "Fournisseur" blocks
    for idx, line in enumerate(lines):
        norm = line.lower()
        inline_supplier = re.match(
            r"^[^\w]*(?:émetteur|emetteur|fournisseur|société|vendeur)"
            r"\s*[:\-–]?\s+(.+?)\s*$",
            line,
            re.IGNORECASE,
        )
        if inline_supplier:
            candidate = inline_supplier.group(1).strip()
            if candidate and not re.search(
                r"\b(?:fictif|document|sans\s+valeur|test)\b", candidate, re.IGNORECASE
            ):
                supplier = candidate
                confidence["supplier"] = 0.9
                break
        if re.search(r"^[^\w]*(?:émetteur|emetteur|fournisseur|société|vendeur)[^\w]*$", norm):
            # The next non-empty line is likely the company name
            if idx + 1 < len(lines):
                candidate = lines[idx + 1]
                if not re.search(r"^(?:ice|if|rc|patente|date|client|tel|adresse)", candidate, re.IGNORECASE):
                    supplier = candidate
                    confidence["supplier"] = 0.9
                    break

    # Fallback: search for known corporate forms in the upper half of document
    if not supplier:
        for line in lines[: min(len(lines), 15)]:
            if re.search(r"\b(?:SARL|S\.A\.R\.L|SA|S\.A|STE|AU|SNC)\b", line, re.IGNORECASE):
                # Avoid client labels
                if not re.search(r"client|destinataire|facturé à|adresse", line, re.IGNORECASE):
                    supplier = line
                    confidence["supplier"] = 0.7
                    break

    # 2. ICE Extraction (15 consecutive digits)
    ice: Optional[str] = None
    ice_matches = re.findall(r"\b(\d{15})\b", text)
    if ice_matches:
        # Disambiguate against active client's ICE if known
        candidates = [c for c in ice_matches if c != active_client_ice]
        if not candidates:
            candidates = ice_matches

        # Prioritize ICE associated with Émetteur / Fournisseur
        for candidate in candidates:
            pattern = rf"(?:émetteur|emetteur|fournisseur|société)[\s\S]{{0,150}}?{candidate}"
            if re.search(pattern, text, re.IGNORECASE):
                ice = candidate
                confidence["ice"] = 0.95
                break

        if not ice:
            ice = candidates[0]
            confidence["ice"] = 0.85

    # 3. Invoice Number Extraction
    invoice_number: Optional[str] = None
    # Check for direct labeled references
    inv_patterns = [
        r"(?i)(?:référence\s+facture|ref(?:\.|érence)?\s+facture|numéro\s+de\s+facture|n°\s+de\s+facture|n°\s*facture|facture\s*n°)\s*[:#\.\s]*\n?([A-Z0-9_\-\/]{3,30})",
        r"(?i)facture\s*\n?(?:[0-9]{1,2}[\/\-\.][0-9]{1,2}[\/\-\.][0-9]{4}\s*\n?)?#([A-Z0-9_\-]{4,25})",
        r"#([A-Z0-9_\-]{5,25})",
        r"(?i)(?:reçu\s+n°|recu\s+n°)\s*[:#\.\s]*\n?([A-Z0-9_\-\/]{3,30})",
    ]
    REJECTED_INV_WORDS = {
        "DATE", "FACTURATION", "COMMANDE", "TOTAL", "CLIENT", "FOURNISSEUR",
        "POUR", "MAROC", "PAGE", "TEL", "FACTURE", "AVEC", "SANS", "REF", "HT", "TTC", "TVA"
    }
    for p in inv_patterns:
        for m in re.finditer(p, text):
            val = m.group(1).strip()
            clean_val = val.lstrip("#").strip()
            # Exclude false positives like dates or pure labels
            if clean_val.upper() not in REJECTED_INV_WORDS and not parse_date(clean_val) and len(clean_val) >= 3:
                invoice_number = clean_val
                confidence["invoiceNumber"] = 0.9
                break
        if invoice_number:
            break

    # Fallback: scan for standard invoice prefix patterns (e.g. FAC-2026-0197, FA048591)
    if not invoice_number:
        m = re.search(r"\b(FAC-[A-Z0-9\-]+|FA\d{5,10}|INV-[A-Z0-9\-]+)\b", text)
        if m:
            invoice_number = m.group(1)
            confidence["invoiceNumber"] = 0.8

    # 4. Date Extraction
    date_val: Optional[str] = None
    date_patterns = [
        r"(?i)(?:date\s+de\s+facturation|date\s+de\s+facture|date\s+facture|facturé\s+le)\s*[:#\.\s]*\n?([0-9]{1,2}[\/\-\.][0-9]{1,2}[\/\-\.][0-9]{4}|[0-9]{4}[\/\-\.][0-9]{1,2}[\/\-\.][0-9]{1,2})",
        r"(?i)\bdate\s*[:#\.\s]*\n?([0-9]{1,2}[\/\-\.][0-9]{1,2}[\/\-\.][0-9]{4}|[0-9]{4}[\/\-\.][0-9]{1,2}[\/\-\.][0-9]{1,2})",
    ]
    for p in date_patterns:
        m = re.search(p, text)
        if m:
            d = parse_date(m.group(1))
            if d:
                date_val = d
                confidence["date"] = 0.9
                break

    if not date_val:
        # Fallback to first valid date in text
        d = parse_date(text)
        if d:
            date_val = d
            confidence["date"] = 0.7

    due_date: Optional[str] = None
    due_match = re.search(DUE_DATE_PATTERN, text)
    if due_match:
        due_date = parse_date(due_match.group(1))
        if due_date:
            confidence["dueDate"] = 0.9

    # 5. Amounts (HT, TVA, VAT Rate, TTC)
    ht: Optional[float] = None
    vat: Optional[float] = None
    vat_rate: Optional[float] = None
    ttc: Optional[float] = None

    # Check for "Pas de taxes" / "Exonéré"
    if re.search(r"(?i)(?:pas\s+de\s+taxes|exonéré|exonere|tva\s+0%|taux\s+0%)", text):
        vat = 0.0
        vat_rate = 0.0
        confidence["vat"] = 0.95
        confidence["vatRate"] = 0.95

    # Rate extraction: e.g. TVA (20 %), Taux de taxe 20 %
    if vat_rate is None:
        rate_match = re.search(
            r"(?i)(?:taux\s*(?:de\s*)?(?:tva|taxe)|tva)\s*[:#\.\s]*"
            r"(?:\(\s*)?([0-9]{1,2}(?:[.,][0-9]{1,2})?)\s*%",
            text,
        )
        if rate_match:
            try:
                parsed_rate = float(rate_match.group(1).replace(",", "."))
                vat_rate = parsed_rate
                confidence["vatRate"] = 0.85
            except ValueError:
                pass

    # HT extraction
    ht_patterns = [
        r"total\s+hors\s+taxes(?:\s*\(?ht\)?)?",
        r"montant\s+hors\s+taxes(?:\s*\(?ht\)?)?",
        r"montant\s+total\s*\(?ht\)?",
        r"montant\s*\(?ht\)?",
        r"base\s*\(?ht\)?",
        r"total\s*\(?ht\)?",
        r"total\s+produits",
        r"prix\s+de\s+base",
    ]
    ht = _amount_after_label(lines, ht_patterns)
    if ht is not None and ht > 0:
        confidence["ht"] = 0.9
    else:
        ht = None

    # TVA extraction
    if vat is None:
        vat_patterns = [
            r"montant\s+tva",
            r"taxe\s+totale",
            r"tva(?:\s*\(\s*\d{1,2}(?:[.,]\d{1,2})?\s*%\s*\)|\s+\d{1,2}(?:[.,]\d{1,2})?\s*%)?",
        ]
        vat = _amount_after_label(lines, vat_patterns)
        if vat is not None and vat >= 0:
            confidence["vat"] = 0.9

    # TTC extraction
    ttc_patterns = [
        r"montant\s+total\s*\(?ttc\)?",
        r"total\s*\(?ttc\)?",
        r"montant\s*\(ttc\)",
        r"total\s+ttc",
        r"net\s+à\s+payer",
        r"total\s+général",
        r"\btotal\b",
    ]
    ttc = _amount_after_label(lines, ttc_patterns)
    if ttc is not None and ttc > 0:
        confidence["ttc"] = 0.9
    else:
        ttc = None

    # 6. Arithmetic Reconciliation & Consistency Checks
    if ht is not None and vat is not None and ttc is None:
        ttc = round(ht + vat, 2)
        confidence["ttc"] = min(confidence.get("ht", 0.5), confidence.get("vat", 0.5))
    elif ht is not None and ttc is not None and vat is None:
        if ttc >= ht:
            vat = round(ttc - ht, 2)
            confidence["vat"] = min(confidence.get("ht", 0.5), confidence.get("ttc", 0.5))
    elif vat is not None and vat_rate is not None and vat_rate > 0 and ht is None:
        ht = round(vat / (vat_rate / 100), 2)
        ttc = round(ht + vat, 2)
        confidence["ht"] = 0.7
        confidence["ttc"] = 0.7
    elif ht is not None and vat_rate is not None and vat is None:
        vat = round(ht * (vat_rate / 100), 2)
        ttc = round(ht + vat, 2)
        confidence["vat"] = 0.75
        confidence["ttc"] = 0.75

    # Check arithmetic consistency: HT + TVA == TTC
    if ht is not None and vat is not None and ttc is not None:
        discrepancy = abs(round(ht + vat, 2) - round(ttc, 2))
        if discrepancy > 0.05:
            warnings.append(
                f"Incohérence arithmétique détectée : HT ({ht:.2f}) + TVA ({vat:.2f}) != TTC ({ttc:.2f})"
            )
        # Infer rate if missing
        if vat_rate is None and ht > 0:
            calc_rate = round((vat / ht) * 100, 1)
            # Match standard rate if within 0.2%
            for std in STANDARD_MOROCCAN_VAT_RATES:
                if abs(calc_rate - std) <= 0.2:
                    vat_rate = std
                    confidence["vatRate"] = 0.8
                    break

    return ExtractedInvoiceData(
        supplier=supplier,
        ice=ice,
        invoiceNumber=invoice_number,
        date=date_val,
        dueDate=due_date,
        ht=ht,
        vat=vat,
        vatRate=vat_rate,
        ttc=ttc,
        rawTextSnippet=text[:400],
        confidence=confidence,
        warnings=warnings,
    )
