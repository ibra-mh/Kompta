import sqlite3

import pytest
from pydantic import ValidationError

from core.client_service import ClientRepository, ClientUpsert, FiscalYearUpsert


def client_request(name="SARL Atlas", ice="123456789012345"):
    return ClientUpsert(
        name=name,
        ice=ice,
        legalForm="SARL",
        tvaRegime="Débit",
        tvaPeriodicite="Mensuelle",
    )


def test_client_and_fiscal_year_round_trip(tmp_path):
    repository = ClientRepository(tmp_path / "kompta.sqlite3")

    created = repository.create_client(client_request())
    assert "isDemo" not in created
    assert created["years"] == []

    saved_year = repository.save_fiscal_year(created["id"], FiscalYearUpsert(year=2026, status="open"))
    assert saved_year == {"clientId": created["id"], "year": 2026, "status": "open"}

    updated = repository.update_client(created["id"], client_request("SARL Modifiée"))
    assert updated["name"] == "SARL Modifiée"
    assert updated["years"] == [{"year": 2026, "status": "open"}]

    assert repository.list_clients() == [updated]

    restarted_repository = ClientRepository(tmp_path / "kompta.sqlite3")
    assert restarted_repository.list_clients() == [updated]


def test_fiscal_year_save_is_upsert(tmp_path):
    repository = ClientRepository(tmp_path / "kompta.sqlite3")
    created = repository.create_client(client_request())

    repository.save_fiscal_year(created["id"], FiscalYearUpsert(year=2026, status="open"))
    repository.save_fiscal_year(created["id"], FiscalYearUpsert(year=2026, status="closed"))

    assert repository.get_client(created["id"])["years"] == [{"year": 2026, "status": "closed"}]


def test_all_clients_are_listed_alphabetically(tmp_path):
    repository = ClientRepository(tmp_path / "kompta.sqlite3")
    for index, name in enumerate(["Zineb SARL", "atlas Négoce", "Brahim & Fils"]):
        repository.create_client(client_request(name, ice=f"00000000000000{index}"))

    assert [client["name"] for client in repository.list_clients()] == ["atlas Négoce", "Brahim & Fils", "Zineb SARL"]


def test_duplicate_ice_is_rejected_without_overwriting_existing_client(tmp_path):
    repository = ClientRepository(tmp_path / "kompta.sqlite3")
    first = repository.create_client(client_request("Premier client"))

    with pytest.raises(sqlite3.IntegrityError):
        repository.create_client(client_request("Deuxième client"))

    assert repository.get_client(first["id"])["name"] == "Premier client"
    assert len(repository.list_clients()) == 1


def test_invalid_client_and_fiscal_year_data_is_rejected(tmp_path):
    with pytest.raises(ValidationError):
        ClientUpsert(name="", legalForm="SARL")
    with pytest.raises(ValidationError):
        ClientUpsert(name="Client", ice="123", legalForm="SARL")
    with pytest.raises(ValidationError):
        FiscalYearUpsert(year=1999)
    with pytest.raises(ValidationError):
        FiscalYearUpsert(year=2026, status="invalid")
    with pytest.raises(ValidationError, match="8 chiffres"):
        ClientUpsert(name="Client", identifiantFiscal="IF001", legalForm="SARL")
    with pytest.raises(ValidationError):
        ClientUpsert(name="Client", legalForm="SARL", fiscalYear=1999)


def test_client_creation_stores_if_and_opens_first_exercise_atomically(tmp_path):
    repository = ClientRepository(tmp_path / "kompta.sqlite3")

    created = repository.create_client(ClientUpsert(
        name="SARL Nouvelle", ice="123456789012345", identifiantFiscal="12345678",
        legalForm="SARL", fiscalYear=2026,
    ))

    assert created["identifiantFiscal"] == "12345678"
    assert created["years"] == [{"year": 2026, "status": "open"}]
    assert repository.list_clients() == [created]


def test_update_with_existing_exercise_keeps_its_status(tmp_path):
    repository = ClientRepository(tmp_path / "kompta.sqlite3")
    created = repository.create_client(client_request())
    repository.save_fiscal_year(created["id"], FiscalYearUpsert(year=2025, status="closed"))

    updated = repository.update_client(created["id"], ClientUpsert(
        name="SARL Atlas", ice="123456789012345", legalForm="SARL", fiscalYear=2025,
    ))

    assert updated["years"] == [{"year": 2025, "status": "closed"}]


def test_failed_creation_leaves_no_partial_exercise(tmp_path):
    repository = ClientRepository(tmp_path / "kompta.sqlite3")
    repository.create_client(client_request("Premier client"))

    with pytest.raises(sqlite3.IntegrityError):
        repository.create_client(ClientUpsert(name="Doublon", ice="123456789012345", legalForm="SARL", fiscalYear=2026))

    with repository._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM fiscal_years").fetchone()[0] == 0


def test_legacy_database_is_migrated_and_flagged_rows_are_treated_like_any_client(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute(
            """CREATE TABLE clients (id TEXT PRIMARY KEY, name TEXT NOT NULL, ice TEXT NOT NULL DEFAULT '',
               legal_form TEXT NOT NULL DEFAULT '', tva_regime TEXT NOT NULL DEFAULT 'Débit',
               tva_periodicite TEXT NOT NULL DEFAULT 'Mensuelle', is_demo INTEGER NOT NULL DEFAULT 0,
               created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""
        )
        db.execute(
            """CREATE TABLE fiscal_years (client_id TEXT NOT NULL, year INTEGER NOT NULL,
               status TEXT NOT NULL DEFAULT 'open', is_demo INTEGER NOT NULL DEFAULT 0,
               created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(client_id, year))"""
        )
        db.execute("INSERT INTO clients(id, name, legal_form, created_at, updated_at) VALUES ('R1', 'Bravo', 'SARL', 'now', 'now')")
        db.execute("INSERT INTO clients(id, name, legal_form, is_demo, created_at, updated_at) VALUES ('D1', 'Alpha', 'SARL', 1, 'now', 'now')")
        db.execute("INSERT INTO fiscal_years(client_id, year, is_demo, created_at, updated_at) VALUES ('D1', 2026, 1, 'now', 'now')")

    repository = ClientRepository(database)

    assert repository.get_client("R1")["identifiantFiscal"] == ""
    assert [client["id"] for client in repository.list_clients()] == ["D1", "R1"]
    updated = repository.update_client("D1", ClientUpsert(name="Alpha SARL", legalForm="SARL", fiscalYear=2027))
    assert updated["years"] == [{"year": 2027, "status": "open"}, {"year": 2026, "status": "open"}]
    assert repository.save_fiscal_year("D1", FiscalYearUpsert(year=2026, status="closed"))["status"] == "closed"
    created = repository.create_client(ClientUpsert(name="Charlie", legalForm="SARL", fiscalYear=2026))
    assert created["years"] == [{"year": 2026, "status": "open"}]
