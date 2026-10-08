from __future__ import annotations

import json

import pytest

from core import cgnc, journal_service
from core.journal_service import JournalLine, JournalEntryPost, JournalRepository
from core.pcge_import import extract_pcge_general_accounts, preview_import


def test_dataset_source_maps_codes_and_classes_as_standard_accounts(tmp_path):
    source = tmp_path / "cgnc.json"
    source.write_text(json.dumps([
        {"account_code": "1111", "label_fr": "Capital social", "class": 1, "status": "standard"},
        {"account_code": "6125", "label_fr": "Achats non stockés", "class": 6, "status": "standard"},
    ]), encoding="utf-8")

    accounts = extract_pcge_general_accounts(source)

    assert [(item["code"], item["class"]) for item in accounts] == [("1111", 1), ("6125", 6)]
    assert all(set(item) == {"code", "label", "class", "source"} for item in accounts)


def test_dataset_rejects_code_whose_class_does_not_match(tmp_path):
    source = tmp_path / "bad.json"
    source.write_text(json.dumps([{"account_code": "6125", "label_fr": "X", "class": 7, "status": "standard"}]))

    with pytest.raises(ValueError, match="Invalid CGNC account record"):
        extract_pcge_general_accounts(source)


def test_chart_is_the_single_cleaned_dataset():
    chart = cgnc.chart_of_accounts()
    standard = json.loads(cgnc.STANDARD_DATASET.read_text(encoding="utf-8"))
    labels = {item["code"]: item["label"] for item in chart}

    assert len(chart) == len(standard) == len(labels)
    assert {item["status"] for item in standard} == {"standard"}
    assert not (cgnc.PROJECT_ROOT / "cgnc_supplement_accounts.json").exists()
    assert {"3455", "4453", "4455"} <= labels.keys()
    assert labels["4456"] == "État, TVA due"
    assert labels["3424"] == "Clients douteux ou litigieux"
    assert "14525" not in labels and labels["44525"] == "Etat, PTS et PSN"
    assert all("|" not in label and " 2314." not in label for label in labels.values())
    assert all(code[:4] in labels for code in labels)


def test_dataset_rejects_sub_account_without_four_digit_parent(tmp_path):
    source = tmp_path / "orphan.json"
    source.write_text(json.dumps([{"account_code": "61251", "label_fr": "X", "class": 6, "status": "standard"}]))

    with pytest.raises(ValueError, match="without a 4-digit parent"):
        extract_pcge_general_accounts(source)


@pytest.mark.parametrize("code, parent, root", [
    ("6125", "6125", "6125"), ("61251", "6125", "61251"), ("44110002", "4411", "4411"),
    ("34210001", "3421", "3421"), ("345520", "3455", "34552"), ("3455220", "3455", "34552"),
    ("445520", "4455", "4455"), ("4453", "4453", "4453"),
    ("611", None, None), ("611100001", None, None), ("9999", None, None), ("61A1", None, None),
])
def test_codes_of_four_to_eight_digits_are_valid_under_their_four_digit_parent(code, parent, root):
    assert cgnc.cgnc_parent(code) == parent
    assert cgnc.cgnc_root(code) == root


def test_journal_line_schema_rejects_codes_outside_four_to_eight_digits():
    from pydantic import ValidationError

    for bad in ("611", "611100001", "61A1"):
        with pytest.raises(ValidationError):
            JournalLine(compte=bad, debit=10)
    assert JournalLine(compte="44110002", auxiliaire="44110002", credit=10).account == "44110002"


def test_posting_rejects_accounts_outside_the_cgnc_chart(tmp_path):
    repository = JournalRepository(tmp_path / "journal.sqlite3")
    request = JournalEntryPost(
        clientId="C001", year=2026, journal="OD", date="2026-01-05",
        accountCatalog=[{"code": "9999", "label": "Hors référentiel"}, {"code": "6125", "label": "Libellé local"}],
        lines=[JournalLine(compte="6125", debit=10), JournalLine(compte="9999", credit=10)],
    )

    with pytest.raises(ValueError, match="Unknown PCM account: 9999"):
        repository.post(request)
    repository.post(request.model_copy(update={"lines": [JournalLine(compte="6125", debit=10), JournalLine(compte="4453", credit=10)]}))
    with repository._connect() as db:
        assert db.execute("SELECT label FROM pcm_accounts WHERE code='6125'").fetchone()[0] == cgnc.official_label("6125")
        assert db.execute("SELECT COUNT(*) FROM pcm_accounts WHERE code='9999'").fetchone()[0] == 0


def test_preview_reports_missing_and_present_without_relabeling_existing():
    source = [
        {"code": "1111", "label": "Capital social", "class": 1, "source": "pcge_general"},
        {"code": "1112", "label": "Fonds de dotation", "class": 1, "source": "pcge_general"},
        {"code": "1117", "label": "Capital personnel", "class": 1, "source": "pcge_general"},
    ]

    # Exercise the pure comparison contract without reading or modifying a database.
    import core.pcge_import as pcge_import
    original = pcge_import.extract_pcge_general_accounts
    pcge_import.extract_pcge_general_accounts = lambda _path=None: source
    try:
        report = preview_import([{"code": "1111", "label": "Custom label"}])
    finally:
        pcge_import.extract_pcge_general_accounts = original

    assert [item["code"] for item in report["added"]] == ["1112", "1117"]
    assert report["alreadyPresent"][0]["currentLabel"] == "Custom label"
    assert "needsReview" not in report


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
