from __future__ import annotations

import json

import pytest

from core import cgnc, journal_service
from core.journal_service import JournalLine, JournalEntryPost, JournalRepository
from core.pcge_import import extract_pcge_general_accounts, preview_import


def test_dataset_source_maps_codes_classes_and_review_status(tmp_path):
    source = tmp_path / "cgnc.json"
    source.write_text(json.dumps([
        {"account_code": "1111", "label_fr": "Capital social", "class": 1, "status": "standard"},
        {"account_code": "6125", "label_fr": "Achats non stockés", "class": 6, "status": "needs_review"},
    ]), encoding="utf-8")

    accounts = extract_pcge_general_accounts(source)

    assert [(item["code"], item["class"]) for item in accounts] == [("1111", 1), ("6125", 6)]
    assert "needs_review" not in accounts[0]
    assert accounts[1]["review_reason"] == "cgnc_dataset_status"


def test_dataset_rejects_code_whose_class_does_not_match(tmp_path):
    source = tmp_path / "bad.json"
    source.write_text(json.dumps([{"account_code": "6125", "label_fr": "X", "class": 7, "status": "standard"}]))

    with pytest.raises(ValueError, match="Invalid CGNC account record"):
        extract_pcge_general_accounts(source)


def test_chart_is_standard_dataset_plus_documented_supplement():
    chart = cgnc.chart_of_accounts()
    standard = json.loads(cgnc.STANDARD_DATASET.read_text(encoding="utf-8"))
    supplement = json.loads(cgnc.SUPPLEMENT_DATASET.read_text(encoding="utf-8"))

    assert len(chart) == len(standard) + len(supplement)
    assert {item["code"] for item in chart if item["source"] == "cgnc_supplement"} == {"3455", "4455"}
    assert cgnc.official_label("4411") == next(i["label_fr"] for i in standard if i["account_code"] == "4411")


@pytest.mark.parametrize("code, root", [
    ("6125", "6125"), ("44110002", "4411"), ("34210001", "3421"),
    ("3455220", "34552"), ("445520", "4455"), ("9999", None), ("4453", None), ("61A", None),
])
def test_accounts_are_valid_when_listed_or_extending_a_listed_code(code, root):
    assert cgnc.cgnc_root(code) == root


def test_posting_rejects_accounts_outside_the_cgnc_chart(tmp_path):
    repository = JournalRepository(tmp_path / "journal.sqlite3")
    request = JournalEntryPost(
        clientId="C001", year=2026, journal="OD", date="2026-01-05",
        accountCatalog=[{"code": "4453", "label": "Hors référentiel"}, {"code": "6125", "label": "Libellé local"}],
        lines=[JournalLine(compte="6125", debit=10), JournalLine(compte="4453", credit=10)],
    )

    with pytest.raises(ValueError, match="Unknown PCM account: 4453"):
        repository.post(request)
    repository.post(request.model_copy(update={"lines": [JournalLine(compte="6125", debit=10), JournalLine(compte="5141", credit=10)]}))
    with repository._connect() as db:
        assert db.execute("SELECT label FROM pcm_accounts WHERE code='6125'").fetchone()[0] == cgnc.official_label("6125")
        assert db.execute("SELECT COUNT(*) FROM pcm_accounts WHERE code='4453'").fetchone()[0] == 0


def test_preview_reports_missing_present_and_review_without_relabeling_existing():
    source = [
        {"code": "1111", "label": "Capital social", "class": 1, "source": "pcge_general"},
        {"code": "1112", "label": "Fonds de dotation", "class": 1, "source": "pcge_general"},
        {"code": "1117", "label": "Capital personnel", "class": 1, "source": "pcge_general", "needs_review": True, "review_reason": "source_text_encoding"},
    ]

    # Exercise the pure comparison contract without reading or modifying a database.
    import core.pcge_import as pcge_import
    original = pcge_import.extract_pcge_general_accounts
    pcge_import.extract_pcge_general_accounts = lambda _path=None: source
    try:
        report = preview_import([{"code": "1111", "label": "Custom label"}])
    finally:
        pcge_import.extract_pcge_general_accounts = original

    assert [item["code"] for item in report["added"]] == ["1112"]
    assert report["alreadyPresent"][0]["currentLabel"] == "Custom label"
    assert [item["code"] for item in report["needsReview"]] == ["1117"]


def test_import_is_idempotent_and_preserves_existing_catalog_and_entries(tmp_path, monkeypatch):
    source = [
        {"code": "1111", "label": "Capital social", "class": 1, "source": "pcge_general"},
        {"code": "1112", "label": "Fonds de dotation", "class": 1, "source": "pcge_general"},
    ]
    monkeypatch.setattr(journal_service, "extract_pcge_general_accounts", lambda _path=None: source)
    import core.pcge_import as pcge_import
    monkeypatch.setattr(pcge_import, "extract_pcge_general_accounts", lambda _path=None: source)
    monkeypatch.setattr(journal_service, "preview_import", preview_import)

    repository = JournalRepository(tmp_path / "pcge.sqlite3")
    with repository._connect() as db:
        db.execute("INSERT INTO pcm_accounts(code, label, account_type, updated_at) VALUES ('1111', 'Custom label', 'parent', 'now')")
        db.execute("INSERT INTO journal_entries(client_id, year, entry_number, piece_number, journal, entry_date, user_id, posted_at) VALUES ('C001', 2026, 1, 'OD-000001', 'OD', '2026-01-01', 'user', 'now')")

    first = repository.import_pcge_general()
    second = repository.import_pcge_general()

    assert [item["code"] for item in first["imported"]] == ["1112"]
    assert second["imported"] == []
    with repository._connect() as db:
        assert db.execute("SELECT label FROM pcm_accounts WHERE code='1111'").fetchone()[0] == "Custom label"
        assert db.execute("SELECT COUNT(*) FROM pcm_accounts WHERE code='1112'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 1
