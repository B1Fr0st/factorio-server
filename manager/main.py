import asyncio
import json
import os
import re
import shutil
import tarfile
import tempfile
import zipfile
from collections import deque
from pathlib import Path
from typing import Optional

import aiofiles
import httpx
from bs4 import BeautifulSoup
from fastapi import (
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
FACTORIO_BIN = Path("/opt/factorio/bin/x64/factorio")
DATA_DIR = Path(os.getenv("FACTORIO_DIR", "/factorio"))
SAVES_DIR = DATA_DIR / "saves"
MODS_DIR = DATA_DIR / "mods"
CONFIG_DIR = DATA_DIR / "config"
SERVER_SETTINGS = CONFIG_DIR / "server-settings.json"
MAP_GEN_SETTINGS = CONFIG_DIR / "map-gen-settings.json"
MOD_LIST_FILE = MODS_DIR / "mod-list.json"

MOD_PORTAL = "https://mods.factorio.com"

# Mods bundled with Factorio / Space Age — never downloaded from the portal
BUILTIN_MODS: frozenset[str] = frozenset({
    "base", "core", "elevated-rails", "quality", "space-age",
})

# DLC mods the user can enable/disable but not uninstall
CONFIGURABLE_DLC: tuple[str, ...] = ("space-age", "quality", "elevated-rails")
FACTORIO_DATA_DIR = Path("/opt/factorio/data")

# Target Factorio version for release filtering (major.minor)
FACTORIO_VERSION = os.getenv("FACTORIO_VERSION", "2.0")

# ---------------------------------------------------------------------------
# Dependency resolution helpers
# ---------------------------------------------------------------------------

def _parse_version(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v))

def _version_str(t: tuple[int, ...]) -> str:
    return ".".join(str(x) for x in t)

def _satisfies(ver: tuple, op: str, req: tuple) -> bool:
    if op == ">=": return ver >= req
    if op == "<=": return ver <= req
    if op == "=":  return ver == req
    if op == ">":  return ver > req
    if op == "<":  return ver < req
    return True

def _parse_dep(s: str) -> tuple:
    """Return (name, op|None, ver_tuple|None, kind) where kind ∈ required/optional/incompatible/unordered."""
    s = s.strip()
    if s.startswith("!"):
        kind = "incompatible"; s = s[1:].strip()
    elif s.startswith("(?)") or s.startswith("?"):
        kind = "optional"; s = s.lstrip("(?)").strip()
    elif s.startswith("~"):
        kind = "unordered"; s = s[1:].strip()
    else:
        kind = "required"
    m = re.match(r"^([\w\-]+)\s*(>=|<=|=|>|<)\s*(\S+)", s)
    if m:
        return m.group(1), m.group(2), _parse_version(m.group(3)), kind
    name = re.match(r"^[\w\-]+", s)
    return (name.group(0) if name else s), None, None, kind

def _compat_releases(releases: list[dict], factorio_version: str) -> list[dict]:
    """Filter to releases for factorio_version (major.minor). Returns empty if none match — no fallback."""
    mm = ".".join(factorio_version.split(".")[:2])
    return [r for r in releases if r.get("info_json", {}).get("factorio_version", "").startswith(mm)]

def _best_release(releases: list[dict], constraints: list[tuple]) -> dict | None:
    """Latest release satisfying every (op, ver_tuple) constraint."""
    for rel in reversed(releases):
        rv = _parse_version(rel.get("version", "0"))
        if all(_satisfies(rv, op, req) for op, req in constraints):
            return rel
    return None

async def _collect_dep_graph(
    root: str,
    client: httpx.AsyncClient,
    active_builtins: frozenset[str] = BUILTIN_MODS,
) -> tuple[dict[str, dict], dict[str, list[tuple]], list[tuple[str, str]], list[str]]:
    """
    BFS over the portal to build:
      mod_info        : name → full API response  (only mods installable on this server)
      version_constraints : name → [(op, ver_tuple)]  accumulated from all dependents
      incompat_pairs  : [(A, B)] meaning A declares '! B'
      errors          : skip reasons (version mismatch, not found, blocked by builtin incompat)
    active_builtins   : BUILTIN_MODS filtered to those actually enabled — incompatibilities
                        against disabled builtins are ignored.
    """
    mod_info: dict[str, dict] = {}
    version_constraints: dict[str, list[tuple]] = {root: []}
    incompat_pairs: list[tuple[str, str]] = []
    errors: list[str] = []
    queue = [root]

    while queue:
        name = queue.pop(0)
        if name in BUILTIN_MODS or name in mod_info:
            continue
        try:
            r = await client.get(
                f"{MOD_PORTAL}/api/mods/{name}/full",
                headers={"User-Agent": "factorio-manager/1.0"},
            )
            if r.status_code == 404:
                errors.append(f"Mod '{name}' not found on portal")
                continue
            r.raise_for_status()
            info = r.json()
        except httpx.RequestError as exc:
            errors.append(f"Network error fetching '{name}': {exc}")
            continue

        all_releases = info.get("releases", [])
        releases = _compat_releases(all_releases, FACTORIO_VERSION)
        if not releases:
            latest_req = (all_releases[-1].get("info_json", {}).get("factorio_version", "?")
                          if all_releases else "unknown")
            errors.append(
                f"Skipping '{name}': no Factorio {FACTORIO_VERSION} release "
                f"(latest requires {latest_req})"
            )
            continue

        latest = releases[-1]
        deps = latest.get("info_json", {}).get("dependencies", [])

        # Block mods that declare incompatibility with an *enabled* builtin
        builtin_conflicts = [
            dep_name
            for dep_str in deps
            for dep_name, _, _, kind in [_parse_dep(dep_str)]
            if kind == "incompatible" and dep_name in active_builtins
        ]
        if builtin_conflicts:
            errors.append(
                f"Skipping '{name}': incompatible with "
                f"{', '.join(repr(b) for b in builtin_conflicts)} (enabled)"
            )
            continue

        mod_info[name] = info

        for dep_str in deps:
            dep_name, op, ver, kind = _parse_dep(dep_str)
            if kind == "incompatible":
                incompat_pairs.append((name, dep_name))
                continue
            if kind == "optional":
                continue
            if dep_name in BUILTIN_MODS:
                continue
            if dep_name not in version_constraints:
                version_constraints[dep_name] = []
                queue.append(dep_name)
            if op and ver is not None:
                version_constraints[dep_name].append((op, ver))

    return mod_info, version_constraints, incompat_pairs, errors

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
MANAGER_PASSWORD = os.getenv("MANAGER_PASSWORD", "")


def require_auth(x_manager_password: Optional[str] = Header(default=None)) -> None:
    if not MANAGER_PASSWORD:
        return
    if x_manager_password != MANAGER_PASSWORD:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ---------------------------------------------------------------------------
# Global process state
# ---------------------------------------------------------------------------
factorio_proc: Optional[asyncio.subprocess.Process] = None
log_buffer: deque = deque(maxlen=2000)
log_subscribers: set[WebSocket] = set()
server_lock = asyncio.Lock()


def _build_start_cmd() -> list[str]:
    saves = sorted(SAVES_DIR.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not saves:
        raise HTTPException(status_code=409, detail="No save files found. Upload a save first.")
    active_save = os.getenv("FACTORIO_SAVE", str(saves[0]))
    cmd = [
        str(FACTORIO_BIN),
        "--start-server", active_save,
        "--server-settings", str(SERVER_SETTINGS),
        "--mod-directory", str(MODS_DIR),
    ]
    if (CONFIG_DIR / "server-banlist.json").exists():
        cmd += ["--server-banlist", str(CONFIG_DIR / "server-banlist.json")]
    if (CONFIG_DIR / "server-whitelist.json").exists():
        cmd += ["--server-whitelist", str(CONFIG_DIR / "server-whitelist.json")]
    rcon_port = os.getenv("RCON_PORT", "27015")
    rcon_password = os.getenv("RCON_PASSWORD", "")
    if rcon_password:
        cmd += ["--rcon-port", rcon_port, "--rcon-password", rcon_password]
    return cmd


async def _stream_output(stream: asyncio.StreamReader, prefix: str) -> None:
    while True:
        line_bytes = await stream.readline()
        if not line_bytes:
            break
        line = line_bytes.decode("utf-8", errors="replace").rstrip()
        entry = f"{prefix}{line}"
        log_buffer.append(entry)
        dead = set()
        for ws in log_subscribers:
            try:
                await ws.send_text(entry)
            except Exception:
                dead.add(ws)
        log_subscribers.difference_update(dead)


async def _wait_for_proc() -> None:
    global factorio_proc
    if factorio_proc:
        await factorio_proc.wait()
        exit_line = f"[manager] Factorio process exited (code {factorio_proc.returncode})"
        log_buffer.append(exit_line)
        for ws in list(log_subscribers):
            try:
                await ws.send_text(exit_line)
            except Exception:
                pass
        factorio_proc = None


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="Factorio Manager", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
async def _startup() -> None:
    for d in (SAVES_DIR, MODS_DIR, CONFIG_DIR):
        d.mkdir(parents=True, exist_ok=True)
    if not SERVER_SETTINGS.exists():
        SERVER_SETTINGS.write_text(json.dumps(_default_server_settings(), indent=2))
    if not MOD_LIST_FILE.exists():
        MOD_LIST_FILE.write_text(json.dumps({"mods": [{"name": "base", "enabled": True}]}, indent=2))


# ---------------------------------------------------------------------------
# Server control
# ---------------------------------------------------------------------------
@app.get("/api/status")
async def get_status() -> dict:
    running = factorio_proc is not None and factorio_proc.returncode is None
    return {"running": running, "pid": factorio_proc.pid if running else None}


@app.post("/api/start", dependencies=[Depends(require_auth)])
async def start_server() -> dict:
    global factorio_proc
    async with server_lock:
        if factorio_proc is not None and factorio_proc.returncode is None:
            return {"ok": True, "message": "Already running"}
        cmd = _build_start_cmd()
        log_buffer.append(f"[manager] Starting: {' '.join(cmd)}")
        factorio_proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(DATA_DIR),
        )
        asyncio.create_task(_stream_output(factorio_proc.stdout, ""))
        asyncio.create_task(_stream_output(factorio_proc.stderr, "[err] "))
        asyncio.create_task(_wait_for_proc())
        return {"ok": True, "pid": factorio_proc.pid}


@app.post("/api/stop", dependencies=[Depends(require_auth)])
async def stop_server() -> dict:
    global factorio_proc
    async with server_lock:
        if factorio_proc is None or factorio_proc.returncode is not None:
            return {"ok": True, "message": "Not running"}
        factorio_proc.terminate()
        try:
            await asyncio.wait_for(factorio_proc.wait(), timeout=15)
        except asyncio.TimeoutError:
            factorio_proc.kill()
        factorio_proc = None
        return {"ok": True}


@app.post("/api/restart", dependencies=[Depends(require_auth)])
async def restart_server() -> dict:
    await stop_server()
    await asyncio.sleep(1)
    return await start_server()


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------
@app.get("/api/logs")
async def get_logs(lines: int = 200) -> dict:
    recent = list(log_buffer)[-lines:]
    return {"lines": recent}


@app.websocket("/api/logs/stream")
async def stream_logs(ws: WebSocket) -> None:
    await ws.accept()
    # Send buffer first
    for line in list(log_buffer)[-200:]:
        await ws.send_text(line)
    log_subscribers.add(ws)
    try:
        while True:
            # Keep connection alive; client can close
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        log_subscribers.discard(ws)


# ---------------------------------------------------------------------------
# Saves
# ---------------------------------------------------------------------------
@app.get("/api/saves")
async def list_saves() -> dict:
    saves = []
    for p in sorted(SAVES_DIR.glob("*.zip"), key=lambda x: x.stat().st_mtime, reverse=True):
        stat = p.stat()
        saves.append({"name": p.name, "size": stat.st_size, "modified": stat.st_mtime})
    active = os.getenv("FACTORIO_SAVE", saves[0]["name"] if saves else None)
    return {"saves": saves, "active": active}


@app.post("/api/saves", dependencies=[Depends(require_auth)])
async def upload_save(file: UploadFile = File(...)) -> dict:
    if not file.filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="Save files must be .zip")
    dest = SAVES_DIR / file.filename
    async with aiofiles.open(dest, "wb") as f:
        while chunk := await file.read(1 << 20):
            await f.write(chunk)
    return {"ok": True, "name": file.filename, "size": dest.stat().st_size}


@app.get("/api/saves/{name}")
async def download_save(name: str) -> FileResponse:
    p = SAVES_DIR / name
    if not p.exists() or p.suffix != ".zip":
        raise HTTPException(status_code=404)
    return FileResponse(str(p), filename=name, media_type="application/zip")


@app.delete("/api/saves/{name}", dependencies=[Depends(require_auth)])
async def delete_save(name: str) -> dict:
    p = SAVES_DIR / name
    if not p.exists():
        raise HTTPException(status_code=404)
    p.unlink()
    return {"ok": True}


@app.post("/api/saves/new", dependencies=[Depends(require_auth)])
async def create_new_save(body: dict) -> dict:
    name = re.sub(r"[^\w\-]", "_", body.get("name", "new-world").strip()) or "new-world"
    if not name.endswith(".zip"):
        name += ".zip"
    dest = SAVES_DIR / name
    if dest.exists():
        raise HTTPException(status_code=409, detail=f"{name} already exists")

    cmd = [str(FACTORIO_BIN), "--create", str(dest), "--mod-directory", str(MODS_DIR)]
    if MAP_GEN_SETTINGS.exists():
        cmd += ["--map-gen-settings", str(MAP_GEN_SETTINGS)]

    log_buffer.append(f"[manager] Generating world: {dest.name} …")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Map generation timed out after 180 s")

    output = (stdout + stderr).decode("utf-8", errors="replace").strip()
    for line in output.splitlines():
        log_buffer.append(line)

    if proc.returncode != 0 or not dest.exists():
        raise HTTPException(
            status_code=500,
            detail=f"factorio --create exited {proc.returncode}: {output[-300:]}",
        )

    log_buffer.append(f"[manager] World created: {dest.name}")
    return {"ok": True, "name": dest.name, "size": dest.stat().st_size}


# ---------------------------------------------------------------------------
# Mods
# ---------------------------------------------------------------------------
def _read_mod_list() -> list[dict]:
    if MOD_LIST_FILE.exists():
        return json.loads(MOD_LIST_FILE.read_text()).get("mods", [])
    return [{"name": "base", "enabled": True}]


def _write_mod_list(mods: list[dict]) -> None:
    MOD_LIST_FILE.write_text(json.dumps({"mods": mods}, indent=2))


def _installed_mods() -> dict[str, dict]:
    """Return name -> {version, path} for every .zip in the mods dir."""
    result = {}
    for p in MODS_DIR.glob("*.zip"):
        # Factorio mod zips are named <name>_<version>.zip
        m = re.match(r"^(.+)_(\d+\.\d+\.\d+)\.zip$", p.name)
        if m:
            result[m.group(1)] = {"version": m.group(2), "path": str(p)}
    return result


def _dlc_version(name: str) -> str:
    try:
        info = json.loads((FACTORIO_DATA_DIR / name / "info.json").read_text())
        return info.get("version", "?")
    except Exception:
        return "?"


def _factorio_version() -> str:
    try:
        return json.loads((FACTORIO_DATA_DIR / "base" / "info.json").read_text()).get("version", "?")
    except Exception:
        return "?"


@app.get("/api/mods")
async def list_mods() -> dict:
    installed = _installed_mods()
    enabled_map = {m["name"]: m["enabled"] for m in _read_mod_list()}
    mods = []
    # DLC mods first — always present, just toggleable
    for name in CONFIGURABLE_DLC:
        mods.append({
            "name": name,
            "version": _dlc_version(name),
            "enabled": enabled_map.get(name, True),
            "builtin": True,
        })
    # User-installed mods
    for name, info in installed.items():
        mods.append({
            "name": name,
            "version": info["version"],
            "enabled": enabled_map.get(name, True),
            "builtin": False,
        })
    return {"mods": mods}


@app.patch("/api/mods/{name}", dependencies=[Depends(require_auth)])
async def set_mod_enabled(name: str, body: dict) -> dict:
    """Body: {"enabled": true/false}"""
    mods = _read_mod_list()
    found = False
    for m in mods:
        if m["name"] == name:
            m["enabled"] = bool(body.get("enabled", True))
            found = True
            break
    if not found:
        mods.append({"name": name, "enabled": bool(body.get("enabled", True))})
    _write_mod_list(mods)
    return {"ok": True}


@app.post("/api/mods/upload", dependencies=[Depends(require_auth)])
async def upload_mod(file: UploadFile = File(...)) -> dict:
    if not file.filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="Mod files must be .zip")
    dest = MODS_DIR / file.filename
    async with aiofiles.open(dest, "wb") as f:
        while chunk := await file.read(1 << 20):
            await f.write(chunk)
    m = re.match(r"^(.+)_(\d+\.\d+\.\d+)\.zip$", file.filename)
    if not m:
        dest.unlink()
        raise HTTPException(status_code=400, detail="Filename must be <name>_<version>.zip")
    mod_name = m.group(1)
    mods = _read_mod_list()
    if not any(x["name"] == mod_name for x in mods):
        mods.append({"name": mod_name, "enabled": True})
        _write_mod_list(mods)
    return {"ok": True, "name": mod_name, "version": m.group(2)}


@app.post("/api/mods/install", dependencies=[Depends(require_auth)])
async def install_mod_from_portal(body: dict) -> dict:
    """Body: {"name": "...", "username": "...", "token": "..."}"""
    mod_name = body.get("name", "").strip()
    username = body.get("username", os.getenv("FACTORIO_USERNAME", ""))
    token = body.get("token", os.getenv("FACTORIO_TOKEN", ""))
    if not mod_name:
        raise HTTPException(status_code=400, detail="name is required")
    if not username or not token:
        raise HTTPException(status_code=400, detail="username and token are required")

    currently_installed = _installed_mods()  # name -> {version, path}

    # Only treat builtins that are actually enabled as "present" for conflict checks
    enabled_map = {m["name"]: m["enabled"] for m in _read_mod_list()}
    active_builtins = frozenset(
        name for name in BUILTIN_MODS if enabled_map.get(name, True)
    )

    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        # --- dependency resolution ---
        mod_info, version_constraints, incompat_pairs, errors = await _collect_dep_graph(
            mod_name, client, active_builtins
        )

        if mod_name not in mod_info:
            detail = errors[0] if errors else f"Mod '{mod_name}' not found"
            raise HTTPException(status_code=404, detail=detail)

        install_plan: list[dict] = []
        skipped: list[dict] = []
        # errors from dep graph (skipped mods) go in as informational conflicts
        conflicts: list[str] = list(errors)

        # Check actual incompatibilities: pairs where both mods will be present
        all_present = set(mod_info) | set(currently_installed) | active_builtins
        seen_pairs: set[frozenset] = set()
        for a, b in incompat_pairs:
            if b in all_present:
                pair: frozenset = frozenset([a, b])
                if pair not in seen_pairs:
                    seen_pairs.add(pair)
                    conflicts.append(f"'{a}' is incompatible with '{b}'")

        for name, info in mod_info.items():
            reqs = list(version_constraints.get(name, []))

            releases = _compat_releases(info.get("releases", []), FACTORIO_VERSION)
            if not releases:
                # Should not happen — _collect_dep_graph already filtered these out
                continue

            if name in currently_installed:
                inst_v = _parse_version(currently_installed[name]["version"])
                unsatisfied = [f"{op} {_version_str(req)}" for op, req in reqs if not _satisfies(inst_v, op, req)]
                if not unsatisfied:
                    skipped.append({"name": name, "version": currently_installed[name]["version"], "reason": "already installed"})
                    continue
                # Needs upgrade — find a release satisfying all constraints
                best = _best_release(releases, reqs)
                if best is None:
                    conflicts.append(
                        f"Cannot upgrade '{name}': installed {currently_installed[name]['version']} "
                        f"doesn't satisfy {', '.join(unsatisfied)} and no compatible release found"
                    )
                    continue
                install_plan.append({**best, "name": name, "is_dependency": name != mod_name, "upgrade": True,
                                     "old_version": currently_installed[name]["version"]})
            else:
                best = _best_release(releases, reqs)
                if best is None:
                    # Pick latest and report conflict
                    best = releases[-1]
                    rv = _parse_version(best["version"])
                    bad = [f"{op} {_version_str(req)}" for op, req in reqs if not _satisfies(rv, op, req)]
                    conflicts.append(
                        f"Version conflict for '{name}': no release satisfies {', '.join(bad)}; "
                        f"installing latest ({best['version']})"
                    )
                install_plan.append({**best, "name": name, "is_dependency": name != mod_name, "upgrade": False})

        # --- download ---
        installed_results: list[dict] = []

        for mod in install_plan:
            dl_url = f"{MOD_PORTAL}{mod['download_url']}?username={username}&token={token}"
            dest = MODS_DIR / mod["file_name"]
            tmp = dest.with_suffix(".tmp")
            try:
                async with client.stream("GET", dl_url) as resp:
                    if resp.status_code == 403:
                        raise HTTPException(status_code=401, detail="Invalid username or token")
                    resp.raise_for_status()
                    async with aiofiles.open(tmp, "wb") as f:
                        async for chunk in resp.aiter_bytes(1 << 20):
                            await f.write(chunk)
                tmp.rename(dest)
            except HTTPException:
                tmp.unlink(missing_ok=True)
                raise
            except Exception as exc:
                tmp.unlink(missing_ok=True)
                conflicts.append(f"Download failed for '{mod['name']}': {exc}")
                continue

            # Remove older zip versions of the same mod to avoid Factorio confusion
            entry_name = mod["name"]
            for old in MODS_DIR.glob(f"{entry_name}_*.zip"):
                if old != dest:
                    old.unlink(missing_ok=True)

            # Update mod-list.json
            mods_list = _read_mod_list()
            existing = next((x for x in mods_list if x["name"] == entry_name), None)
            if existing:
                existing["enabled"] = True
            else:
                mods_list.append({"name": entry_name, "enabled": True})
            _write_mod_list(mods_list)

            log_buffer.append(
                f"[manager] {'Upgraded' if mod['upgrade'] else 'Installed'} "
                f"{entry_name} {mod['version']}"
                + (f" (was {mod['old_version']})" if mod.get('upgrade') else "")
                + (" [dependency]" if mod['is_dependency'] else "")
            )
            installed_results.append({
                "name": entry_name,
                "version": mod["version"],
                "is_dependency": mod["is_dependency"],
                "upgrade": mod.get("upgrade", False),
            })

    server_running = factorio_proc is not None and factorio_proc.returncode is None
    return {
        "ok": True,
        "installed": installed_results,
        "skipped": skipped,
        "conflicts": conflicts,
        "restart_required": server_running,
    }


@app.get("/api/mods/search")
async def search_mods(q: str) -> dict:
    # The portal uses htmx: the search box fires GET /search?query=<q>
    # which returns a server-rendered HTML fragment — the only working search.
    # /api/mods?q= silently ignores the query and returns all mods alphabetically.
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(
                f"{MOD_PORTAL}/search",
                params={"query": q},
                headers={"User-Agent": "Mozilla/5.0 factorio-manager/1.0"},
            )
            r.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"Mod portal returned {e.response.status_code}")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Could not reach mod portal: {e}")

    soup = BeautifulSoup(r.text, "html.parser")
    results = []
    seen: set[str] = set()

    for card in soup.select(".mod-list .panel-inset-lighter.flex-column"):
        link = card.select_one('a[href*="/mod/"]')
        if not link:
            continue
        m = re.search(r"/mod/([^/?#]+)", link.get("href", ""))
        if not m:
            continue
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)

        h2 = card.select_one("h2 a.result-field")
        title = h2.get_text(" ", strip=True) if h2 else name

        p = card.select_one("p.result-field")
        summary = p.get_text(" ", strip=True) if p else ""

        owner_a = card.select_one("a.orange.bold.result-field")
        owner = owner_a.get_text(strip=True) if owner_a else ""

        results.append({"name": name, "title": title, "summary": summary, "owner": owner})

    return {"results": results}


@app.delete("/api/mods/{name}", dependencies=[Depends(require_auth)])
async def delete_mod(name: str) -> dict:
    if name in BUILTIN_MODS:
        raise HTTPException(status_code=400, detail=f"'{name}' is a built-in mod and cannot be removed")
    installed = _installed_mods()
    if name not in installed:
        raise HTTPException(status_code=404)
    Path(installed[name]["path"]).unlink()
    mods = [m for m in _read_mod_list() if m["name"] != name]
    _write_mod_list(mods)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@app.get("/api/config")
async def get_config() -> dict:
    if not SERVER_SETTINGS.exists():
        return _default_server_settings()
    return json.loads(SERVER_SETTINGS.read_text())


@app.put("/api/config", dependencies=[Depends(require_auth)])
async def put_config(body: dict) -> dict:
    SERVER_SETTINGS.write_text(json.dumps(body, indent=2))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Version management
# ---------------------------------------------------------------------------
FACTORIO_RELEASES_API = "https://factorio.com/api/latest-releases"
FACTORIO_DOWNLOAD_URL = "https://factorio.com/get-download/{version}/headless/linux64"


@app.get("/api/version")
async def get_version() -> dict:
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        try:
            r = await client.get(FACTORIO_RELEASES_API)
            releases = r.json()
            stable = releases.get("stable", {}).get("headless")
            experimental = releases.get("experimental", {}).get("headless")
        except Exception:
            stable = experimental = None
    return {
        "current": _factorio_version(),
        "stable": stable,
        "experimental": experimental,
    }


@app.post("/api/version", dependencies=[Depends(require_auth)])
async def set_version(body: dict) -> dict:
    global factorio_proc
    version = body.get("version", "").strip()
    if not version:
        raise HTTPException(status_code=400, detail="version is required")

    was_running = factorio_proc is not None and factorio_proc.returncode is None
    if was_running:
        factorio_proc.terminate()
        try:
            await asyncio.wait_for(factorio_proc.wait(), timeout=15)
        except asyncio.TimeoutError:
            factorio_proc.kill()
            await factorio_proc.wait()
        factorio_proc = None
        log_buffer.append("[manager] Server stopped for version change")

    url = FACTORIO_DOWNLOAD_URL.format(version=version)
    log_buffer.append(f"[manager] Downloading Factorio {version}…")

    tmp_path = Path(tempfile.mktemp(suffix=".tar.xz"))
    try:
        async with httpx.AsyncClient(timeout=600, follow_redirects=True) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code in (400, 404):
                    raise HTTPException(status_code=400, detail=f"Version '{version}' not found")
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0))
                downloaded = 0
                last_milestone = -1
                async with aiofiles.open(tmp_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(1 << 20):
                        await f.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            milestone = (downloaded * 100 // total) // 25 * 25
                            if milestone > 0 and milestone != last_milestone:
                                last_milestone = milestone
                                log_buffer.append(
                                    f"[manager] Downloading: {milestone}% "
                                    f"({downloaded // 1024 // 1024}/{total // 1024 // 1024} MB)"
                                )

        log_buffer.append("[manager] Extracting…")

        def _extract():
            with tempfile.TemporaryDirectory() as tmpdir:
                with tarfile.open(tmp_path, "r:xz") as tar:
                    tar.extractall(tmpdir)
                extracted = Path(tmpdir) / "factorio"
                if FACTORIO_DATA_DIR.parent.exists():  # /opt/factorio
                    shutil.rmtree(FACTORIO_DATA_DIR.parent, ignore_errors=True)
                shutil.move(str(extracted), str(FACTORIO_DATA_DIR.parent))

        await asyncio.to_thread(_extract)
        tmp_path.unlink(missing_ok=True)

        new_ver = _factorio_version()
        log_buffer.append(f"[manager] Factorio {new_ver} ready")

        if was_running:
            log_buffer.append("[manager] Restarting server…")
            await start_server()

        return {"ok": True, "version": new_ver}

    except HTTPException:
        tmp_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        log_buffer.append(f"[manager] Version change failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------
app.mount("/static", StaticFiles(directory="/manager/static"), name="static")


@app.get("/", include_in_schema=False)
async def root() -> FileResponse:
    return FileResponse("/manager/static/index.html")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _default_server_settings() -> dict:
    return {
        "name": "My Factorio Server",
        "description": "",
        "tags": [],
        "max_players": 0,
        "visibility": {"public": False, "lan": True},
        "username": "",
        "token": "",
        "game_password": "",
        "require_user_verification": True,
        "max_upload_in_kilobytes_per_second": 0,
        "max_upload_slots": 5,
        "minimum_latency_in_ticks": 0,
        "ignore_player_limit_for_returning_players": False,
        "allow_commands": "admins-only",
        "autosave_interval": 10,
        "autosave_slots": 5,
        "afk_autokick_interval": 0,
        "auto_pause": True,
        "only_admins_can_pause_the_game": True,
        "autosave_only_on_server": True,
        "non_blocking_saving": False,
        "minimum_segment_size": 25,
        "minimum_segment_size_peer_count": 20,
        "maximum_segment_size": 100,
        "maximum_segment_size_peer_count": 10,
    }


if __name__ == "__main__":
    port = int(os.getenv("MANAGER_PORT", "8080"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
