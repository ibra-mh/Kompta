"""SQLite-backed client and fiscal-year setup for local Kompta dossiers."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .storage import connect_database, resolve_database_path, utc_now_iso


class ClientUpsert(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(..., min_length=1, max_length=255)
    ice: str = Field("", max_length=32)
    identifiant_fiscal: str = Field("", max_length=32, alias="identifiantFiscal")
    legal_form: str = Field("", min_length=1, max_length=100, alias="legalForm")
    tva_regime: str = Field("Débit", min_length=1, max_length=50, alias="tvaRegime")
    tva_periodicite: str = Field("Mensuelle", min_length=1, max_length=50, alias="tvaPeriodicite")
    fiscal_year: int | None = Field(None, ge=2000, le=2100, alias="fiscalYear")

    @field_validator("name", "ice", "identifiant_fiscal", "legal_form", "tva_regime", "tva_periodicite")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_business_fields(self) -> "ClientUpsert":
        if not self.name or not self.legal_form or not self.tva_regime or not self.tva_periodicite:
            raise ValueError("Les champs client obligatoires ne peuvent pas être vides")
        if self.ice and (not self.ice.isdigit() or len(self.ice) != 15):
            raise ValueError("L'ICE doit contenir exactement 15 chiffres")
        if self.identifiant_fiscal and (not self.identifiant_fiscal.isdigit() or len(self.identifiant_fiscal) != 8):
            raise ValueError("L'identifiant fiscal (IF) doit contenir exactement 8 chiffres")
        return self


class FiscalYearUpsert(BaseModel):
    year: int = Field(..., ge=2000, le=2100)
    status: str = Field("open", pattern="^(open|closed)$")


class ClientRepository:
    def __init__(self, database: str | Path | None = None) -> None:
        self.database = str(resolve_database_path(database))
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_database(self.database, autocommit=True)

    @staticmethod
    def _now() -> str:
        return utc_now_iso()

    def _initialize(self) -> None:
        with closing(self._connect()) as db, db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS clients (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    ice TEXT NOT NULL DEFAULT '',
                    legal_form TEXT NOT NULL DEFAULT '',
                    tva_regime TEXT NOT NULL DEFAULT 'Débit',
                    tva_periodicite TEXT NOT NULL DEFAULT 'Mensuelle',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fiscal_years (
                    client_id TEXT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
                    year INTEGER NOT NULL CHECK(year BETWEEN 2000 AND 2100),
                    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'closed')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(client_id, year)
                );
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(clients)")}
            if "identifiant_fiscal" not in columns:
                db.execute("ALTER TABLE clients ADD COLUMN identifiant_fiscal TEXT NOT NULL DEFAULT ''")
            indexes = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='clients'")}
            # Databases created before the demo/real split was removed keep their equivalent legacy index.
            if "clients_unique_real_ice" not in indexes:
                db.execute("CREATE UNIQUE INDEX IF NOT EXISTS clients_unique_ice ON clients(ice) WHERE ice <> ''")

    @staticmethod
    def _client(row: sqlite3.Row, years: list[dict[str, object]]) -> dict[str, object]:
        return {
            "id": row["id"],
            "name": row["name"],
            "ice": row["ice"],
            "identifiantFiscal": row["identifiant_fiscal"],
            "legalForm": row["legal_form"],
            "tvaRegime": row["tva_regime"],
            "tvaPeriodicite": row["tva_periodicite"],
            "years": years,
        }

    @staticmethod
    def _fiscal_year(item: sqlite3.Row) -> dict[str, object]:
        return {"year": item["year"], "status": item["status"]}

    @classmethod
    def _fiscal_years(cls, db: sqlite3.Connection, client_id: str) -> list[dict[str, object]]:
        rows = db.execute(
            "SELECT year, status FROM fiscal_years WHERE client_id=? ORDER BY year DESC",
            (client_id,),
        ).fetchall()
        return [cls._fiscal_year(item) for item in rows]

    def list_clients(self) -> list[dict[str, object]]:
        with closing(self._connect()) as db, db:
            rows = db.execute("SELECT * FROM clients ORDER BY name COLLATE NOCASE, created_at").fetchall()
            years: dict[str, list[dict[str, object]]] = {}
            for item in db.execute("SELECT client_id, year, status FROM fiscal_years ORDER BY client_id, year DESC"):
                years.setdefault(item["client_id"], []).append(self._fiscal_year(item))
            return [self._client(row, years.get(row["id"], [])) for row in rows]

    def _ensure_fiscal_year(self, db: sqlite3.Connection, client_id: str, year: int | None, now: str) -> None:
        """Open the requested exercise if it does not exist yet; an existing status is never changed."""
        if year is None:
            return
        db.execute(
                """INSERT OR IGNORE INTO fiscal_years(client_id, year, status, created_at, updated_at)
               VALUES (?, ?, 'open', ?, ?)""",
            (client_id, year, now, now),
        )

    def create_client(self, request: ClientUpsert) -> dict[str, object]:
        client_id = f"R{uuid4().hex[:10].upper()}"
        now = self._now()
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """INSERT INTO clients(id, name, ice, identifiant_fiscal, legal_form, tva_regime, tva_periodicite, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (client_id, request.name, request.ice, request.identifiant_fiscal, request.legal_form, request.tva_regime, request.tva_periodicite, now, now),
            )
            self._ensure_fiscal_year(db, client_id, request.fiscal_year, now)
            db.execute("COMMIT")
        return self.get_client(client_id)

    def update_client(self, client_id: str, request: ClientUpsert) -> dict[str, object]:
        now = self._now()
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                """UPDATE clients SET name=?, ice=?, identifiant_fiscal=?, legal_form=?, tva_regime=?, tva_periodicite=?, updated_at=?
                   WHERE id=?""",
                (request.name, request.ice, request.identifiant_fiscal, request.legal_form, request.tva_regime, request.tva_periodicite, now, client_id),
            )
            if cursor.rowcount == 0:
                db.execute("ROLLBACK")
                raise KeyError(client_id)
            self._ensure_fiscal_year(db, client_id, request.fiscal_year, now)
            db.execute("COMMIT")
        return self.get_client(client_id)

    def get_client(self, client_id: str) -> dict[str, object]:
        with closing(self._connect()) as db, db:
            row = db.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
            if row is None:
                raise KeyError(client_id)
            return self._client(row, self._fiscal_years(db, client_id))

    def save_fiscal_year(self, client_id: str, request: FiscalYearUpsert) -> dict[str, object]:
        now = self._now()
        with closing(self._connect()) as db, db:
            client = db.execute("SELECT id FROM clients WHERE id=?", (client_id,)).fetchone()
            if client is None:
                raise KeyError(client_id)
            db.execute(
                """INSERT INTO fiscal_years(client_id, year, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(client_id, year) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at""",
                (client_id, request.year, request.status, now, now),
            )
        return {"clientId": client_id, "year": request.year, "status": request.status}
