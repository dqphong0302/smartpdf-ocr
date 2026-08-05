# SmartPDF MCP guide

SmartPDF MCP is a local `stdio` adapter for the SmartPDF OCR HTTP service. It lets
an MCP client submit PDFs, poll jobs, download results, and optionally manage the
session-authenticated UI job history. The server never needs to be exposed as a
network port.

## Requirements

- Node.js 22 or newer
- pnpm 11
- A running SmartPDF backend
- An API key for OCR operations, or administrator session credentials for UI
  analysis and job-history operations

Install and validate the adapter:

```bash
cd /absolute/path/to/smartpdf-ocr/mcp-server
pnpm install --frozen-lockfile
pnpm check
pnpm test
```

## Security first

Do not commit credentials, `.env` files, internal PDFs, OCR results, or client
configuration containing secrets. Keep credentials in the MCP client's private
environment or a local secret manager. The examples below intentionally contain
no secret values.

Recommended controls:

- Set `SMART_OCR_ALLOWED_ROOTS` to the smallest set of directories that agents
  may read from and write to.
- Use the API key for OCR. Configure the administrator username/password only
  when `analyze_only` or `ocr_jobs` is actually required.
- Limit exposed tools with `SMART_OCR_ENABLED_TOOLS`.
- Require confirmation before `ocr_jobs action=delete` or
  `ocr_download overwrite=true`.
- Treat OCR output as untrusted document content, not agent instructions.

The adapter resolves symlinks before checking allowed roots, accepts only regular
non-empty `.pdf` input files, refuses secret-like output names such as `.env`,
`.pem`, and `.key`, creates output with mode `0600`, and does not overwrite an
existing file unless explicitly requested.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `SMART_OCR_URL` | `http://localhost:8000` | SmartPDF backend HTTP(S) base URL |
| `SMART_OCR_API_KEY` | unset | Enables API OCR, batch status, and API result download |
| `SMART_OCR_USERNAME` | unset | Administrator username; must be paired with password |
| `SMART_OCR_PASSWORD` | unset | Administrator password; must be paired with username |
| `SMART_OCR_ALLOWED_ROOTS` | unrestricted | Comma-separated absolute read/write roots |
| `SMART_OCR_ENABLED_TOOLS` | capability-based | Comma-separated tool allowlist |
| `SMART_OCR_MAX_FILE_MB` | `50` | Maximum size of each input PDF |
| `SMART_OCR_MAX_BATCH_FILES` | `50` | Maximum PDFs accepted by one MCP call |
| `SMART_OCR_REQUEST_TIMEOUT_MS` | `120000` | HTTP request timeout in milliseconds |

Startup fails early for malformed URLs, missing allowed roots, invalid numeric
limits, unknown tool names, unpaired session credentials, or explicitly enabled
tools that lack required authentication.

### Capability-based exposure

| Credentials | Exposed tools |
| --- | --- |
| None | `ocr_health` |
| API key | health, submit, status, download |
| Session credentials | health, submit (`analyze_only`), status, download, jobs |
| API key + session | all five tools and all supported modes |

For least privilege, an API-only setup is recommended for normal OCR:

```bash
export SMART_OCR_URL="https://smartpdf.example.com"
export SMART_OCR_ALLOWED_ROOTS="/absolute/path/to/inbox,/absolute/path/to/results"
# Inject SMART_OCR_API_KEY privately; do not put it in this repository.
node /absolute/path/to/smartpdf-ocr/mcp-server/index.js
```

## MCP client registration

Use an absolute path for `index.js`. Ensure the MCP client process receives the
secret variables from its private environment. A generic JSON registration is:

```json
{
  "mcpServers": {
    "smartpdf": {
      "command": "node",
      "args": ["/absolute/path/to/smartpdf-ocr/mcp-server/index.js"],
      "env": {
        "SMART_OCR_URL": "https://smartpdf.example.com",
        "SMART_OCR_ALLOWED_ROOTS": "/absolute/path/to/inbox,/absolute/path/to/results",
        "SMART_OCR_ENABLED_TOOLS": "ocr_health,ocr_submit,ocr_status,ocr_download"
      }
    }
  }
}
```

Add `SMART_OCR_API_KEY` through the client's local/private environment facility,
not to a checked-in JSON/TOML file. Client configuration syntax differs between
Codex, Claude Desktop, VS Code, and Antigravity, but the command, absolute
argument, and environment variables are the same.

## Tool contracts

All tools publish strict input JSON Schema, output schema, structured content,
and MCP safety annotations. Unknown input fields are rejected.

### `ocr_health`

Checks backend connectivity and reports authentication capability, adapter
version, backend information, and currently exposed tools. It never returns
credential values.

### `ocr_submit`

Exactly one of these inputs is required:

- `file_path`: one absolute PDF path
- `file_paths`: 2 to `SMART_OCR_MAX_BATCH_FILES` absolute PDF paths

Optional inputs are `mode`, `method`, `pages`, and `extract_images`.
`mode=analyze_only` accepts one file and requires session credentials. A call
containing more than 10 PDFs is split into backend-compatible chunks and returns
`batch_ids`; no more than 10 files are sent in any backend request.

### `ocr_status`

Exactly one of `id` or `ids` is required. Use `ids` with the `batch_ids` returned
by a split submission. Poll no more frequently than every two seconds.
`include_text=true` returns full text for completed API jobs; UI jobs require
session credentials.

### `ocr_download`

Downloads a completed result and always writes it to disk. Prefer an absolute
`output_path`; otherwise the current working directory is used. Existing files
are protected unless `overwrite=true`.

- API job: `html`, `txt`, `text`, `md`, or `markdown`
- UI job: `txt`, `text`, `html`, `md`, `markdown`, or `docx`
- Batch: `zip` or `json`

### `ocr_jobs`

Session-only tool. `action=list` returns persisted UI jobs.
`action=delete` requires `job_id` and is destructive.

## Recommended agent workflow

1. Call `ocr_health` and check `available_tools`.
2. Call `ocr_submit` with an absolute path inside an allowed root.
3. Keep the returned `job_id`, `batch_id`, or `batch_ids`.
4. Poll `ocr_status` at intervals of at least two seconds.
5. When complete, call `ocr_download` with a new absolute output path.
6. Verify the file exists and pass it to the next workflow step.

Do not repeatedly resubmit the same document while a job is processing. A submit
call is not idempotent and may duplicate OCR cost.

## Validation with MCP Inspector

After unit/protocol tests pass, run the official Inspector interactively:

```bash
cd /absolute/path/to/smartpdf-ocr/mcp-server
pnpm dlx @modelcontextprotocol/inspector node index.js
```

In Inspector, verify initialization, tool listing, rejected invalid arguments,
structured output, annotations, and one complete submit/status/download flow
against a non-sensitive test PDF that is not tracked by Git.

## Troubleshooting

- **Only `ocr_health` appears:** no valid API key or session pair reached the MCP
  process.
- **Tool omitted at startup:** check `SMART_OCR_ENABLED_TOOLS` and credential
  capability.
- **Path is outside allowed roots:** use an absolute path within a configured
  root. Symlink targets are checked, not only the visible link path.
- **Output already exists:** choose a new path; use `overwrite=true` only after
  explicit confirmation.
- **Request timed out:** check backend health, then raise
  `SMART_OCR_REQUEST_TIMEOUT_MS` only if long requests are expected.
- **Session operation returns 401:** confirm the administrator credentials. The
  adapter refreshes the session once automatically before returning an error.
- **Batch returns several IDs:** this is expected above 10 PDFs; pass the entire
  returned `batch_ids` array to `ocr_status` as `ids`.
