# Hosting Kompta 24/7 on a Windows laptop (Mourad's test window)

Target: dedicated Windows 10/11 laptop (8 GB RAM, plugged in, wired), serving Mourad over a stable HTTPS URL for 1-2 weeks.

```
Mourad's browser --HTTPS--> ngrok static domain --> ngrok agent (laptop) --> uvicorn 127.0.0.1:8000 --> SQLite
```

Folder layout used below (outside the repo, so `git pull` can never touch data or secrets):

| Path | Purpose |
|---|---|
| `C:\Kompta\app` | Git clone, branch `LXP-4`, plus `.venv` |
| `C:\KomptaData` | Live database `kompta.sqlite3` |
| `C:\KomptaBackups` | Timestamped DB backups |
| `C:\KomptaOps` | Launcher scripts, secrets file, logs |

Run all PowerShell blocks as the laptop's normal user unless marked **(Admin)**.

---

## 0. Code prerequisites (must be on `LXP-4` before going live)

The app currently has no password and cannot be reached through a URL: `app.js` hardcodes `http://127.0.0.1:8000` and the backend serves no static files. Skip this section only if these changes are already committed on `LXP-4`.

**`app.js` line 5** (same-origin API base; `file://` use keeps working):

```js
const KOMPTA_API_BASE = window.location.protocol === 'file:' ? 'http://127.0.0.1:8000' : '';
const KOMPTA_API_LABEL = KOMPTA_API_BASE || window.location.origin;
```

In `apiConnectionErrorMessage`, replace `${KOMPTA_API_BASE}` with `${KOMPTA_API_LABEL}`.

**`main.py`** imports:

```python
import base64
import binascii
import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response
```

**`main.py`**, after the existing `add_middleware(CORSMiddleware, ...)` block:

```python
ROOT = Path(__file__).resolve().parent


def _load_basic_auth() -> tuple[bytes, bytes] | None:
    configured = os.environ.get("KOMPTA_BASIC_AUTH", "")
    if not configured:
        if os.environ.get("KOMPTA_REQUIRE_AUTH") == "1":
            raise RuntimeError("KOMPTA_REQUIRE_AUTH=1 but KOMPTA_BASIC_AUTH is not set")
        return None
    user, sep, password = configured.partition(":")
    if not sep or not user or not password:
        raise RuntimeError("KOMPTA_BASIC_AUTH must be 'user:password'")
    return user.encode(), password.encode()


_BASIC_AUTH = _load_basic_auth()


@app.middleware("http")
async def require_basic_auth(request: Request, call_next):
    if _BASIC_AUTH is None or request.url.path == "/health":
        return await call_next(request)
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "basic":
        try:
            user, _, password = base64.b64decode(token, validate=True).partition(b":")
        except (binascii.Error, ValueError):
            user, password = b"", b""
        user_ok = secrets.compare_digest(user, _BASIC_AUTH[0])
        password_ok = secrets.compare_digest(password, _BASIC_AUTH[1])
        if user_ok and password_ok:
            return await call_next(request)
    return Response(
        "Authentication required", status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="Kompta", charset="UTF-8"'},
    )


def _static_route(filename: str):
    def serve():
        return FileResponse(ROOT / filename, headers={"Cache-Control": "no-cache"})
    return serve


# Explicit files only: mounting the project root would expose kompta.sqlite3 and the source.
app.add_api_route("/", _static_route("index.html"), methods=["GET"], include_in_schema=False)
for _name in ("index.html", "app.js", "styles.css"):
    app.add_api_route(f"/{_name}", _static_route(_name), methods=["GET"], include_in_schema=False)
```

`KOMPTA_REQUIRE_AUTH=1` makes the server refuse to start when the password is missing, so it can never run unprotected by accident.

---

## 1. Windows prerequisites

Open **PowerShell (Admin)**:

```powershell
winget install -e --id Python.Python.3.12
winget install -e --id Git.Git
winget install -e --id Microsoft.VCRedist.2015+.x64   # required by onnxruntime / OpenCV
winget install -e --id Ngrok.Ngrok
```

Close and reopen PowerShell, then verify:

```powershell
py -3.12 --version     # Python 3.12.x (anything >= 3.10 works)
git --version
ngrok version
```

Create the folders and clone the branch:

```powershell
mkdir C:\Kompta, C:\KomptaData, C:\KomptaBackups, C:\KomptaOps, C:\KomptaOps\logs
git clone --branch LXP-4 <REPO_URL> C:\Kompta\app
cd C:\Kompta\app
git branch --show-current    # must print LXP-4
```

If the repository is private, Git Credential Manager opens a browser sign-in on first clone.

Update later with `git pull` (see section 8). `kompta.sqlite3` is gitignored, and the live DB lives in `C:\KomptaData` anyway.

---

## 2. Power, sleep and update settings

### 2.1 Never sleep, never hibernate, ignore the lid

**(Admin)** PowerShell:

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change standby-timeout-dc 0
powercfg /change hibernate-timeout-ac 0
powercfg /change hibernate-timeout-dc 0
powercfg /hibernate off
powercfg /change monitor-timeout-ac 10
# Lid close = Do nothing (plugged in and on battery)
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setdcvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setactive SCHEME_CURRENT
```

Check with `powercfg /query SCHEME_CURRENT SUB_BUTTONS LIDACTION`: the AC and DC setting indexes should both be `0x00000000`.

### 2.2 Keep the wired network awake

Device Manager > Network adapters > your Ethernet adapter > Properties > Power Management: untick **Allow the computer to turn off this device to save power**. Do the same under the Wi-Fi adapter if it stays enabled.

### 2.3 Stop automatic update restarts

1. **Simplest for a 1-2 week window:** Settings > Windows Update > **Pause updates** (pick the longest period offered).
2. Settings > Windows Update > Advanced options > **Active hours** > Manual > set the maximum range (for example 06:00 to 00:00).
3. **(Admin)** No automatic restart while a user is signed in (honoured on Pro/Enterprise; on Home it is best effort, so keep step 1):

```powershell
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU" /v NoAutoRebootWithLoggedOnUsers /t REG_DWORD /d 1 /f
```

### 2.4 Recover from reboots or power cuts

The scheduled task in section 6 starts on user logon, so enable automatic sign-in on this dedicated laptop: press `Win+R`, run `netplwiz`, untick **Users must enter a user name and password to use this computer**, enter the account password. In BIOS/UEFI, set **Restore on AC power loss** to *Power On* if the option exists. Use BitLocker or keep the laptop physically secure, because auto-login removes the sign-in barrier.

No router port forwarding or inbound firewall rule is needed: the tunnel only makes outbound connections.

---

## 3. Python environment and OCR pre-warm

```powershell
cd C:\Kompta\app
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

No other system dependencies are needed: the OpenCV, ONNX Runtime and pypdfium2 wheels bundle their native libraries (the VC++ runtime from section 1 is the only prerequisite).

Pre-download the French OCR models now (internet needed once), so Mourad's first upload is not slow or failing. The parameters mirror `core/ocr_service.py`:

```powershell
.\.venv\Scripts\python.exe -c "import numpy as np; from rapidocr import RapidOCR; e = RapidOCR(params={'Det.lang_type': 'fr', 'Rec.lang_type': 'fr'}); e(np.full((64, 256, 3), 255, dtype=np.uint8)); print('OCR warm-up OK')"
```

Expected: model download progress, then `OCR warm-up OK`. Confirm the models were saved:

```powershell
Get-ChildItem .\.venv\Lib\site-packages\rapidocr\models -Filter *.onnx
```

---

## 4. Secrets, backend and local smoke test

### 4.1 Generate the password

```powershell
-join ((48..57)+(65..90)+(97..122) | Get-Random -Count 20 | ForEach-Object { [char]$_ })
```

Use letters and digits only: characters such as `%`, `&`, `^` or `"` break batch files.

### 4.2 `C:\KomptaOps\env.bat`

Create it in Notepad (outside the repo, never commit it):

```bat
@echo off
set KOMPTA_BASIC_AUTH=mourad:PASTE-THE-GENERATED-PASSWORD
set KOMPTA_REQUIRE_AUTH=1
set KOMPTA_DATABASE_PATH=C:\KomptaData\kompta.sqlite3
set NGROK_DOMAIN=YOUR-STATIC-DOMAIN.ngrok-free.app
```

### 4.3 `C:\KomptaOps\run-backend.bat`

```bat
@echo off
call C:\KomptaOps\env.bat
cd /d C:\Kompta\app
:loop
if exist "C:\KomptaOps\STOP" exit /b
echo [%date% %time%] backend starting >> C:\KomptaOps\logs\backend.log
.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1 >> C:\KomptaOps\logs\backend.log 2>&1
echo [%date% %time%] backend exited with code %errorlevel%, restarting >> C:\KomptaOps\logs\backend.log
ping -n 6 127.0.0.1 >nul
goto loop
```

- Bind to `127.0.0.1`, not `0.0.0.0`: the tunnel runs on the same machine, so the app is never exposed on the LAN.
- **One worker only**, because SQLite is a single-writer file and the journal is append-only.
- `ping` is used as the delay because `timeout` fails inside non-interactive scheduled tasks.

### 4.4 Smoke test (before the tunnel)

In one window run `C:\KomptaOps\run-backend.bat`. In another:

```powershell
curl.exe -i http://127.0.0.1:8000/health                                  # 200 {"status":"ok"}
curl.exe -i http://127.0.0.1:8000/                                        # 401
curl.exe -i -u "mourad:YOUR-PASSWORD" http://127.0.0.1:8000/              # 200, HTML
curl.exe -i -u "mourad:YOUR-PASSWORD" http://127.0.0.1:8000/api/clients   # 200 []
```

If you get `RuntimeError: KOMPTA_REQUIRE_AUTH=1 but KOMPTA_BASIC_AUTH is not set`, `env.bat` was not loaded. Stop the loop with `Ctrl+C` after the test.

---

## 5. Stable HTTPS URL (ngrok static domain)

1. Create a free account at https://dashboard.ngrok.com.
2. Dashboard > **Domains** > create your free static domain (for example `kompta-mourad.ngrok-free.app`) and put it in `NGROK_DOMAIN` in `env.bat`.
3. Dashboard > **Your Authtoken**, then:

```powershell
ngrok config add-authtoken <YOUR_AUTHTOKEN>
```

4. **`C:\KomptaOps\run-tunnel.bat`**:

```bat
@echo off
call C:\KomptaOps\env.bat
:loop
if exist "C:\KomptaOps\STOP" exit /b
echo [%date% %time%] tunnel starting >> C:\KomptaOps\logs\tunnel.log
ngrok http 8000 --url=%NGROK_DOMAIN% --log=stdout >> C:\KomptaOps\logs\tunnel.log 2>&1
echo [%date% %time%] tunnel exited with code %errorlevel%, restarting >> C:\KomptaOps\logs\tunnel.log
ping -n 11 127.0.0.1 >nul
goto loop
```

Notes:
- On older ngrok agents the flag is `--domain=` instead of `--url=`.
- Free ngrok shows a one-time "You are about to visit..." interstitial in the browser. Tell Mourad to click **Visit Site**.
- The free plan has monthly bandwidth/request limits, so check the current numbers on ngrok's pricing page. Exports are small, and OCR uploads are capped at 10 MB each.
- **Alternative, Cloudflare Tunnel:** a *named* tunnel is free but needs a domain on Cloudflare (about $10/year). Quick tunnels (`cloudflared tunnel --url ...`) get a new random URL on every restart, which is unsuitable here and would also wipe Mourad's browser `localStorage` state. Named-tunnel flow: `cloudflared tunnel login`, `cloudflared tunnel create kompta`, `cloudflared tunnel route dns kompta kompta.<yourdomain>`, then `cloudflared tunnel run --url http://127.0.0.1:8000 kompta` in place of the ngrok line.

---

## 6. Auto-start and crash recovery

### 6.1 `C:\KomptaOps\start-all.bat`

```bat
@echo off
if exist "C:\KomptaOps\STOP" del "C:\KomptaOps\STOP"
start "Kompta backend" /min cmd /c C:\KomptaOps\run-backend.bat
ping -n 6 127.0.0.1 >nul
call C:\KomptaOps\run-tunnel.bat
```

Recovery layers:
- Each `run-*.bat` loop restarts its process 5-10 seconds after any crash.
- The scheduled task restarts the whole script if it dies, and starts it on every logon after a reboot.

### 6.2 Register the task

PowerShell (as the same user):

```powershell
$action   = New-ScheduledTaskAction -Execute 'C:\KomptaOps\start-all.bat'
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1) `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'Kompta' -Action $action -Trigger $trigger -Settings $settings
Start-ScheduledTask -TaskName 'Kompta'
```

`-ExecutionTimeLimit ([TimeSpan]::Zero)` is essential: tasks otherwise get killed after 72 hours by default.

Two console windows stay open (backend minimized, tunnel visible). Do not close them.

### 6.3 Verify end to end

```powershell
curl.exe -i https://YOUR-STATIC-DOMAIN.ngrok-free.app/health
```

Then open `https://YOUR-STATIC-DOMAIN.ngrok-free.app/` from a phone on mobile data, accept the ngrok interstitial, and log in. Finally **restart the laptop** and confirm the URL works again without touching it.

---

## 7. SQLite backup

The SQLite backup API is safe while the app is running. A plain file copy can capture a half-written file.

### 7.1 `C:\KomptaOps\backup.bat`

```bat
@echo off
C:\Kompta\app\.venv\Scripts\python.exe -c "import sqlite3,datetime as d;s=sqlite3.connect(r'C:\KomptaData\kompta.sqlite3');t=sqlite3.connect(r'C:\KomptaBackups\kompta-'+d.datetime.now().strftime('%%Y%%m%%d-%%H%%M')+'.sqlite3');s.backup(t);t.close();s.close()"
forfiles /p C:\KomptaBackups /m kompta-*.sqlite3 /d -14 /c "cmd /c del @path" 2>nul
```

(`%%` is required inside a `.bat`. When typing the Python one-liner directly in a terminal, use single `%`.) The second line deletes backups older than 14 days.

### 7.2 Run it hourly

```powershell
$action  = New-ScheduledTaskAction -Execute 'C:\KomptaOps\backup.bat'
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName 'Kompta Backup' -Action $action -Trigger $trigger
Start-ScheduledTask -TaskName 'Kompta Backup'
Get-ChildItem C:\KomptaBackups
```

The backups sit on the same disk. Once a day, copy `C:\KomptaBackups` to a USB drive or a OneDrive folder. Browser-side data (custom accounts, liasse state in `localStorage`) is **not** in the DB or its backups.

**Restore:** stop Kompta (section 8), copy the chosen backup over `C:\KomptaData\kompta.sqlite3` (keep the old file renamed), and start Kompta again.

---

## 8. Operations

### Stop, update and restart

```powershell
New-Item C:\KomptaOps\STOP -ItemType File -Force          # tells both loops not to restart
Stop-ScheduledTask -TaskName 'Kompta'
Get-Process ngrok -ErrorAction SilentlyContinue | Stop-Process -Force
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object CommandLine -like '*uvicorn*' | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

cd C:\Kompta\app
git pull
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

Start-ScheduledTask -TaskName 'Kompta'                     # start-all.bat removes STOP itself
```

### Daily health check (about 1 minute)

- `curl.exe https://YOUR-STATIC-DOMAIN.ngrok-free.app/health` returns `{"status":"ok"}`.
- `Get-ChildItem C:\KomptaBackups | Sort-Object LastWriteTime -Descending | Select-Object -First 3` shows a backup from the last hour.
- `Get-Content C:\KomptaOps\logs\backend.log -Tail 30` shows no repeating "restarting" lines.
- `Get-PSDrive C` shows enough free disk space (OCR source files are stored inside the DB).

### Troubleshooting

| Symptom | Check |
|---|---|
| URL shows ngrok error `ERR_NGROK_...` | `tunnel.log`; confirm the authtoken and the domain in `env.bat`. |
| 502 or connection refused from ngrok | Backend is down: read `backend.log`, and run `curl.exe http://127.0.0.1:8000/health` locally. |
| Login prompt keeps reappearing | Wrong password in `env.bat`. Edit it, then do the stop and start procedure. |
| OCR upload fails or is slow the first time | Re-run the pre-warm from section 3. |
| Laptop asleep after a while | Re-check section 2.1 with `powercfg /query`. |
| Everything dead after a reboot | Confirm auto sign-in (2.4) and the `Kompta` task in Task Scheduler. |

### Hand-over to Mourad

Send the URL and the password through two separate channels. Tell him to:
- always use the same browser and the exact same URL, because custom accounts and liasse state live in that browser's `localStorage`;
- click **Visit Site** on the first ngrok warning page;
- expect that journal entries are append-only: mistakes are fixed by reversal, not deletion.

When testing ends, stop the task (section 8), delete `C:\KomptaOps\env.bat`, and revoke the ngrok authtoken in the ngrok dashboard.
