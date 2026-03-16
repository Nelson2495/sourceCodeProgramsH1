# HackerOne Source Code Programs Dashboard

A full-stack web application that aggregates all HackerOne bug bounty programs that have **source code repositories** (GitHub, GitLab, Bitbucket) in their scope. Built for security researchers who want to quickly identify which programs offer code-level attack surfaces.

**Live demo:** Designed to run on a single Ubuntu 24.04 VPS behind Apache2.

---

## What It Does

The HackerOne REST API exposes structured scope data for every program. This tool:

1. Paginates through all accessible HackerOne programs (`/v1/hackers/programs`)
2. For each program, fetches its structured scopes (`/v1/hackers/programs/{handle}/structured_scopes`)
3. Filters for scopes where `asset_type == "SOURCE_CODE"` **or** where the `asset_identifier` contains a `github.com`, `gitlab.com`, or `bitbucket.org` URL
4. Stores qualifying programs in a local SQLite cache
5. Serves them through a searchable, filterable dashboard

---

## Screenshots

> Dashboard shows program name, logo, type (Public/Private), bounty status, max severity, and all in-scope repository URLs with direct links.

![alt text](https://github.com/actuallyclover/sourceCodeProgramsH1/blob/main/Screenshot%202026-03-15%20183122.png)

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend API | Python 3 + FastAPI + uvicorn |
| HTTP client | httpx (async) |
| Cache / DB | SQLite via aiosqlite |
| Frontend | Vanilla JS + Tailwind CSS (CDN) |
| Web server | Apache2 (reverse proxy) |
| Process mgmt | systemd |
| OS | Ubuntu 24.04 LTS |

---

## Project Structure

```
sourceCodeProgramsH1/
├── backend/
│   ├── main.py            # FastAPI application — all API logic
│   ├── requirements.txt   # Python dependencies
│   └── .env.example       # Credential template
├── frontend/
│   └── index.html         # Single-page dashboard (self-contained)
├── deploy/
│   ├── h1scope.service    # systemd unit file
│   └── h1scope.conf       # Apache2 virtual host config
├── .gitignore
└── README.md
```

---

## Prerequisites

- Ubuntu 24.04 (or any Debian-based Linux)
- Python 3.10+
- Apache2
- A HackerOne account with API access

---

## HackerOne API Credentials

1. Log in to HackerOne and go to **Settings → API Token**
2. Create a new API token — note the **Identifier** and **Token**
3. Your account must be enrolled in programs to see private ones; public programs are always visible

---

## Installation

### 1. System dependencies

```bash
apt-get update -y && apt-get upgrade -y
apt-get install -y python3 python3-pip python3-venv apache2 curl
a2enmod proxy proxy_http headers rewrite
systemctl enable apache2 && systemctl start apache2
```

### 2. Deploy files

```bash
mkdir -p /var/www/h1scope/backend /var/www/h1scope/frontend

# Copy backend
cp backend/main.py       /var/www/h1scope/backend/
cp backend/requirements.txt /var/www/h1scope/backend/
cp backend/.env.example  /var/www/h1scope/backend/.env   # edit this next

# Copy frontend
cp frontend/index.html   /var/www/h1scope/frontend/
```

### 3. Configure credentials

```bash
nano /var/www/h1scope/backend/.env
```

```env
HACKERONE_API_IDENTIFIER=your_identifier_here
HACKERONE_API_TOKEN=your_token_here
```

### 4. Python virtual environment

```bash
cd /var/www/h1scope/backend
python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt
```

### 5. Fix permissions

```bash
chown -R www-data:www-data /var/www/h1scope
chmod 640 /var/www/h1scope/backend/.env
```

### 6. systemd service

```bash
cp deploy/h1scope.service /etc/systemd/system/h1scope.service
systemctl daemon-reload
systemctl enable h1scope
systemctl start h1scope
systemctl status h1scope
```

### 7. Apache virtual host

```bash
cp deploy/h1scope.conf /etc/apache2/sites-available/h1scope.conf
a2dissite 000-default.conf
a2ensite h1scope.conf
apache2ctl configtest
systemctl reload apache2
```

### 8. Verify

```bash
# Backend health check
curl http://127.0.0.1:8765/api/health

# Through Apache proxy
curl http://127.0.0.1/api/health

# Frontend
curl -o /dev/null -w "%{http_code}" http://127.0.0.1/
```

Open `http://YOUR_SERVER_IP/` in a browser and click **Sync**.

---

## API Reference

All endpoints are prefixed `/api` and proxied through Apache.

### `GET /api/health`

Returns service status and current sync state.

```json
{
  "status": "ok",
  "credentials_configured": true,
  "sync_status": {
    "running": false,
    "total": 116,
    "error": null,
    "progress": "Done — 116 programs with source code scope."
  }
}
```

---

### `GET /api/programs`

Returns cached programs. Supports query parameters:

| Parameter | Type | Description |
|---|---|---|
| `search` | string | Filter by name, handle, or repo URL |
| `program_type` | `public` \| `private` \| `all` | Filter by program visibility |
| `offers_bounties` | `true` \| `false` | Filter bounty vs VDP programs |

**Example:**
```
GET /api/programs?search=cloudflare&program_type=public&offers_bounties=true
```

**Response:**
```json
{
  "total": 1,
  "data": [
    {
      "handle": "cloudflare",
      "name": "Cloudflare",
      "logo_url": "https://...",
      "github_urls": [
        "https://github.com/cloudflare/workerd",
        "https://github.com/cloudflare/vinext"
      ],
      "offers_bounties": 1,
      "max_severity": "critical",
      "submission_state": "open",
      "program_type": "public",
      "scope_count": 3,
      "updated_at": "2026-03-15T23:18:32Z"
    }
  ]
}
```

---

### `POST /api/sync`

Triggers a fresh pull from the HackerOne API in the background. Returns `409` if a sync is already running.

```json
{ "message": "Sync started", "status": "running" }
```

---

### `GET /api/sync/status`

Returns the current or last sync state. Poll this while `running: true`.

```json
{
  "running": true,
  "total": 686,
  "error": null,
  "progress": "Scanned 350/686 programs, 89 match so far...",
  "last_sync_db": "2026-03-15T22:00:00Z",
  "cached_count": 116
}
```

---

## How the Sync Works

```
POST /api/sync
      │
      ▼
fetch all programs (paginated, 100/page)
      │
      ▼  (concurrent, semaphore=5)
for each program → GET /hackers/programs/{handle}/structured_scopes
      │
      ▼
filter: asset_type == "SOURCE_CODE"
     OR asset_identifier contains github.com / gitlab.com / bitbucket.org
      │
      ▼
store qualifying programs in SQLite
      │
      ▼
GET /api/programs serves from cache instantly
```

The semaphore limits concurrent scope fetches to 5 at a time to stay within HackerOne's rate limits. A full sync of ~700 programs takes approximately 3–4 minutes.

---

## Dashboard Features

| Feature | Description |
|---|---|
| **Search** | Live search across program name, handle, and repo URLs |
| **Type filter** | Public vs Private (invite-only) programs |
| **Bounty filter** | Paid bounty programs vs VDP (no monetary reward) |
| **Sort options** | By bounty status + repo count, name, max severity, or most repos |
| **Severity badge** | Shows maximum scope severity (Critical / High / Medium / Low) |
| **Open badge** | Indicates programs currently accepting submissions |
| **Sync progress** | Live progress text updates every 2.5 seconds during sync |
| **Direct links** | Click repo URLs to open on GitHub; "View on H1" links to the program |

---

## Scope Detection Logic

The HackerOne API exposes several `asset_type` values. This tool captures source code repositories through two rules:

**Rule 1 — Explicit type:**
```python
asset_type == "SOURCE_CODE"
```
Catches repos explicitly tagged as source code (e.g., `https://github.com/rails/rails`).

**Rule 2 — Git URL anywhere:**
```python
"github.com" in identifier OR "gitlab.com" in identifier OR "bitbucket.org" in identifier
```
Catches repos listed under `OTHER`, `URL`, or other types where the identifier is still a git hosting URL (e.g., HackerOne's own `react-datepicker` repo listed as `OTHER`).

Only scopes with `eligible_for_submission: true` are included.

---

## Updating Credentials

```bash
nano /var/www/h1scope/backend/.env
systemctl restart h1scope
```

---

## Logs

```bash
# Live backend logs
journalctl -u h1scope -f

# Apache access log
tail -f /var/log/apache2/h1scope_access.log

# Apache error log
tail -f /var/log/apache2/h1scope_error.log
```

---

## Troubleshooting

**Sync runs but finds 0 programs**
- Check credentials: `curl http://127.0.0.1:8765/api/health`
- Ensure your H1 account has program access (even public programs require a logged-in API identity)

**Backend not starting**
- Check logs: `journalctl -u h1scope -n 50`
- Verify the venv path matches the one in `h1scope.service`

**Apache 502 Bad Gateway**
- Confirm the backend is running on port 8765: `ss -tlnp | grep 8765`
- Check proxy modules are enabled: `apache2ctl -M | grep proxy`

**Rate limiting (429 responses)**
- The sync uses a semaphore of 5 concurrent requests with 0.2s delays
- If you see 429s in logs, the sync will retry automatically after a 10s backoff

---

## License

MIT — do whatever you want with it.
