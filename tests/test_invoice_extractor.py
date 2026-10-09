from pathlib import Path
import pytest

from core.invoice_extractor import (
    clean_amount,
    parse_date,
    extract_text_from_pdf,
    parse_invoice_text,
)
from core.ocr_service import OcrDocumentStore
import core.ocr_service as ocr_service


SAMPLE_RECEIPT_PATH = Path(__file__).parent / "fixtures" / "kompta_sample_payment_receipt.pdf"
SAMPLE_INVOICE_PATH = Path(__file__).parent / "fixtures" / "FA048591.pdf"
FICTIVE_OCR_TEXT_PATH = Path(__file__).parent / "fixtures" / "facture_fictive_test_ocr.txt"
MOROCCAN_OCR_PDF_PATH = Path(__file__).parent / "fixtures" / "Facture_fictive_test_OCR.pdf"


class TestInvoiceExtractorHelpers:
    def test_clean_amount_variations(self):
        assert clean_amount("2 500,00 MAD") == 2500.00
        assert clean_amount(" 143,00 dhs ") == 143.00
        assert clean_amount("1,250.50") == 1250.50
        assert clean_amount("230.00") == 230.00
        assert clean_amount("0,00 MAD") == 0.0
        assert clean_amount("invalid") is None
        assert clean_amount("") is None

    def test_parse_date_variations(self):
        assert parse_date("23/09/2026") == "2026-09-23"
        assert parse_date("22-09-2026") == "2026-09-22"
        assert parse_date("27.07.2026") == "2026-07-27"
        assert parse_date("2026-09-22") == "2026-09-22"
        assert parse_date("no date") is None
        assert parse_date("") is None

    def test_extract_text_from_empty_or_corrupt_pdf(self):
        assert extract_text_from_pdf(b"") == ""
        assert extract_text_from_pdf(b"not a valid pdf binary") == ""


class TestInvoiceExtractionRealSamples:
    @pytest.mark.skipif(not SAMPLE_RECEIPT_PATH.exists(), reason="Sample receipt PDF not found")
    def test_extract_sample_payment_receipt(self):
        content = SAMPLE_RECEIPT_PATH.read_bytes()
        text = extract_text_from_pdf(content)
        assert "Atlas Fournitures SARL" in text

        # Disambiguate against active client's ICE (Kompta Services SARL ICE = 002345678901234)
        extracted = parse_invoice_text(text, active_client_ice="002345678901234")

        assert extracted.supplier == "Atlas Fournitures SARL"
        assert extracted.ice == "001234567890123"
        assert extracted.invoice_number == "FAC-2026-0197"
        assert extracted.date == "2026-09-22"
        assert extracted.ht == 2500.00
        assert extracted.vat == 500.00
        assert extracted.vat_rate == 20.0
        assert extracted.ttc == 3000.00
        assert len(extracted.warnings) == 0

    @pytest.mark.skipif(not SAMPLE_INVOICE_PATH.exists(), reason="Sample invoice PDF not found")
    def test_extract_sample_tcpdf_invoice(self):
        content = SAMPLE_INVOICE_PATH.read_bytes()
        text = extract_text_from_pdf(content)
        assert "FACTURE" in text

        extracted = parse_invoice_text(text)
        assert extracted.invoice_number == "FA048591"
        assert extracted.date == "2026-07-27"
        assert extracted.ht == 230.00
        assert extracted.vat == 0.00
        assert extracted.vat_rate == 0.00
        assert extracted.ttc == 230.00
        assert len(extracted.warnings) == 0


class TestArithmeticAndEdgeCases:
    def test_missing_vat_reconciliation(self):
        text = """
        FACTURE N° FAC-999
        Date: 15/05/2026
        Fournisseur
        STE TEST SARL
        ICE : 001122334455667
        Total (HT) : 1000,00 MAD
        Total (TTC) : 1200,00 MAD
        """
        extracted = parse_invoice_text(text)
        assert extracted.ht == 1000.00
        assert extracted.ttc == 1200.00
        assert extracted.vat == 200.00
        assert extracted.vat_rate == 20.0
        assert len(extracted.warnings) == 0

    def test_arithmetic_discrepancy_generates_warning(self):
        text = """
        Facture N° ERR-001
        Date: 10/01/2026
        Total HT : 1000,00
        TVA : 100,00
        Total TTC : 1500,00
        """
        extracted = parse_invoice_text(text)
        assert len(extracted.warnings) > 0
        assert "Incohérence arithmétique" in extracted.warnings[0]


class TestMoroccanInvoiceAmountParsing:
    def test_actual_moroccan_invoice_pdf_extracts_and_persists_iso_date(self, tmp_path):
        store = OcrDocumentStore(tmp_path / "moroccan-invoice.sqlite3")
        content = MOROCCAN_OCR_PDF_PATH.read_bytes()
        document = store.upload("C-DEMO", 2026, MOROCCAN_OCR_PDF_PATH.name, "application/pdf", content)
        extracted = document.extracted_data
        saved = store.get(document.document_id, "C-DEMO", 2026)

        assert extracted is not None
        assert extracted.supplier == "Atlas Bureautique Demo SARL"
        assert extracted.ice == "000000000000000"
        assert extracted.invoice_number == "TEST-OCR-2026-001"
        assert extracted.date == "2026-09-15"
        assert extracted.ht == 1000.00
        assert extracted.vat == 200.00
        assert extracted.vat_rate == 20.0
        assert extracted.ttc == 1200.00
        assert saved is not None
        assert saved.extracted_data is not None
        assert saved.extracted_data.date == "2026-09-15"

    def test_duplicate_upload_refreshes_fields_missing_from_older_extraction(self, tmp_path):
        store = OcrDocumentStore(tmp_path / "stale-ocr.sqlite3")
        content = MOROCCAN_OCR_PDF_PATH.read_bytes()
        original = store.upload(
            "C-DEMO", 2026, MOROCCAN_OCR_PDF_PATH.name, "application/pdf", content
        )
        stale = original.extracted_data.model_copy(update={
            "ht": None,
            "vat": None,
            "vat_rate": None,
            "confidence": {
                key: value for key, value in original.extracted_data.confidence.items()
                if key not in {"ht", "vat", "vatRate"}
            },
        })
        with store._connect() as db:
            db.execute(
                "UPDATE ocr_documents SET extracted_json=? WHERE id=?",
                (stale.model_dump_json(by_alias=True), original.document_id),
            )

        duplicate = store.upload(
            "C-DEMO", 2026, MOROCCAN_OCR_PDF_PATH.name, "application/pdf", content
        )
        saved = store.get(original.document_id, "C-DEMO", 2026)

        assert duplicate.status == "duplicate_suspected"
        assert duplicate.document_id == original.document_id
        assert duplicate.extracted_data is not None
        assert duplicate.extracted_data.ht == 1000.00
        assert duplicate.extracted_data.vat == 200.00
        assert duplicate.extracted_data.vat_rate == 20.0
        assert saved is not None
        assert saved.extracted_data is not None
        assert saved.extracted_data.ht == 1000.00
        assert saved.extracted_data.vat == 200.00
        assert saved.extracted_data.vat_rate == 20.0

    def test_parses_amounts_from_the_fictive_pdf_extraction(self):
        text = FICTIVE_OCR_TEXT_PATH.read_text(encoding="utf-8")

        extracted = parse_invoice_text(text)

        assert extracted.supplier == "Atlas Bureautique Demo SARL"
        assert extracted.ice == "000000000000000"
        assert extracted.invoice_number == "TEST-OCR-2026-001"
        assert extracted.date == "2026-09-15"
        assert extracted.ht == 1000.00
        assert extracted.vat == 200.00
        assert extracted.vat_rate == 20.0
        assert extracted.ttc == 1200.00

    @pytest.mark.parametrize(
        "text, expected_ht, expected_vat, expected_rate, expected_ttc",
        [
            (
                "Total hors taxes (HT): 1.000,00 MAD\n"
                "TVA (20 %): 200,00 MAD\nTotal TTC: 1.200,00 MAD",
                1000.00, 200.00, 20.0, 1200.00,
            ),
            (
                "Montant HT | 1,000.00 DH\n"
                "TVA 20 % | 200.00 DH\nNet à payer | 1,200.00 DH",
                1000.00, 200.00, 20.0, 1200.00,
            ),
            (
                "Total HT\n1\u202f000,00 MAD\nTVA 20 %\n200,00 MAD\n"
                "Total TTC\n1\u202f200,00 MAD",
                1000.00, 200.00, 20.0, 1200.00,
            ),
        ],
    )
    def test_parses_inline_split_and_localized_table_amounts(
        self, text, expected_ht, expected_vat, expected_rate, expected_ttc
    ):
        extracted = parse_invoice_text(text)

        assert extracted.ht == expected_ht
        assert extracted.vat == expected_vat
        assert extracted.vat_rate == expected_rate
        assert extracted.ttc == expected_ttc


class TestOcrDocumentStoreIntegration:
    def test_png_is_ocr_extracted_parsed_and_retained_for_review(self, tmp_path, monkeypatch):
        recognized_lines = (
            "FOURNISSEUR Atlas Bureautique Demo SARL",
            "ICE 123456789012345",
            "N° facture : TEST-PNG-2026-001",
            "Date facture 15/09/2026",
            "Total hors taxes (HT)",
            "1 000,00 MAD",
            "TVA 20 %",
            "200,00 MAD",
            "TOTAL TTC",
            "1 200,00 MAD",
        )

        class FakeImageOcr:
            def __call__(self, image):
                assert image is decoded_image
                return type("OcrResult", (), {"txts": recognized_lines})()

        decoded_image = object()
        monkeypatch.setattr(ocr_service, "_IMAGE_OCR_ENGINE", FakeImageOcr())
        monkeypatch.setattr(ocr_service, "_decode_image", lambda content: decoded_image)
        store = OcrDocumentStore(tmp_path / "png-ocr.sqlite3")
        png_content = b"\x89PNG\r\n\x1a\nsynthetic image payload"

        document = store.upload(
            "C001", 2026, "facture.png", "image/png", png_content,
            active_client_ice="999999999999999",
        )
        saved = store.get(document.document_id, "C001", 2026)

        assert document.status == "needs_review"
        assert document.extracted_data is not None
        assert document.extracted_data.supplier == "Atlas Bureautique Demo SARL"
        assert document.extracted_data.ice == "123456789012345"
        assert document.extracted_data.invoice_number == "TEST-PNG-2026-001"
        assert document.extracted_data.date == "2026-09-15"
        assert document.extracted_data.ht == 1000.00
        assert document.extracted_data.vat == 200.00
        assert document.extracted_data.vat_rate == 20.0
        assert document.extracted_data.ttc == 1200.00
        assert saved is not None
        assert saved.extracted_data == document.extracted_data

    def test_pdf_ocr_fills_fields_missing_from_selectable_text(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            ocr_service,
            "extract_text_from_pdf",
            lambda content: "Fournisseur Atlas Bureautique Demo SARL\nICE 123456789012345",
        )
        monkeypatch.setattr(
            ocr_service,
            "_extract_text_from_pdf_images",
            lambda content: (
                "N° facture : TEST-PDF-2026-001\nDate facture 15/09/2026\n"
                "Total HT : 1 000,00 MAD\nTVA 20 % : 200,00 MAD\nTotal TTC : 1 200,00 MAD"
            ),
        )
        store = OcrDocumentStore(tmp_path / "pdf-ocr.sqlite3")

        document = store.upload(
            "C001", 2026, "facture.pdf", "application/pdf", b"%PDF-synthetic"
        )

        assert document.extracted_data is not None
        assert document.extracted_data.supplier == "Atlas Bureautique Demo SARL"
        assert document.extracted_data.ice == "123456789012345"
        assert document.extracted_data.invoice_number == "TEST-PDF-2026-001"
        assert document.extracted_data.date == "2026-09-15"
        assert document.extracted_data.ht == 1000.00
        assert document.extracted_data.vat == 200.00
        assert document.extracted_data.ttc == 1200.00

    def test_upload_extracts_and_persists_invoice_data(self, tmp_path):
        store = OcrDocumentStore(tmp_path / "ocr.sqlite3")
        if not SAMPLE_RECEIPT_PATH.exists():
            pytest.skip("Sample receipt not found")

        content = SAMPLE_RECEIPT_PATH.read_bytes()
        doc = store.upload("C001", 2026, "recu.pdf", "application/pdf", content)

        assert doc.status == "needs_review"
        assert doc.extracted_data is not None
        assert doc.extracted_data.supplier == "Atlas Fournitures SARL"
        assert doc.extracted_data.ht == 2500.00
        assert doc.extracted_data.ttc == 3000.00

        # Retrieve and verify persistence
        loaded = store.get(doc.document_id)
        assert loaded is not None
        assert loaded.extracted_data is not None
        assert loaded.extracted_data.invoice_number == "FAC-2026-0197"
        assert loaded.extracted_data.ice == "001234567890123"

    def test_duplicate_upload_preserves_extraction_and_flags_duplicate(self, tmp_path):
        store = OcrDocumentStore(tmp_path / "ocr.sqlite3")
        if not SAMPLE_RECEIPT_PATH.exists():
            pytest.skip("Sample receipt not found")

        content = SAMPLE_RECEIPT_PATH.read_bytes()
        orig = store.upload("C001", 2026, "recu1.pdf", "application/pdf", content)
        assert orig.status == "needs_review"

        dup = store.upload("C001", 2026, "recu2.pdf", "application/pdf", content)
        assert dup.status == "duplicate_suspected"
        assert dup.document_id == orig.document_id
        assert dup.extracted_data is not None
        assert dup.extracted_data.ht == 2500.00

