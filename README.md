# SmartPDF OCR

[![CI](https://github.com/dqphong0302/smartpdf-ocr/actions/workflows/ci.yml/badge.svg)](https://github.com/dqphong0302/smartpdf-ocr/actions/workflows/ci.yml)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![React 19](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=black)](https://react.dev/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

SmartPDF OCR is a self-hosted PDF extraction service built with FastAPI, React,
Tesseract and an optional OpenAI-compatible Vision endpoint. It classifies each
page before choosing the least expensive extraction method that can preserve its
content and layout.

> Tiếng Việt: dịch vụ OCR PDF tự host, ưu tiên xử lý local và chỉ dùng Vision AI
> cho những trang scan phức tạp cần độ chính xác cao hơn.

## Why SmartPDF OCR?

- **Page-level routing:** digital, simple scan and complex scan pages can coexist
  in one PDF; each page is processed with the appropriate engine.
- **Local-first:** born-digital pages and ordinary scans do not consume Vision
  API credits.
- **Layout-aware output:** headings, lists, tables and extracted images are
  preserved as Markdown/HTML where possible.
- **Web and agent access:** use the React interface, REST API, batch API or the
  included MCP server.
- **Production controls:** bounded uploads and concurrency, authentication,
  sanitized model output, retention jobs, SQLite-safe backups and hardened
  systemd units.

## Processing pipeline

```text
Upload PDF
    └─ Inspect each page
       ├─ Born-digital ──> pdf-inspector / PyMuPDF ─┐
       ├─ Simple scan ───> Local Tesseract OCR ─────┼─> Sanitize ─> Preview / download
       └─ Complex scan ──> Vision AI batch ─────────┘
```

| Page type | Default method | Network/API cost |
| --- | --- | --- |
| Born-digital text | `pdf-inspector` + PyMuPDF | None |
| Simple scanned page | Tesseract | None |
| Complex or low-confidence scan | Vision model | Optional API usage |

## Features

- React web UI with live job progress over WebSocket
- Per-page automatic routing or forced `tesseract` / `vision` mode
- Page selection: all, odd, even or an explicit list
- Single and batch OCR APIs
- Markdown, HTML, text and ZIP download flows
- English and Vietnamese Tesseract support by default (`eng+vie`)
- Optional extraction of embedded PDF images
- Optional LaTeX compilation, disabled by default and resource-restricted when enabled
- MCP tools: `ocr_health`, `ocr_submit`, `ocr_status`, `ocr_download`, `ocr_jobs`
- SQLite job history with WAL mode and busy timeout
- Automated cleanup and private backup timers

## Technology

| Layer | Components |
| --- | --- |
| Backend | Python 3.12, FastAPI, PyMuPDF, pdf-inspector, pytesseract, mistune |
| Frontend | React 19, Vite 8, DOMPurify, pnpm |
| Agent integration | Node.js MCP stdio server |
| Persistence | SQLite WAL; uploads on local disk |
| Production | Proxmox LXC or Ubuntu, systemd, Cloudflare Tunnel |

## Repository layout

```text
smartpdf-ocr/
├── backend/                FastAPI app, OCR engines and security tests
├── frontend/               React UI and production build
├── mcp-server/             MCP stdio adapter
├── deploy/
│   ├── journald/           Log retention policy
│   └── systemd/            App, cleanup and backup units/timers
├── scripts/                Backup, cleanup and password rotation helpers
├── pyproject.toml          Ruff configuration and Python requirement
└── .github/workflows/      CI for Python and Node.js
```

## Quick start for development

### Requirements

- Python 3.12+
- Node.js 22+ and pnpm 11+
- Tesseract OCR with the language packs you need
- `uv` for the documented Python workflow

On Ubuntu 24.04:

```bash
sudo apt update
sudo apt install -y python3 python3-venv tesseract-ocr tesseract-ocr-eng tesseract-ocr-vie
corepack enable pnpm
```

Install and start the backend:

```bash
git clone https://github.com/dqphong0302/smartpdf-ocr.git
cd smartpdf-ocr
cp backend/.env.example backend/.env
# Edit backend/.env locally. Never commit this file.

python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
cd backend
../.venv/bin/python -m uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

In a second terminal, start the frontend:

```bash
cd frontend
pnpm install --frozen-lockfile
pnpm dev
```

Vite serves the development UI. In production, FastAPI serves the compiled
`frontend/dist` bundle from the same origin.

## Configuration

Copy [`backend/.env.example`](backend/.env.example) to `backend/.env`. The example
contains placeholders only; real values belong in the private runtime file or a
secret manager.

### OCR and Vision

| Variable | Purpose |
| --- | --- |
| `TESSERACT_LANG` | Tesseract languages, such as `eng+vie` |
| `CONFIDENCE_THRESHOLD` | Confidence below which auto mode may use Vision |
| `OPENAI_BASE_URL` | Base URL of an OpenAI-compatible `/v1` endpoint |
| `OPENAI_API_KEY` | Private Vision credential; leave unset for local-only use |
| `VISION_MODEL` | Vision-capable model exposed by the configured gateway |
| `BATCH_SIZE` | Pages sent in one Vision request |
| `PARALLEL_BATCHES` | Maximum concurrent Vision batches |
| `MAX_TESSERACT_WORKERS` | Maximum concurrent local OCR workers |

### Limits and retention

| Variable | Purpose |
| --- | --- |
| `MAX_UPLOAD_SIZE_MB` | Reject oversized uploads |
| `MAX_PDF_PAGES` | Reject unexpectedly long documents |
| `MAX_CONCURRENT_JOBS` | Bound total OCR CPU and memory pressure |
| `JOB_MAX_AGE_DAYS` | Application job retention |
| `SMART_PDF_CLEANUP_DAYS` | File cleanup timer retention |
| `SMART_PDF_BACKUP_DAYS` | Backup retention |

### Authentication and browser security

| Variable | Purpose |
| --- | --- |
| `SMART_PDF_ADMIN_USER` | Explicit web administrator username |
| `SMART_PDF_ADMIN_PASSWORD` | Admin password; 12+ characters for seeding |
| `OCR_API_KEY` | Credential for programmatic and detailed-health endpoints |
| `COOKIE_SECURE` | Keep `true` for HTTPS production deployments |
| `COOKIE_SAMESITE` | Session cookie SameSite policy; defaults to `lax` |
| `CORS_ORIGINS` | Comma-separated trusted origins; avoid `*` in production |
| `ENABLE_API_DOCS` | Keep `false` on a public deployment |
| `LATEX_COMPILE_ENABLED` | Keep `false` unless the hardened unit is installed |

The application has no built-in administrator password or API key. Startup can
run without them, but protected login/API flows remain unavailable until explicit
runtime credentials are configured.

## API overview

Two authentication mechanisms are intentionally separate:

- **Session cookie:** browser UI and job content endpoints.
- **`X-API-Key`:** programmatic OCR, batch, LaTeX and detailed health endpoints.

| Method | Path | Authentication | Purpose |
| --- | --- | --- | --- |
| `GET` | `/api/health` | Public | Minimal liveness/readiness response |
| `GET` | `/api/health/details` | API key | Dependency details |
| `POST` | `/api/auth/login` | Public | Create a secure web session |
| `POST` | `/api/upload` | Session | Analyze and create a UI job |
| `POST` | `/api/ocr/{job_id}` | Session | Start processing a UI job |
| `GET` | `/api/jobs/{job_id}` | Session | Job state and page results |
| `GET` | `/api/download/{job_id}` | Session | Download UI job output |
| `WS` | `/ws/{job_id}` | Session | Live job updates |
| `POST` | `/api/v1/ocr` | API key | Submit one PDF asynchronously |
| `GET` | `/api/v1/ocr/{job_id}` | API key | Poll one API job/result |
| `POST` | `/api/v1/ocr/batch` | API key | Submit up to 10 PDFs |
| `GET` | `/api/v1/ocr/batch/{batch_id}` | API key | Poll a batch |
| `GET` | `/api/v1/ocr/batch/{batch_id}/download` | Key | Download ZIP |
| `POST` | `/api/v1/latex/compile` | Key | Optional LaTeX compile |

Example single-file submission, with the credential read from the caller's
environment rather than embedded in a command or source file:

```bash
curl --fail-with-body \
  -H "X-API-Key: ${SMARTPDF_API_KEY}" \
  -F "file=@document.pdf" \
  "${SMARTPDF_URL}/api/v1/ocr?pages=all&method=auto&extract_images=false"
```

The response includes a `job_id` and `poll_url`. API jobs are kept in memory for
one hour after completion; persist downloaded results in the calling workflow.

## MCP server

The MCP adapter uses stdio, strict schemas, structured output, bounded file
access, request timeouts, and capability-based tool exposure. See the complete
[MCP setup, security, tool-contract, and troubleshooting guide](mcp-server/MCP_GUIDE.md).

```bash
cd mcp-server
pnpm install --frozen-lockfile
pnpm check
pnpm test
SMART_OCR_URL="${SMARTPDF_URL}" \
SMART_OCR_API_KEY="${SMARTPDF_API_KEY}" \
node index.js
```

Set `SMART_OCR_USERNAME` and `SMART_OCR_PASSWORD` through the MCP client's private
environment only if `ocr_jobs` needs session-authenticated UI history. API-only
OCR tools need `SMART_OCR_API_KEY`, not the administrator password.

For least privilege, configure `SMART_OCR_ALLOWED_ROOTS` and optionally
`SMART_OCR_ENABLED_TOOLS`. Never commit MCP client configuration containing
credentials, internal test PDFs, or downloaded OCR results.

## Production deployment with systemd

The supplied unit expects the checkout at `/opt/smart-pdf`, a dedicated
`smartpdf` user, a virtual environment at `/opt/smart-pdf/.venv`, and writable
runtime directories owned by that user.

```bash
sudo useradd --system --home-dir /opt/smart-pdf --shell /usr/sbin/nologin smartpdf
sudo install -d -o smartpdf -g smartpdf -m 0750 \
  /opt/smart-pdf/backend/data /opt/smart-pdf/backend/uploads

sudo python3 -m venv /opt/smart-pdf/.venv
sudo /opt/smart-pdf/.venv/bin/pip install -r /opt/smart-pdf/backend/requirements.txt

cd /opt/smart-pdf/frontend
sudo corepack enable pnpm
sudo pnpm install --frozen-lockfile
sudo pnpm build

cd /opt/smart-pdf
sudo chown root:smartpdf /opt/smart-pdf/backend/.env
sudo chmod 0640 /opt/smart-pdf/backend/.env
sudo install -m 0644 deploy/systemd/smart-pdf.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/smart-pdf-cleanup.* /etc/systemd/system/
sudo install -m 0644 deploy/systemd/smart-pdf-backup.* /etc/systemd/system/
sudo install -m 0644 deploy/journald/99-smart-pdf.conf /etc/systemd/journald.conf.d/

sudo systemctl daemon-reload
sudo systemctl restart systemd-journald
sudo systemctl enable --now smart-pdf smart-pdf-cleanup.timer smart-pdf-backup.timer
```

Before starting, create `backend/.env` from the example outside Git. The unit runs
without root privileges, drops Linux capabilities, protects the filesystem and
kernel, caps memory/tasks and permits writes only in the runtime directories.

Cloudflare Tunnel or another HTTPS reverse proxy can forward to port `8000`.
Keep `COOKIE_SECURE=true`, restrict `CORS_ORIGINS`, and do not expose the backend
directly to an untrusted network.

## Operations and recovery

| Task | Command |
| --- | --- |
| Service state | `systemctl status smart-pdf` |
| Recent logs | `journalctl -u smart-pdf -n 100 --no-pager` |
| Health | `curl --fail http://127.0.0.1:8000/api/health` |
| Run backup now | `systemctl start smart-pdf-backup.service` |
| Inspect timers | `systemctl list-timers 'smart-pdf-*'` |
| Rotate admin password | `sudo scripts/rotate-admin-password.sh` |
| Security review | `systemd-analyze security smart-pdf.service` |

Backups are written under `/var/backups/smart-pdf/<UTC timestamp>/` with mode
`0600`. Each backup contains a consistent SQLite snapshot, uploads archive,
private environment copy and service unit. The default retention is 14 days.

The password rotation helper updates both the private environment file and the
database hash, then writes the new credential to
`/root/smartpdf-admin-credential.txt` with mode `0600`. Read it locally, move it
to the intended password manager, and never copy it into Git or logs.

For recovery, stop the service, preserve the current runtime directory, restore
the SQLite snapshot/uploads/environment from one timestamped backup, fix runtime
ownership, and start the service. Verify `/api/health` and a small local OCR job
before reopening external traffic.

## Security model

- Argon2id password hashes with migration from the legacy hash format
- Constant-time credential comparison and throttled login failures
- HttpOnly, Secure, SameSite session cookies
- Authentication on job data, images, downloads and WebSocket
- Streaming upload size checks, filename normalization, page limits and path containment
- Backend Bleach allowlist plus frontend DOMPurify for OCR/model-produced HTML
- LaTeX shell escape disabled, paranoid file access and process resource limits
- API documentation disabled by default; public health exposes minimal metadata
- HSTS, CSP, clickjacking, MIME-sniffing, referrer and permissions-policy headers

No uploaded PDF, generated output, database, `.env`, password, token or API key
is intended for source control. The repository ignores runtime data and
local-only test helpers; run a secret scan before publishing changes.

## Validation

Run the same core gates used by CI:

```bash
uv run --with-requirements backend/requirements-dev.txt ruff check backend
uv run --with-requirements backend/requirements-dev.txt pytest -q backend/tests
uv run --with-requirements backend/requirements.txt python -m py_compile backend/*.py

pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend build

pnpm --dir mcp-server install --frozen-lockfile
pnpm --dir mcp-server check
pnpm --dir mcp-server test
```

Optional dependency audits:

```bash
uvx pip-audit -r backend/requirements.txt
pnpm --dir frontend audit --audit-level high
pnpm --dir mcp-server audit --audit-level high
```

## Known operational boundaries

- Active API/batch job state is in memory and is lost on a service restart.
- SQLite persists UI job history, but backup restoration is an operator action.
- Vision quality, latency and cost depend on the configured external model.
- Tesseract language packs must be installed at the OS level.
- LaTeX is intentionally unavailable unless explicitly enabled in a hardened host.
- Production sizing depends on PDF resolution, page count and concurrent OCR jobs.

## License

Released under the [MIT License](LICENSE).
