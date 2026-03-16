import os
import re
import csv
import json
import asyncio
import aiosqlite
import httpx
import io
from datetime import datetime, timezone
from typing import Optional
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="HackerOne SourceCode Programs")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = "/var/www/h1scope/backend/cache.db"
H1_API_BASE = "https://api.hackerone.com/v1"
SYNC_LOCK = asyncio.Lock()
sync_status = {"running": False, "last_sync": None, "total": 0, "error": None, "progress": ""}
SYNC_COOLDOWN_SECONDS = 300  # minimum 5 minutes between syncs


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS programs (
                handle TEXT PRIMARY KEY,
                name TEXT,
                logo_url TEXT,
                github_urls TEXT,
                offers_bounties INTEGER,
                max_severity TEXT,
                submission_state TEXT,
                program_type TEXT,
                scope_count INTEGER,
                updated_at TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await db.commit()


@app.on_event("startup")
async def startup():
    await init_db()


def get_credentials():
    identifier = os.getenv("HACKERONE_API_IDENTIFIER", "")
    token = os.getenv("HACKERONE_API_TOKEN", "")
    if not identifier or not token or identifier == "your_api_identifier_here":
        return None, None
    return identifier, token


_HANDLE_RE = re.compile(r'^[a-zA-Z0-9_\-]+$')

def is_safe_handle(handle: str) -> bool:
    """Validate program handle before using it in a URL path."""
    return bool(handle and len(handle) <= 100 and _HANDLE_RE.match(handle))


def is_safe_https_url(url: str) -> bool:
    """Only allow http/https URLs — blocks javascript: data: etc."""
    if not url or not isinstance(url, str):
        return False
    lower = url.strip().lower()
    return lower.startswith("https://") or lower.startswith("http://")


def is_git_url(identifier: str) -> bool:
    lower = identifier.lower()
    return any(host in lower for host in ("github.com", "gitlab.com", "bitbucket.org"))


def row_to_dict(row) -> dict:
    item = dict(row)
    item["github_urls"] = json.loads(item["github_urls"] or "[]")
    return item


async def fetch_structured_scopes(handle: str, auth: tuple, client: httpx.AsyncClient) -> list:
    """Fetch ALL structured scopes for a program via the dedicated scopes endpoint."""
    scopes = []
    page = 1
    while True:
        try:
            resp = await client.get(
                f"{H1_API_BASE}/hackers/programs/{handle}/structured_scopes",
                auth=auth,
                headers={"Accept": "application/json", "User-Agent": "H1ScopeDashboard/1.0"},
                params={"page[number]": page, "page[size]": 100},
                timeout=30.0,
            )
            if resp.status_code == 404:
                break
            if resp.status_code == 429:
                await asyncio.sleep(10)
                continue
            resp.raise_for_status()
            data = resp.json()
            batch = data.get("data", [])
            scopes.extend(batch)
            if not data.get("links", {}).get("next"):
                break
            page += 1
        except Exception:
            break
    return scopes


async def fetch_all_programs(auth: tuple) -> list:
    """Fetch all programs from HackerOne API, paginating through all pages."""
    programs = []
    page = 1
    async with httpx.AsyncClient(timeout=60.0) as client:
        while True:
            try:
                resp = await client.get(
                    f"{H1_API_BASE}/hackers/programs",
                    params={"page[number]": page, "page[size]": 100},
                    auth=auth,
                    headers={"Accept": "application/json", "User-Agent": "H1ScopeDashboard/1.0"},
                )
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPStatusError as e:
                raise ValueError(f"HackerOne API error {e.response.status_code}: {e.response.text[:500]}")

            items = data.get("data", [])
            if not items:
                break
            programs.extend(items)

            if not data.get("links", {}).get("next"):
                break
            page += 1
            await asyncio.sleep(0.3)

    return programs


async def run_sync():
    """Main sync: fetch programs, get their scopes, filter for SOURCE_CODE with git URLs."""
    global sync_status

    async with SYNC_LOCK:
        sync_status["running"] = True
        sync_status["error"] = None
        sync_status["progress"] = "Fetching program list..."

        try:
            identifier, token = get_credentials()
            if not identifier:
                raise ValueError("HackerOne API credentials not configured in .env file")

            auth = (identifier, token)
            all_programs = await fetch_all_programs(auth)
            total = len(all_programs)
            sync_status["total"] = total
            sync_status["progress"] = f"Fetched {total} programs, scanning scopes..."

            qualifying = []
            processed = 0
            sem = asyncio.Semaphore(5)

            sev_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1, "none": 0}

            async def process_program(prog):
                nonlocal processed
                attrs = prog.get("attributes", {})
                handle = attrs.get("handle", "")
                if not handle:
                    processed += 1
                    return

                if not is_safe_handle(handle):
                    processed += 1
                    return

                async with sem:
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        scopes = await fetch_structured_scopes(handle, auth, client)
                    await asyncio.sleep(0.2)

                github_urls = []
                best_sev = -1
                max_sev = None

                for scope in scopes:
                    sa = scope.get("attributes", {})
                    asset_type = sa.get("asset_type", "")
                    identifier_val = sa.get("asset_identifier", "")
                    eligible = sa.get("eligible_for_submission", True)

                    if not eligible:
                        continue

                    is_source_type = asset_type == "SOURCE_CODE"
                    has_git = is_git_url(identifier_val)

                    if is_source_type or has_git:
                        if identifier_val and identifier_val not in github_urls:
                            github_urls.append(identifier_val)

                        sev = sa.get("max_severity", "")
                        if sev and sev_rank.get(sev, -1) > best_sev:
                            best_sev = sev_rank[sev]
                            max_sev = sev

                if not github_urls:
                    processed += 1
                    return

                profile_pic = attrs.get("profile_picture", {})
                logo_url = None
                if isinstance(profile_pic, dict):
                    candidate = (profile_pic.get("medium") or profile_pic.get("small")
                                 or profile_pic.get("62x62") or profile_pic.get("260x260"))
                    if is_safe_https_url(candidate):
                        logo_url = candidate
                elif isinstance(profile_pic, str) and is_safe_https_url(profile_pic):
                    logo_url = profile_pic

                qualifying.append({
                    "handle": handle,
                    "name": attrs.get("name", handle),
                    "logo_url": logo_url,
                    "github_urls": json.dumps(github_urls),
                    "offers_bounties": 1 if attrs.get("offers_bounties") else 0,
                    "max_severity": max_sev,
                    "submission_state": attrs.get("submission_state"),
                    "program_type": "private" if attrs.get("state") == "soft_launched" else "public",
                    "scope_count": len(github_urls),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })

                processed += 1
                if processed % 50 == 0:
                    sync_status["progress"] = (
                        f"Scanned {processed}/{total} programs, "
                        f"{len(qualifying)} match so far..."
                    )

            await asyncio.gather(*[process_program(p) for p in all_programs])

            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("DELETE FROM programs")
                for p in qualifying:
                    await db.execute("""
                        INSERT OR REPLACE INTO programs
                        (handle, name, logo_url, github_urls, offers_bounties, max_severity,
                         submission_state, program_type, scope_count, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        p["handle"], p["name"], p["logo_url"], p["github_urls"],
                        p["offers_bounties"], p["max_severity"], p["submission_state"],
                        p["program_type"], p["scope_count"], p["updated_at"],
                    ))
                await db.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_sync', ?)",
                    (datetime.now(timezone.utc).isoformat(),)
                )
                await db.commit()

            sync_status["last_sync"] = datetime.now(timezone.utc).isoformat()
            sync_status["total"] = len(qualifying)
            sync_status["progress"] = f"Done — {len(qualifying)} programs with source code scope."

        except Exception as e:
            sync_status["error"] = str(e)
            sync_status["progress"] = f"Failed: {e}"
            raise
        finally:
            sync_status["running"] = False


# ── Query helpers ──────────────────────────────────────────────────────────────

async def query_programs(
    search: Optional[str] = None,
    program_type: Optional[str] = None,
    offers_bounties: Optional[bool] = None,
) -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = "SELECT * FROM programs WHERE 1=1"
        params = []

        if search:
            query += " AND (name LIKE ? OR handle LIKE ? OR github_urls LIKE ?)"
            s = f"%{search}%"
            params.extend([s, s, s])

        if program_type and program_type != "all":
            query += " AND program_type = ?"
            params.append(program_type)

        if offers_bounties is not None:
            query += " AND offers_bounties = ?"
            params.append(1 if offers_bounties else 0)

        query += " ORDER BY offers_bounties DESC, scope_count DESC, name ASC"
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        return [row_to_dict(r) for r in rows]


# ── API routes ─────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    identifier, _ = get_credentials()
    return {
        "status": "ok",
        "credentials_configured": identifier is not None,
        "sync_status": sync_status,
    }


@app.get("/api/programs")
async def get_programs(
    search: Optional[str] = None,
    program_type: Optional[str] = None,
    offers_bounties: Optional[bool] = None,
):
    result = await query_programs(search, program_type, offers_bounties)
    total_repos = sum(p.get("scope_count", 0) for p in result)
    return {"data": result, "total": len(result), "total_repos": total_repos}


@app.get("/api/programs/{handle}")
async def get_program(handle: str):
    if not is_safe_handle(handle):
        raise HTTPException(status_code=400, detail="Invalid program handle")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM programs WHERE handle = ?", (handle,))
        row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Program not found")
    return row_to_dict(row)


@app.get("/api/export")
async def export_programs(
    format: str = "json",
    search: Optional[str] = None,
    program_type: Optional[str] = None,
    offers_bounties: Optional[bool] = None,
):
    if format not in ("json", "csv"):
        raise HTTPException(status_code=400, detail="format must be 'json' or 'csv'")

    rows = await query_programs(search, program_type, offers_bounties)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    if format == "json":
        content = json.dumps({"exported_at": datetime.now(timezone.utc).isoformat(),
                               "total": len(rows), "data": rows}, indent=2)
        return StreamingResponse(
            iter([content]),
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename=h1_sourcecode_{ts}.json"},
        )

    # CSV — one row per program, repos pipe-separated in one column
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_ALL)
    writer.writerow([
        "handle", "name", "program_type", "offers_bounties", "max_severity",
        "submission_state", "scope_count", "repo_urls", "h1_url", "updated_at",
    ])
    for p in rows:
        writer.writerow([
            p["handle"],
            p["name"],
            p["program_type"],
            "yes" if p["offers_bounties"] else "no",
            p["max_severity"] or "",
            p["submission_state"] or "",
            p["scope_count"],
            "|".join(p["github_urls"]),
            f"https://hackerone.com/{p['handle']}",
            p["updated_at"],
        ])

    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=h1_sourcecode_{ts}.csv"},
    )


@app.get("/api/sync/status")
async def get_sync_status():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT value FROM meta WHERE key = 'last_sync'")
        row = await cursor.fetchone()
        last_sync = row["value"] if row else None
        cursor2 = await db.execute("SELECT COUNT(*) as cnt FROM programs")
        count_row = await cursor2.fetchone()
        count = count_row["cnt"] if count_row else 0
        cursor3 = await db.execute("SELECT SUM(scope_count) as total_repos FROM programs")
        repo_row = await cursor3.fetchone()
        total_repos = repo_row["total_repos"] or 0

    return {
        **sync_status,
        "last_sync_db": last_sync,
        "cached_count": count,
        "total_repos": total_repos,
    }


@app.post("/api/sync")
async def trigger_sync(background_tasks: BackgroundTasks):
    if sync_status["running"]:
        raise HTTPException(status_code=409, detail="Sync already in progress")
    last = sync_status.get("last_sync")
    if last:
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
        if elapsed < SYNC_COOLDOWN_SECONDS:
            wait = int(SYNC_COOLDOWN_SECONDS - elapsed)
            raise HTTPException(status_code=429, detail=f"Sync cooldown active — wait {wait}s")
    background_tasks.add_task(run_sync)
    return {"message": "Sync started", "status": "running"}
