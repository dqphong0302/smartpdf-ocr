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
- 📝 **LaTeX export** — built-in `pdflatex` / `xelatex` / `lualatex` compiler pipeline
- 🔌 **MCP server** — exposes OCR as Claude/AI agent tools via Model Context Protocol
- 🔐 **Auth** — salted SHA-256 session auth + API key support
- 🌐 **Cloudflare Tunnel** — designed for zero-trust public deployment

## Tech Stack

- **Backend**: Python 3.12 · FastAPI · PyMuPDF · [pdf-inspector](https://github.com/firecrawl/pdf-inspector) · Tesseract · mistune
- **Frontend**: React 18 · Vite · Vanilla CSS (glassmorphism dark mode)
- **MCP**: Node.js stdio server
- **Deployment**: Proxmox LXC · systemd · Cloudflare Tunnel

## Quick Start

### Prerequisites

```bash
# Ubuntu 24.04 LXC / server
apt install -y python3 python3-pip tesseract-ocr tesseract-ocr-vie \
               texlive-full latexmk nodejs npm
pip3 install pdf-inspector mistune --break-system-packages
```

### Backend

```bash
cd backend
cp .env.example .env
# Edit .env with your keys
pip3 install -r requirements.txt
python3 -m uvicorn main:app --host 0.0.0.0 --port 8000
```

### Frontend

```bash
cd frontend
npm install -g pnpm
pnpm install
pnpm dev
```

### MCP Server

```bash
cd mcp-server
pnpm install
SMART_OCR_URL=http://localhost:8000 \
SMART_OCR_API_KEY=sk-your-key \
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

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/health` | Service health check |
| `POST` | `/api/upload` | Upload PDF → returns `job_id` + analysis |
| `POST` | `/api/ocr/{job_id}` | Start OCR processing |
| `GET` | `/api/jobs/{job_id}` | Get job status + results |
| `GET` | `/api/jobs/{job_id}/download/md` | Download result as Markdown |
| `GET` | `/api/jobs/{job_id}/download/docx` | Download result as DOCX |
| `POST` | `/api/latex/compile` | Compile LaTeX project |

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
# Create service
cp /etc/systemd/system/smart-pdf.service.example /etc/systemd/system/smart-pdf.service
systemctl enable --now smart-pdf cloudflared
```

## License

MIT
