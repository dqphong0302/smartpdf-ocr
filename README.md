# SmartPDF OCR

**SmartPDF OCR** is a production-grade FastAPI + React service that intelligently routes PDF documents to the most appropriate extraction method:

| PDF Type | Method | Notes |
|---|---|---|
| Born-digital (text-based) | `pdf-inspector` (Rust) → Markdown | Layout-aware: tables, multi-column |
| Simple scans | Local Tesseract OCR | Fast, offline, no API cost |
| Complex scans | Vision AI (OpenAI-compatible) | Best accuracy for complex layouts |

## Features

- ⚡ **Smart routing** — pdf-inspector classifies every page in <10ms before choosing an engine
- 📊 **Layout-aware Markdown** — tables, columns, and headings preserved via pdf-inspector + mistune
- 🔍 **Tesseract OCR** — local, offline, supports `eng+vie` and many other languages
- 🤖 **Vision AI (batched)** — sends up to N pages per API call to reduce token costs
- 📝 **Optional LaTeX export** — disabled by default; bounded with paranoid file access and resource limits when enabled
- 🔌 **MCP server** — exposes OCR as Claude/AI agent tools via Model Context Protocol
- 🔐 **Auth** — Argon2id password hashing, secure sessions, login throttling, and constant-time API key checks
- 🧼 **Untrusted HTML sanitization** — backend allowlist plus DOMPurify in the browser
- 🌐 **Cloudflare Tunnel** — designed for zero-trust public deployment

## Tech Stack

- **Backend**: Python 3.12 · FastAPI · PyMuPDF · [pdf-inspector](https://github.com/firecrawl/pdf-inspector) · Tesseract · mistune
- **Frontend**: React 19 · Vite · Vanilla CSS (glassmorphism dark mode)
- **MCP**: Node.js stdio server
- **Deployment**: Proxmox LXC · systemd · Cloudflare Tunnel

## Quick Start

### Prerequisites

```bash
# Ubuntu 24.04 LXC / server
apt install -y python3 python3-venv tesseract-ocr tesseract-ocr-vie \
               texlive-full latexmk nodejs
```

### Backend

```bash
cd backend
cp .env.example .env
# Put real secrets only in .env; never commit this file.
python3 -m venv ../.venv
../.venv/bin/pip install -r requirements.txt
../.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

### Frontend

```bash
cd frontend
corepack enable pnpm
pnpm install --frozen-lockfile
pnpm dev
```

### MCP Server

```bash
cd mcp-server
pnpm install --frozen-lockfile
SMART_OCR_URL=http://localhost:8000 \
SMART_OCR_API_KEY=read-from-your-secret-store \
node index.js
```

## Configuration

Copy `backend/.env.example` to `backend/.env` and fill in the values:

| Variable | Description |
|---|---|
| `NINE_ROUTER_URL` | OpenAI-compatible Vision AI gateway URL |
| `NINE_ROUTER_API_KEY` | API key for the Vision AI gateway |
| `VISION_MODEL` | Model name (e.g. `gpt-4o-mini`, `gpt-4-vision`) |
| `OCR_API_KEY` | API key for programmatic access |
| `SMART_PDF_ADMIN_USER` | Admin username |
| `SMART_PDF_ADMIN_PASSWORD` | Admin password (change in production!) |
| `CORS_ORIGINS` | Comma-separated list of allowed origins |
| `MAX_PDF_PAGES` | Reject unexpectedly large documents |
| `MAX_CONCURRENT_JOBS` | Bound memory and CPU pressure |
| `LATEX_COMPILE_ENABLED` | Keep `false` unless the hardened unit is installed |
| `ENABLE_API_DOCS` | Keep `false` on public deployments |

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/health` | Service health check |
| `POST` | `/api/upload` | Upload PDF → returns `job_id` + analysis |
| `POST` | `/api/ocr/{job_id}` | Start OCR processing |
| `GET` | `/api/jobs/{job_id}` | Get job status + results (session required) |
| `GET` | `/api/download/{job_id}?format=md` | Download result (session required) |
| `POST` | `/api/v1/latex/compile` | Compile LaTeX project (API key; optional) |

## PDF Intelligence Pipeline

```
Upload PDF
    │
    ▼
pdf-inspector.classify_pdf()     ← Rust, <10ms per doc
    │
    ├─ text_based ──► extract_page_markdown()  ← mistune → HTML (tables preserved)
    │
    ├─ scanned (simple) ─► Tesseract OCR       ← local, offline
    │
    └─ scanned (complex) ─► Vision AI (batched) ← OpenAI-compatible API
```

## Production Deployment (Proxmox LXC)

```bash
# Run from the repository root after creating the `smartpdf` system user,
# the Python virtual environment, runtime directories, and private .env.
install -m 0644 deploy/systemd/smart-pdf.service /etc/systemd/system/
install -m 0644 deploy/systemd/smart-pdf-cleanup.* /etc/systemd/system/
install -m 0644 deploy/systemd/smart-pdf-backup.* /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now smart-pdf smart-pdf-cleanup.timer smart-pdf-backup.timer cloudflared
```

The supplied unit runs as a dedicated non-root user, removes Linux capabilities,
limits memory/tasks, protects the filesystem, and grants write access only to
`backend/data` and `backend/uploads`. Runtime secrets remain in
`backend/.env` with owner `root:smartpdf` and mode `0640`.

## Validation

```bash
uv run --with-requirements backend/requirements-dev.txt pytest -q backend/tests
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend build
pnpm --dir mcp-server install --frozen-lockfile
node --check mcp-server/index.js
```

## License

MIT
