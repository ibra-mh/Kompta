"""Append-only journal posting service for Kompta.

The browser may prepare a draft, but only this module can allocate numbers and
persist a posted entry. SQLite transactions are serialized with BEGIN IMMEDIATE;
triggers make posted journal data and audit records immutable.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from .storage import connect_database, resolve_database_path, utc_now_iso
from .pcge_import import preview_import, extract_pcge_general_accounts
from .cgnc import TIER_ROOT_TYPES, TIER_ROOTS, is_cgnc_account, official_label


JOURNAL_PIECE_PREFIXES = {"ACHATS": "JA", "VENTES": "JV", "BANQUE": "JB", "CAISSE": "JC"}
AUXILIARY_ACCOUNTS_DDL = """
    CREATE TABLE IF NOT EXISTS auxiliary_accounts (
        code TEXT NOT NULL,
        label TEXT NOT NULL,
        root_code TEXT NOT NULL,
        client_id TEXT NOT NULL,
        ice TEXT,
        tax_id TEXT,
        account_type TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(client_id, code),
        FOREIGN KEY(root_code) REFERENCES pcm_accounts(code)
    );
"""


class JournalLine(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    account: str = Field(..., min_length=1, alias="compte")
    auxiliary: str | None = Field(None, alias="auxiliaire")
    label: str = Field("", alias="libelle")
    debit: float = Field(0, ge=0)
    credit: float = Field(0, ge=0)
    invoice: str = Field("", alias="facture")
    vat_rate: float = Field(0, ge=0, alias="tva")

    @model_validator(mode="after")
    def one_side_only(self) -> "JournalLine":
        if self.debit and self.credit:
            raise ValueError("A journal line cannot contain both debit and credit")
        if not self.debit and not self.credit:
            raise ValueError("A journal line must contain a debit or a credit")
        return self


class JournalEntryPost(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    user_id: str = Field("local-owner", min_length=1, alias="userId")
    client_id: str = Field(..., min_length=1, alias="clientId")
    year: int = Field(..., ge=2000, le=2100)
    journal: str = Field(..., min_length=1)
    entry_date: str = Field(..., min_length=8, alias="date")
    reference: str = Field("", alias="reference")
    label: str = Field("", alias="libelle")
    lines: list[JournalLine] = Field(..., min_length=2)
    account_catalog: list[dict[str, Any]] = Field(default_factory=list, alias="accountCatalog")

    @field_validator("entry_date")
    @classmethod
    def validate_entry_date(cls, value: str) -> str:
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError as error:
            raise ValueError("Date d’écriture invalide; format attendu AAAA-MM-JJ") from error
        return value


class JournalEntryPosted(BaseModel):
    entry_id: int = Field(alias="entryId")
    entry_number: int = Field(alias="entryNumber")
    piece_number: str = Field(alias="pieceNumber")
    client_id: str = Field(alias="clientId")
    year: int
    model_config = ConfigDict(populate_by_name=True)


class JournalRepository:
    def __init__(self, database: str | Path | None = None) -> None:
        self.database = str(resolve_database_path(database))
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        return connect_database(self.database, autocommit=True)

    def _initialize(self) -> None:
        with closing(self._connect()) as db, db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS pcm_accounts (
                    code TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    parent TEXT,
                    account_type TEXT NOT NULL DEFAULT 'parent',
                    catalog_source TEXT NOT NULL DEFAULT 'existing',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS journal_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_id TEXT NOT NULL,
                    year INTEGER NOT NULL,
                    entry_number INTEGER NOT NULL,
                    piece_number TEXT NOT NULL,
                    journal TEXT NOT NULL,
                    entry_date TEXT NOT NULL,
                    reference TEXT NOT NULL DEFAULT '',
                    label TEXT NOT NULL DEFAULT '',
                    user_id TEXT NOT NULL,
                    posted_at TEXT NOT NULL,
                    UNIQUE(client_id, year, entry_number),
                    UNIQUE(client_id, year, piece_number)
                );
                CREATE TABLE IF NOT EXISTS journal_lines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entry_id INTEGER NOT NULL REFERENCES journal_entries(id),
                    line_number INTEGER NOT NULL,
                    account_code TEXT NOT NULL,
                    auxiliary_code TEXT,
                    label TEXT NOT NULL DEFAULT '',
                    debit NUMERIC NOT NULL DEFAULT 0,
                    credit NUMERIC NOT NULL DEFAULT 0,
                    invoice TEXT NOT NULL DEFAULT '',
                    vat_rate NUMERIC NOT NULL DEFAULT 0,
                    UNIQUE(entry_id, line_number)
                );
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    action_type TEXT NOT NULL CHECK(action_type IN ('CREATE','POST','REVERSAL')),
                    entry_id INTEGER NOT NULL,
                    payload_diff TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS journal_sequences (
                    client_id TEXT NOT NULL,
                    year INTEGER NOT NULL,
                    next_entry_number INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(client_id, year)
                );
                CREATE TRIGGER IF NOT EXISTS journal_entries_no_update
                BEFORE UPDATE ON journal_entries BEGIN SELECT RAISE(ABORT, 'posted journal entries are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS journal_entries_no_delete
                BEFORE DELETE ON journal_entries BEGIN SELECT RAISE(ABORT, 'posted journal entries are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS journal_lines_no_update
                BEFORE UPDATE ON journal_lines BEGIN SELECT RAISE(ABORT, 'posted journal lines are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS journal_lines_no_delete
                BEFORE DELETE ON journal_lines BEGIN SELECT RAISE(ABORT, 'posted journal lines are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS audit_logs_no_update
                BEFORE UPDATE ON audit_logs BEGIN SELECT RAISE(ABORT, 'audit logs are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS audit_logs_no_delete
                BEFORE DELETE ON audit_logs BEGIN SELECT RAISE(ABORT, 'audit logs are immutable'); END;
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(pcm_accounts)").fetchall()}
            if "catalog_source" not in columns:
                db.execute("ALTER TABLE pcm_accounts ADD COLUMN catalog_source TEXT NOT NULL DEFAULT 'existing'")
            self._ensure_auxiliary_accounts_table(db)

    @staticmethod
    def _ensure_auxiliary_accounts_table(db: sqlite3.Connection) -> None:
        info = db.execute("PRAGMA table_info(auxiliary_accounts)").fetchall()
        if not info:
            db.executescript(AUXILIARY_ACCOUNTS_DDL)
            return
        primary_key = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
        if primary_key == ["client_id", "code"]:
            return
        # Legacy schema keyed on code alone; shared supplier/client codes need per-dossier rows.
        db.executescript(
            "BEGIN IMMEDIATE;"
            "ALTER TABLE auxiliary_accounts RENAME TO auxiliary_accounts_legacy;"
            + AUXILIARY_ACCOUNTS_DDL
            + "INSERT INTO auxiliary_accounts(code, label, root_code, client_id, ice, tax_id, account_type, updated_at)"
            " SELECT code, label, root_code, client_id, ice, tax_id, account_type, updated_at FROM auxiliary_accounts_legacy;"
            "DROP TABLE auxiliary_accounts_legacy;"
            "COMMIT;"
        )

    @staticmethod
    def _now() -> str:
        return utc_now_iso()

    def _sync_catalog(self, db: sqlite3.Connection, catalog: list[dict[str, Any]]) -> None:
        """Mirror catalog accounts accepted by the CGNC chart; listed codes keep their official label."""
        now = self._now()
        for account in catalog:
            code = str(account.get("code", "")).strip()
            label = official_label(code) or str(account.get("label", account.get("libelle", ""))).strip()
            if not code or not label or not is_cgnc_account(code):
                continue
            db.execute(
                """INSERT INTO pcm_accounts(code, label, parent, account_type, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(code) DO UPDATE SET label=excluded.label,
                     parent=excluded.parent, account_type=excluded.account_type,
                     updated_at=excluded.updated_at""",
                (code, label, account.get("parent"), account.get("type", "parent"), now),
            )

    def _sync_auxiliary_accounts(self, db: sqlite3.Connection, client_id: str, catalog: list[dict[str, Any]]) -> None:
        """Register client (3421...) and supplier (4411...) sub-accounts sent with the entry for this dossier."""
        now = self._now()
        for account in catalog:
            code = str(account.get("code", "")).strip()
            root = code[:4]
            label = str(account.get("label", account.get("libelle", ""))).strip()
            if root not in TIER_ROOT_TYPES or len(code) <= 4 or not code.isdigit() or not label:
                continue
            db.execute(
                "INSERT OR IGNORE INTO pcm_accounts(code, label, account_type, catalog_source, updated_at) VALUES (?, ?, 'parent', 'cgnc_standard', ?)",
                (root, official_label(root), now),
            )
            db.execute(
                """INSERT INTO auxiliary_accounts(code, label, root_code, client_id, ice, tax_id, account_type, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(client_id, code) DO UPDATE SET label=excluded.label, ice=excluded.ice,
                     tax_id=excluded.tax_id, account_type=excluded.account_type, updated_at=excluded.updated_at""",
                (
                    code, label, root, client_id,
                    str(account.get("ice") or "").strip() or None,
                    str(account.get("identifiant_fiscal") or "").strip() or None,
                    str(account.get("type_tiers") or TIER_ROOT_TYPES[root]),
                    now,
                ),
            )

    def preview_pcge_general(self, source_path: str | Path | None = None, existing: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        with closing(self._connect()) as db, db:
            catalog = existing
            if catalog is None:
                catalog = [dict(row) for row in db.execute("SELECT code, label, parent, account_type AS type FROM pcm_accounts").fetchall()]
        return preview_import(catalog, source_path)

    def import_pcge_general(self, source_path: str | Path | None = None, existing: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        report = self.preview_pcge_general(source_path, existing)
        now = self._now()
        imported = []
        codes = {item["code"] for item in extract_pcge_general_accounts(source_path)}
        with closing(self._connect()) as db, db:
            for account in report["added"]:
                cursor = db.execute(
                    """INSERT OR IGNORE INTO pcm_accounts(code, label, parent, account_type, catalog_source, updated_at)
                       VALUES (?, ?, ?, ?, 'cgnc_standard', ?)""",
                    (account["code"], account["label"], account.get("parent"), "parent" if any(code.startswith(account["code"]) and code != account["code"] for code in codes) else "account", now),
                )
                if cursor.rowcount:
                    imported.append(account)
        report["imported"] = imported
        report["importedCount"] = len(imported)
        return report

    def _validate_lines(self, db: sqlite3.Connection, request: JournalEntryPost) -> None:
        debit = sum(line.debit for line in request.lines)
        credit = sum(line.credit for line in request.lines)
        if round(debit - credit, 2) != 0 or debit <= 0:
            raise ValueError("Journal entry must be balanced and have a positive total")
        for line in request.lines:
            if not is_cgnc_account(line.account):
                raise ValueError(f"Unknown PCM account: {line.account} (absent du référentiel CGNC)")
            if line.account.startswith(TIER_ROOTS):
                if not line.auxiliary:
                    raise ValueError(f"Auxiliary account is required for tier account {line.account}")
                auxiliary = db.execute(
                    "SELECT root_code FROM auxiliary_accounts WHERE code = ? AND client_id = ?",
                    (line.auxiliary.strip(), request.client_id),
                ).fetchone()
                if auxiliary is None or not line.auxiliary.startswith(line.account[:4]):
                    raise ValueError(f"Invalid auxiliary account for tier account {line.account}")

    def post(self, request: JournalEntryPost) -> JournalEntryPosted:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            self._sync_catalog(db, request.account_catalog)
            self._sync_auxiliary_accounts(db, request.client_id, request.account_catalog)
            result = self._post_in_transaction(db, request)
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def list_entries(self, client_id: str, year: int) -> list[dict[str, Any]]:
        with closing(self._connect()) as db, db:
            entries = db.execute(
                "SELECT * FROM journal_entries WHERE client_id=? AND year=? ORDER BY entry_number",
                (client_id, year),
            ).fetchall()
            lines_by_entry: dict[int, list[sqlite3.Row]] = {}
            for line in db.execute(
                """SELECT l.* FROM journal_lines l JOIN journal_entries e ON e.id = l.entry_id
                   WHERE e.client_id=? AND e.year=? ORDER BY l.entry_id, l.line_number""",
                (client_id, year),
            ):
                lines_by_entry.setdefault(line["entry_id"], []).append(line)
            result = []
            for entry in entries:
                lines = lines_by_entry.get(entry["id"], [])
                entry_date = entry["entry_date"]
                result.append({
                    "serverEntryId": entry["id"],
                    "piece": entry["piece_number"],
                    "journal": entry["journal"],
                    "jour": int(entry_date[8:10]),
                    "mois": int(entry_date[5:7]),
                    "year": entry["year"],
                    "libelle": entry["label"],
                    "n_facture": entry["reference"],
                    "n_mvt": entry["entry_number"],
                    "lines": [{
                        "compte": line["account_code"],
                        "auxiliaire": line["auxiliary_code"],
                        "libelle": line["label"],
                        "dbcr": "D" if line["debit"] else "C",
                        "montant": float(line["debit"] or line["credit"]),
                        "tva": float(line["vat_rate"]),
                        "facture": line["invoice"],
                        "lettre": "",
                    } for line in lines],
                })
            return result

    def reverse(
        self,
        entry_id: int,
        user_id: str,
        expected_client_id: str | None = None,
        expected_year: int | None = None,
    ) -> JournalEntryPosted:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            entry = db.execute("SELECT * FROM journal_entries WHERE id=?", (entry_id,)).fetchone()
            if entry is None:
                raise ValueError("Posted journal entry not found")
            if (
                expected_client_id is not None
                and entry["client_id"] != expected_client_id
            ) or (expected_year is not None and entry["year"] != expected_year):
                raise ValueError("Écriture introuvable dans ce dossier et cet exercice")
            lines = db.execute("SELECT * FROM journal_lines WHERE entry_id=? ORDER BY line_number", (entry_id,)).fetchall()
            request = JournalEntryPost(
                userId=user_id, clientId=entry["client_id"], year=entry["year"], journal=entry["journal"], date=entry["entry_date"],
                reference=f"REVERSAL {entry['piece_number']}", label=f"Contre-passation de {entry['piece_number']}",
                lines=[JournalLine(compte=row["account_code"], auxiliaire=row["auxiliary_code"], libelle=f"Annulation: {row['label']}", debit=row["credit"], credit=row["debit"], facture=row["invoice"], tva=row["vat_rate"]) for row in lines],
            )
            result = self._post_in_transaction(db, request)
            db.execute("INSERT INTO audit_logs(user_id, timestamp, action_type, entry_id, payload_diff) VALUES (?, ?, 'REVERSAL', ?, ?)", (user_id, self._now(), result.entry_id, json.dumps({"reversedEntryId": entry_id, "sourcePiece": entry["piece_number"]})))
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _post_in_transaction(self, db: sqlite3.Connection, request: JournalEntryPost) -> JournalEntryPosted:
        self._validate_lines(db, request)
        sequence = db.execute("SELECT next_entry_number FROM journal_sequences WHERE client_id=? AND year=?", (request.client_id, request.year)).fetchone()
        number = int(sequence[0]) if sequence else 1
        if sequence:
            db.execute("UPDATE journal_sequences SET next_entry_number=? WHERE client_id=? AND year=?", (number + 1, request.client_id, request.year))
        else:
            db.execute("INSERT INTO journal_sequences VALUES (?, ?, ?)", (request.client_id, request.year, number + 1))
        prefix = JOURNAL_PIECE_PREFIXES.get(request.journal, "OD")
        piece = f"{prefix}-{number:06d}"
        now = self._now()
        cursor = db.execute("INSERT INTO journal_entries(client_id, year, entry_number, piece_number, journal, entry_date, reference, label, user_id, posted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (request.client_id, request.year, number, piece, request.journal, request.entry_date, request.reference, request.label, request.user_id, now))
        new_id = int(cursor.lastrowid)
        for line_number, line in enumerate(request.lines, 1):
            db.execute("INSERT INTO journal_lines(entry_id, line_number, account_code, auxiliary_code, label, debit, credit, invoice, vat_rate) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (new_id, line_number, line.account, line.auxiliary, line.label, line.debit, line.credit, line.invoice, line.vat_rate))
        payload = request.model_dump(by_alias=True, mode="json")
        payload.update({"entryId": new_id, "entryNumber": number, "pieceNumber": piece})
        for action in ("CREATE", "POST"):
            db.execute("INSERT INTO audit_logs(user_id, timestamp, action_type, entry_id, payload_diff) VALUES (?, ?, ?, ?, ?)", (request.user_id, now, action, new_id, json.dumps(payload, sort_keys=True)))
        return JournalEntryPosted(entryId=new_id, entryNumber=number, pieceNumber=piece, clientId=request.client_id, year=request.year)
