# Kompta — Prototype d’exports SIMPL marocains

Local prototype for SIMPL-TVA, SIMPL-IR, and SIMPL-IS data preparation.
It includes project-defined validation and XML generation, but its schemas
and output formats have not been verified against official DGI materials.

## How to run this (no coding needed)

1. **Install Python** if you don't have it: [python.org/downloads](https://www.python.org/downloads/)
   (on Windows, tick "Add Python to PATH" during install).
2. **Open a terminal in this folder** (`kompta_tax_export/`):
   - Windows: open the folder in File Explorer, type `cmd` in the address bar, press Enter.
   - Mac: right-click the folder → "New Terminal at Folder" (or open Terminal and `cd` into it).
3. **Install the dependencies** (only needed once):
   ```
   pip install -r requirements.txt
   ```
4. **Start the tax engine**:
   ```
   python run.py
   ```
   Leave this terminal window open — it's your backend server. You'll see
   `Kompta tax engine starting at http://127.0.0.1:8000`.
5. **Open `index.html`** (double-click it, or drag it into your browser).
   Go to **Traitements → Déclaration TVA + RAS**, then click
   **"Exporter déclaration"**. The page calls the local Python engine.
   Generated XML files are not DGI-ready.

### VS Code Live Server

The tested Live Server origin is `http://127.0.0.1:5500`; `python run.py`
allows this exact loopback origin while continuing to bind the API to
`127.0.0.1:8000`. If the backend was already running when this setting was
added, stop it with `Ctrl+C` and restart it with `python run.py`.

If Live Server uses another loopback URL, set that exact origin before
starting the backend in PowerShell:

```powershell
$env:KOMPTA_DEV_CORS_ORIGINS = "http://localhost:5500"
python run.py
```

Only explicit HTTP origins on `localhost` or `127.0.0.1` with a port are
accepted. Wildcards and LAN addresses are rejected. If launching the ASGI
app directly in a production context, leave `KOMPTA_DEV_CORS_ORIGINS`
unset; the default remains `file://` (`Origin: null`) only.

To verify, open the page at the exact configured URL, open developer tools,
and confirm `GET /api/journal-entries` returns `200` with a matching
`Access-Control-Allow-Origin`. Then open a dossier, switch sections, try an
Excel export, and upload an OCR document; JSON `POST` preflights should
return `200` for this origin. A different origin or port must remain blocked.

To stop the backend, go back to the terminal and press `Ctrl+C`. You need
to restart it (`python run.py`) each time you want to use the export
button again.

### Heads up about the demo data

The built-in clients and journal entries are fictitious examples. They are
marked as demonstration data in the French interface and are included in
the TVA exports so the demo dossiers produce populated files. Generated XML
files must not be submitted to the DGI.
Some sample identifiers resemble real identifiers; that does not make them
valid taxpayer data. The app does not yet provide a complete production
client/exercise onboarding workflow.

Two other things the export currently defaults, since Kompta doesn't
track them per-invoice yet:
- **Mode de paiement**: defaulted to `VIREMENT` for every line.
- **Date de paiement**: defaulted to the invoice date (no separate
  payment date is captured yet — that'll make more sense once the Bank
  Reconciliation module is wired up).
These values are placeholders, not confirmed DGI requirements. The
JavaScript now lives in `app.js`; use actual payment dates and methods only
after the corresponding business data is collected and verified.

### OCR for images

PDF text is extracted with `pypdf`; when invoice fields are missing, pages are
rendered with `pypdfium2` and supplemented with RapidOCR. PNG and JPEG images
are decoded with OpenCV and recognized with RapidOCR and the CPU ONNX Runtime.
OCR text then goes through the same invoice-field parser and stays in the
manual review workflow. The first image OCR may download the French recognition
models into the Python package cache, so network access is required once.
Subsequent image recognition uses the cached models locally. Install the
dependencies with `pip install -r requirements.txt`.

## ⚠️ Before production use

No authoritative DGI specifications or accepted sample exports are present
in this repository. The two files in `schemas/` explicitly identify
themselves as placeholders. Local XSD validation therefore checks only
the project's own XML shape; it is not evidence of DGI acceptance.

Before using any generated file for a declaration, obtain and compare:

1. SIMPL-TVA: the current official Relevé des Déductions specification,
   matching XSD, and at least one accepted export for the intended tax year.
2. SIMPL-IR: the current official État 9421 specification, matching XSD,
   and an accepted export.
3. SIMPL-IS: the official SIMPL-IS transport/XML specification, XSD or
   equivalent schema, official liasse cell dictionary/mapping rules, and
   an accepted export. No SIMPL-IS DGI schema is included here.

The version strings mentioned in earlier project notes are not verified
against DGI materials. Do not describe SIMPL-TVA, SIMPL-IR, or SIMPL-IS
outputs as DGI-compliant until the corresponding official materials and
accepted samples have been checked. Business-rule validation is also
project-defined and is not a substitute for official tax rules.

## Structure

```
kompta_tax_export/
├── core/
│   ├── models.py                 # Pydantic models: DeductionLine, ReleveDeductions,
│   │                              # SalarieLine, Etat9421
│   ├── validators.py             # Blocking pre-validation (ICE/IF, arithmetic, duplicates)
│   ├── xml_builders.py           # XML generation (⚠️ tag layout — verify against real DGI schema)
│   ├── xsd_validator.py          # Pre-flight XSD structural validation (lxml)
│   ├── packaging.py              # Project-defined XML archive packaging
│   ├── export_service.py         # Orchestrates: validate → build XML → XSD check → zip
│   ├── export_service_types.py   # ExportOutcome result type
│   ├── export_safety.py          # Demo-export restrictions and download headers
│   ├── excel_export.py           # TVA workbook generation
│   ├── client_service.py         # Client and fiscal-year persistence
│   ├── journal_service.py        # Atomic append-only journal persistence
│   ├── invoice_extractor.py      # PDF/image invoice extraction and parsing
│   ├── ocr_service.py            # OCR document intake and persistence
│   ├── pcge_import.py            # Chart import/preview sourced from the CGNC dataset
│   ├── cgnc.py                   # CGNC chart loader, account validation, shared account roots
│   ├── liasse_models.py          # SIMPL-IS request and mapping models
│   ├── liasse_service.py         # SIMPL-IS calculation and XML validation
│   └── storage.py                # Shared SQLite location/connection policy
├── schemas/
│   ├── releve_deductions.xsd     # Placeholder; not the official DGI schema
│   └── etat_9421.xsd             # Placeholder; not the official DGI schema
├── tests/                        # Backend and OCR regression tests
├── tools/
│   └── pcge_audit/
│       ├── scripts/              # PCGE account extraction and comparison tools
│       └── data/                 # Source snapshots and generated audit reports
├── archive/
│   └── legacy-snapshots/         # Preserved ZIPs and the 3 audit scripts that diverged from tools/
├── api.py                        # FastAPI routes
├── main.py                       # FastAPI app entrypoint
├── index.html                    # French interface structure
├── styles.css                    # Extracted interface styles
├── app.js                        # Extracted interface behavior
├── cgnc_standard_accounts.json   # Official CGNC chart — single source of truth
├── cgnc_supplement_accounts.json # Documented additions missing from the dataset (3455, 4455)
├── kompta.sqlite3                # Local accounting database; keep private
├── requirements.txt              # Runtime dependencies
└── requirements-dev.txt          # Runtime + pytest/pyflakes
```

### Chart of accounts (CGNC)

`cgnc_standard_accounts.json` is the single source of truth for the chart of
accounts. `cgnc_supplement_accounts.json` adds only the roots the app posts to
that are missing from that dataset (3455 TVA récupérable, 4455 TVA facturée),
each with the reason it is needed.

- An account code is valid when it is listed, or extends a listed code
  (e.g. `44110002` under `4411`, `3455220` under `34552`). Journal posting
  rejects any other code.
- The interface loads the chart from `GET /api/accounts/cgnc`; official
  accounts cannot be relabeled, renumbered or deleted in the UI. Local
  sub-accounts (clients, suppliers, TVA rates) remain editable and are saved
  in the browser.
- The PCGE import/preview reads the same dataset; entries whose `status` is
  `needs_review` are listed for review instead of being imported.

### PCGE account audit tools

The standalone account-catalog analysis scripts are grouped under
`tools/pcge_audit/scripts/`; their JSON inputs and reports are in
`tools/pcge_audit/data/`. They are not imported by the running application.
`extract_existing.py` and `parse_app_js.py` audited the chart formerly
hard-coded in `app.js`, which now comes from the CGNC dataset.
Run them from any working directory with paths relative to each script:

```powershell
python tools/pcge_audit/scripts/build_official_plan.py
python tools/pcge_audit/scripts/extract_existing.py
python tools/pcge_audit/scripts/parse_app_js.py
python tools/pcge_audit/scripts/compare_accounts.py
python tools/pcge_audit/scripts/categorize_accounts.py
```

## Validation rules implemented

**SIMPL-TVA (per line):**
- ICE (15 digits) or IF (8 digits) mandatory
- `round(HT + TVA, 2) == round(TTC, 2)` exact arithmetic check
- Duplicate detection on `numFacture` + `ice` (or `IF` as fallback)
- Non-blocking warning if `tauxTVA` isn't one of the standard rates (0/7/10/14/20)

**SIMPL-IR (État 9421):**
- CIN mandatory for `PERMANENT` and `SALARIE_CFC` categories
- Duplicate CIN detection across employees
- `date_sortie` must not precede `date_entree`

All blocking issues are returned with the line number, the failing
field, and a message — see `ValidationReport.to_dict()`. These checks are
project rules, not an official DGI validation service.

## Running

```bash
pip install -r requirements-dev.txt   # runtime + test/lint tools
python -m pytest tests/ -v
python run.py             # binds to 127.0.0.1:8000
```

## Local security and persisted data

Kompta currently has a **single local owner**, not authenticated user
accounts. `run.py` binds the API to `127.0.0.1`; CORS allows only the
`null` origin used when opening the standalone `index.html` file. CORS is
not authentication. Client and exercise fields identify a local data scope,
and server reads/reversals check that scope, but they do not create
multi-user isolation. Anyone with access to the same Windows account can
use all dossiers in this local database. Do not bind the API to a LAN or
public interface. Multi-user use requires authenticated identities and a
server-owned client/exercise ownership registry, neither of which exists.

The database defaults to `<project folder>/kompta.sqlite3`, independent of
the shell's current directory. Set `KOMPTA_DATABASE_PATH` to an absolute
path to move it; relative overrides are resolved from the project folder.
The journal and OCR services create missing tables at startup. The OCR
migration adds `extracted_json` when absent and preserves existing rows;
there are no destructive schema migrations. Journal postings are committed
inside SQLite `BEGIN IMMEDIATE` transactions and are append-only. OCR
deduplication and insertion are serialized in one transaction.

Persistence boundaries:
- Posted journal entries and retained OCR source files are stored in SQLite.
   The selected client/exercise's posted entries are reloaded from SQLite when
   the interface starts or opens that dossier.
- Liasse state and local sub-account/auxiliary-account customizations use this browser's
   `localStorage`; they are not included in the SQLite database or its backup.
- Built-in demo clients, demo journal rows, opening balances, and most other
   interface state are JavaScript fixtures/in-memory state. They are not
   durable production records. Demo rows are included in TVA exports and
   excluded from SIMPL-IS liasse calculations.

Back up the SQLite database while the app is stopped. Use a new destination
filename for each backup so an earlier backup is not overwritten. For the
default location, from the project folder run:

```powershell
$backup = "kompta-backup-$(Get-Date -Format yyyyMMdd-HHmmss).sqlite3"
python -c "import sqlite3,sys; source=sqlite3.connect('kompta.sqlite3'); target=sqlite3.connect(sys.argv[1]); source.backup(target); target.close(); source.close()" $backup
```

The SQLite backup API is preferable to copying a database while it is in
use. Store backups securely because they contain accounting documents and
financial data. To restore the default database, stop Kompta, then run this
from the project folder with the intended backup filename:

```powershell
$db = Join-Path $PWD "kompta.sqlite3"
$backup = (Resolve-Path ".\kompta-backup-YYYYMMDD-HHMMSS.sqlite3").Path
Copy-Item $db "$db.before-restore-$(Get-Date -Format yyyyMMdd-HHmmss)"
Copy-Item $backup $db -Force
```

For a configured database, set `$db` to that exact path before running the
commands. The first copy preserves the current database; verify both paths
before the explicit replacement. Restart Kompta and confirm expected
records are present. `localStorage` values must be backed up separately
from the browser profile if they matter.

### Example: validating without exporting

```
POST /api/exports/simpl-tva/validate
{
  "ice_declarant": "000111222333444",
  "if_declarant": "12345678",
  "periode": "2026-08",
  "lines": [ { "ord": 1, "numFacture": "FA-001", "designation": "...",
               "montantHT": 1000, "tauxTVA": 20, "montantTVA": 200,
               "montantTTC": 1200, "ice": "123456789012345",
               "modePaiement": "VIREMENT", "datePaiement": "2026-08-15",
               "dateFacture": "2026-08-10" } ]
}
```

Returns a `ValidationReport` with `is_blocked` and any issues. A successful
local validation allows the API to generate its project-format archive; it
does not establish DGI acceptance.


## Remaining before production

- Obtain and verify current official DGI specifications and accepted samples
   for SIMPL-TVA, SIMPL-IR, and SIMPL-IS, then replace the placeholder schemas.
- Replace the static demo client/exercise setup with a production data-entry
   and ownership model before using client records for fiscal calculations.
- Add authenticated identities and server-side ownership checks before any
   multi-user or network-accessible deployment.
- Decide whether browser-only customization/Liasse state should move into the
   SQLite backup and migration lifecycle.
