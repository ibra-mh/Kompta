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
    legal_form: str = Field("", min_length=1, max_length=100, alias="legalForm")
    tva_regime: str = Field("Débit", min_length=1, max_length=50, alias="tvaRegime")
    tva_periodicite: str = Field("Mensuelle", min_length=1, max_length=50, alias="tvaPeriodicite")

    @field_validator("name", "ice", "legal_form", "tva_regime", "tva_periodicite")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_business_fields(self) -> "ClientUpsert":
        if not self.name or not self.legal_form or not self.tva_regime or not self.tva_periodicite:
            raise ValueError("Les champs client obligatoires ne peuvent pas être vides")
        if self.ice and (not self.ice.isdigit() or len(self.ice) != 15):
            raise ValueError("L'ICE doit contenir exactement 15 chiffres")
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
                    is_demo INTEGER NOT NULL DEFAULT 0 CHECK(is_demo IN (0, 1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fiscal_years (
                    client_id TEXT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
                    year INTEGER NOT NULL CHECK(year BETWEEN 2000 AND 2100),
                    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'closed')),
                    is_demo INTEGER NOT NULL DEFAULT 0 CHECK(is_demo IN (0, 1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(client_id, year)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS clients_unique_real_ice
                    ON clients(ice) WHERE is_demo = 0 AND ice <> '';
                """
            )

    @staticmethod
    def _client(row: sqlite3.Row, years: list[dict[str, object]]) -> dict[str, object]:
        return {
            "id": row["id"],
            "name": row["name"],
            "ice": row["ice"],
            "legalForm": row["legal_form"],
            "tvaRegime": row["tva_regime"],
            "tvaPeriodicite": row["tva_periodicite"],
            "isDemo": bool(row["is_demo"]),
            "years": years,
        }

    @staticmethod
    def _fiscal_year(item: sqlite3.Row) -> dict[str, object]:
        return {"year": item["year"], "status": item["status"], "isDemo": bool(item["is_demo"])}

    @classmethod
    def _fiscal_years(cls, db: sqlite3.Connection, client_id: str) -> list[dict[str, object]]:
        rows = db.execute(
            "SELECT year, status, is_demo FROM fiscal_years WHERE client_id=? ORDER BY year DESC",
            (client_id,),
        ).fetchall()
        return [cls._fiscal_year(item) for item in rows]

    def list_clients(self, *, include_demo: bool = True) -> list[dict[str, object]]:
        with closing(self._connect()) as db, db:
            where = "" if include_demo else "WHERE is_demo = 0"
            rows = db.execute(f"SELECT * FROM clients {where} ORDER BY is_demo DESC, name COLLATE NOCASE").fetchall()
            years: dict[str, list[dict[str, object]]] = {}
            for item in db.execute("SELECT client_id, year, status, is_demo FROM fiscal_years ORDER BY client_id, year DESC"):
                years.setdefault(item["client_id"], []).append(self._fiscal_year(item))
            return [self._client(row, years.get(row["id"], [])) for row in rows]

    def create_client(self, request: ClientUpsert) -> dict[str, object]:
        client_id = f"R{uuid4().hex[:10].upper()}"
        now = self._now()
        with closing(self._connect()) as db, db:
            db.execute(
                """INSERT INTO clients(id, name, ice, legal_form, tva_regime, tva_periodicite, is_demo, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)""",
                (client_id, request.name, request.ice, request.legal_form, request.tva_regime, request.tva_periodicite, now, now),
            )
        return self.get_client(client_id)

    def update_client(self, client_id: str, request: ClientUpsert) -> dict[str, object]:
        now = self._now()
        with closing(self._connect()) as db, db:
            cursor = db.execute(
                """UPDATE clients SET name=?, ice=?, legal_form=?, tva_regime=?, tva_periodicite=?, updated_at=?
                   WHERE id=? AND is_demo=0""",
                (request.name, request.ice, request.legal_form, request.tva_regime, request.tva_periodicite, now, client_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(client_id)
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
            client = db.execute("SELECT id, is_demo FROM clients WHERE id=?", (client_id,)).fetchone()
            if client is None:
                raise KeyError(client_id)
            if client["is_demo"]:
                raise ValueError("Les exercices de démonstration ne sont pas modifiables")
            db.execute(
                """INSERT INTO fiscal_years(client_id, year, status, is_demo, created_at, updated_at)
                   VALUES (?, ?, ?, 0, ?, ?)
                   ON CONFLICT(client_id, year) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at""",
                (client_id, request.year, request.status, now, now),
            )
        return {"clientId": client_id, "year": request.year, "status": request.status, "isDemo": False}
