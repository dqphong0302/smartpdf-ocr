#!/usr/bin/env node
/**
 * Smart PDF — MCP Server v2.0.0 (consolidated)
 *
 * Exposes the Smart PDF API as MCP tools for AI coding assistants.
 * v2.0.0 collapses 14 tools → 6 via param-driven dispatch.
 *
 * Tools: ocr_health, ocr_submit, ocr_status, ocr_download, ocr_jobs
 *
 * Environment variables:
 *   SMART_OCR_URL      - Base URL (default: http://localhost:8000)
 *   SMART_OCR_API_KEY  - API key for /api/v1/ocr endpoints
 *   SMART_OCR_USERNAME - Username for web UI auth (optional, for full API)
 *   SMART_OCR_PASSWORD - Password for web UI auth (optional, for full API)
 */

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import fs from "fs";
import path from "path";

// ── Config ──────────────────────────────────────────────────────────
const BASE_URL = process.env.SMART_OCR_URL || "http://localhost:8000";
const API_KEY = process.env.SMART_OCR_API_KEY || "";
const USERNAME = process.env.SMART_OCR_USERNAME || "";
const PASSWORD = process.env.SMART_OCR_PASSWORD || "";

let sessionCookie = "";

// ── HTTP Helpers ────────────────────────────────────────────────────

async function apiRequest(endpoint, options = {}) {
  const url = `${BASE_URL}${endpoint}`;
  const headers = { ...options.headers };

  if (options.useApiKey && API_KEY) headers["X-API-Key"] = API_KEY;
  if (options.useSession && sessionCookie) headers["Cookie"] = `session=${sessionCookie}`;

  const res = await fetch(url, {
    method: options.method || "GET",
    headers,
    body: options.body,
  });

  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const err = await res.json();
      detail = err.detail || err.error || detail;
    } catch {}
    const error = new Error(`API error: ${detail}`);
    error.status = res.status;
    throw error;
  }

  const contentType = res.headers.get("content-type") || "";
  if (contentType.includes("application/json")) return await res.json();
  return await res.text();
}

async function ensureSession() {
  if (sessionCookie) return;
  if (!USERNAME || !PASSWORD) {
    throw new Error(
      "Session auth required but SMART_OCR_USERNAME/PASSWORD not set. " +
      "Set them in env, or use a mode that only needs API key."
    );
  }
  const res = await fetch(`${BASE_URL}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: USERNAME, password: PASSWORD }),
  });
  if (!res.ok) throw new Error("Login failed — check credentials");
  const setCookie = res.headers.get("set-cookie") || "";
  const match = setCookie.match(/session=([^;]+)/);
  if (match) sessionCookie = match[1];
}

/** Build multipart body for one file. */
function buildSingleFileMultipart(filePath, fieldName = "file", fields = {}, contentType = "application/pdf") {
  const boundary = `----MCPBoundary${Date.now()}${Math.random().toString(36).slice(2)}`;
  const fileName = path.basename(filePath);
  const fileContent = fs.readFileSync(filePath);
  const chunks = [];

  for (const [name, value] of Object.entries(fields)) {
    if (value === undefined || value === null || value === "") continue;
    chunks.push(Buffer.from(
      `--${boundary}\r\n` +
      `Content-Disposition: form-data; name="${name}"\r\n\r\n` +
      `${value}\r\n`
    ));
  }
  chunks.push(Buffer.from(
    `--${boundary}\r\n` +
    `Content-Disposition: form-data; name="${fieldName}"; filename="${fileName}"\r\n` +
    `Content-Type: ${contentType}\r\n\r\n`
  ));
  chunks.push(fileContent);
  chunks.push(Buffer.from(`\r\n--${boundary}--\r\n`));
  return { body: Buffer.concat(chunks), contentType: `multipart/form-data; boundary=${boundary}` };
}

/** Build multipart body for many files under same field name. */
function buildMultiFileMultipart(filePaths, fieldName = "files") {
  const boundary = `----MCPBatchBoundary${Date.now()}${Math.random().toString(36).slice(2)}`;
  const chunks = [];
  for (const fp of filePaths) {
    const fileName = path.basename(fp);
    const fileContent = fs.readFileSync(fp);
    chunks.push(Buffer.from(
      `--${boundary}\r\n` +
      `Content-Disposition: form-data; name="${fieldName}"; filename="${fileName}"\r\n` +
      `Content-Type: application/pdf\r\n\r\n`
    ));
    chunks.push(fileContent);
    chunks.push(Buffer.from("\r\n"));
  }
  chunks.push(Buffer.from(`--${boundary}--\r\n`));
  return { body: Buffer.concat(chunks), contentType: `multipart/form-data; boundary=${boundary}` };
}

/** Recommend OCR method given a page-level analysis. */
function decideOcrMethod(analysis, requestedMethod) {
  if (requestedMethod && requestedMethod !== "auto") {
    return { method: requestedMethod, reasoning: `User forced method: ${requestedMethod}`, summary: analysis.summary };
  }
  const summary = analysis.summary || {};
  const digital = summary.digital || 0;
  const simple = summary.scan_simple || 0;
  const complex = summary.scan_complex || 0;
  const total = analysis.total_pages || 0;

  if (digital === total) return { method: "auto", reasoning: `All ${total} pages are digital. Direct text extraction.`, summary };
  if (complex === 0) return { method: "tesseract", reasoning: `No complex pages (${digital} digital, ${simple} simple). Forcing Tesseract.`, summary };
  return { method: "auto", reasoning: `Mixed: ${digital} digital, ${simple} simple, ${complex} complex. Auto routing per page.`, summary };
}

/** Try batch endpoint first, then single-job endpoint. Returns {kind, data}. */
async function detectIdKind(id) {
  if (!API_KEY) {
    // Without API key we can only try session-based job lookup
    await ensureSession();
    const data = await apiRequest(`/api/jobs/${id}?include_text=false`, { useSession: true });
    return { kind: "job_full", data };
  }
  // Try batch first (cheap call)
  try {
    const data = await apiRequest(`/api/v1/ocr/batch/${id}`, { useApiKey: true });
    return { kind: "batch", data };
  } catch (err) {
    if (err.status !== 404) throw err;
  }
  // Then simple job
  try {
    const data = await apiRequest(`/api/v1/ocr/${id}`, { useApiKey: true });
    return { kind: "job_simple", data };
  } catch (err) {
    if (err.status !== 404) throw err;
  }
  // Fallback to session-based full job
  if (USERNAME && PASSWORD) {
    await ensureSession();
    const data = await apiRequest(`/api/jobs/${id}?include_text=false`, { useSession: true });
    return { kind: "job_full", data };
  }
  throw new Error(`No job or batch found with id="${id}"`);
}

// ── Tools Definition (consolidated to 6) ────────────────────────────

const TOOLS = [
  {
    name: "ocr_health",
    description: "Check Smart PDF service health and connectivity. Returns auth/dependency status.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "ocr_submit",
    description:
      "Universal OCR submission. Accepts a single PDF (file_path) OR many PDFs (file_paths) and " +
      "auto-routes between simple-job, batch-queue, and analyze-only flows.\n\n" +
      "Modes:\n" +
      "- file_paths (>=2 items) → submits as a BATCH; returns batch_id. Files queued sequentially with " +
      "global concurrency control. Recommended for >10 PDFs.\n" +
      "- file_path + mode='ocr' (default) → smart-routes complexity, returns job_id. Poll via ocr_status.\n" +
      "- file_path + mode='analyze_only' → returns page-by-page classification (digital/scan_simple/" +
      "scan_complex) WITHOUT running OCR. Useful for previewing routing.\n\n" +
      "Pages selector applies only to single-file 'ocr' mode.",
    inputSchema: {
      type: "object",
      properties: {
        file_path: { type: "string", description: "Absolute path to a single PDF (mutually exclusive with file_paths)" },
        file_paths: {
          type: "array",
          items: { type: "string" },
          description: "Absolute paths to multiple PDFs — triggers batch mode (mutually exclusive with file_path)",
        },
        mode: {
          type: "string",
          enum: ["ocr", "analyze_only"],
          description: "Single-file mode (ignored for batch). Default: 'ocr'",
          default: "ocr",
        },
        method: {
          type: "string",
          enum: ["auto", "tesseract", "vision"],
          description: "auto (smart routing), tesseract (force local OCR), or vision (force AI). Default: auto",
          default: "auto",
        },
        pages: {
          type: "string",
          description: "Pages to OCR for single-file mode: 'all' (default), 'odd', 'even', or '1,3,5'",
          default: "all",
        },
        extract_images: {
          type: "boolean",
          description: "Extract original images (e.g. diagrams, illustrations) from the PDF. Default: false",
          default: false,
        },
      },
    },
  },
  {
    name: "ocr_status",
    description:
      "Check status of any OCR job or batch by id. Auto-detects whether the id refers to a batch " +
      "or a single job. For single jobs, returns progress while processing or a text/HTML preview " +
      "when complete. For batches, returns total_files / completed_files counters.\n\n" +
      "Set include_text=true to fetch detailed page-level results via session API (single jobs only).",
    inputSchema: {
      type: "object",
      properties: {
        id: { type: "string", description: "Job ID or Batch ID returned from ocr_submit" },
        include_text: {
          type: "boolean",
          description: "Include extracted text per page (single job only, requires session auth). Default: false",
          default: false,
        },
      },
      required: ["id"],
    },
  },
  {
    name: "ocr_download",
    description:
      "Download OCR results by id. Auto-detects job vs batch.\n\n" +
      "Single job formats: txt, text, md, markdown, html, docx (requires session auth).\n" +
      "Batch formats: zip (default; bundles all .html files) or json (structured summary).\n\n" +
      "Provide output_path to save the file; otherwise returns a content preview (or bytes count for binary).",
    inputSchema: {
      type: "object",
      properties: {
        id: { type: "string", description: "Job ID or Batch ID" },
        format: {
          type: "string",
          enum: ["txt", "text", "html", "md", "markdown", "docx", "zip", "json"],
          description: "Output format. Default: txt for jobs, zip for batches",
        },
        output_path: { type: "string", description: "Optional absolute path to save the result" },
      },
      required: ["id"],
    },
  },
  {
    name: "ocr_jobs",
    description:
      "Manage stored OCR jobs (web UI). Requires session auth (SMART_OCR_USERNAME/PASSWORD).\n\n" +
      "Actions:\n" +
      "- 'list' (default): list all jobs with status\n" +
      "- 'delete': delete a specific job and its files (requires job_id)",
    inputSchema: {
      type: "object",
      properties: {
        action: {
          type: "string",
          enum: ["list", "delete"],
          description: "Action to perform. Default: list",
          default: "list",
        },
        job_id: { type: "string", description: "Job ID to delete (required when action='delete')" },
      },
    },
  },
];

// ── Tool Handlers ───────────────────────────────────────────────────

async function handleTool(name, args) {
  switch (name) {
    // ── Health ──
    case "ocr_health": {
      const result = await apiRequest("/api/health");
      return {
        ...result,
        base_url: BASE_URL,
        api_key_configured: !!API_KEY,
        session_auth_configured: !!(USERNAME && PASSWORD),
        mcp_version: "2.0.0",
      };
    }

    // ── Universal Submit ──
    case "ocr_submit": {
      const filePath = args.file_path;
      const filePaths = args.file_paths;
      const mode = args.mode || "ocr";
      const method = args.method || "auto";
      const pages = args.pages || "all";
      const extract_images = !!args.extract_images;

      if (filePath && filePaths) {
        throw new Error("Provide either file_path OR file_paths, not both.");
      }

      // ── BATCH PATH ──
      if (Array.isArray(filePaths) && filePaths.length > 0) {
        if (!API_KEY) throw new Error("SMART_OCR_API_KEY not configured (required for batch).");
        const missing = filePaths.filter((fp) => !fs.existsSync(fp));
        if (missing.length) throw new Error(`Files not found: ${missing.join(", ")}`);
        const nonPdf = filePaths.filter((fp) => !fp.toLowerCase().endsWith(".pdf"));
        if (nonPdf.length) throw new Error(`Only PDF files allowed: ${nonPdf.join(", ")}`);

        const { body, contentType } = buildMultiFileMultipart(filePaths);
        const result = await apiRequest(
          `/api/v1/ocr/batch?method=${encodeURIComponent(method)}`,
          { method: "POST", headers: { "Content-Type": contentType, "X-API-Key": API_KEY }, body }
        );
        return {
          kind: "batch",
          ...result,
          message: `Submitted ${filePaths.length} PDFs as batch. Poll with ocr_status(id="${result.batch_id}").`,
        };
      }

      // ── SINGLE PATH ──
      if (!filePath) throw new Error("Provide file_path (single) or file_paths (batch).");
      if (!fs.existsSync(filePath)) throw new Error(`File not found: ${filePath}`);
      if (!filePath.toLowerCase().endsWith(".pdf")) throw new Error("Only PDF files are supported.");

      // Analyze-only mode
      if (mode === "analyze_only") {
        await ensureSession();
        const { body, contentType } = buildSingleFileMultipart(filePath);
        const upload = await apiRequest("/api/upload", {
          method: "POST",
          headers: { "Content-Type": contentType },
          body,
          useSession: true,
        });
        const analysis = upload.analysis || {};
        const decision = decideOcrMethod(analysis, "auto");
        // Auto-cleanup: we only wanted analysis
        try {
          await apiRequest(`/api/jobs/${upload.job_id}`, { method: "DELETE", useSession: true });
        } catch {}
        return {
          kind: "analysis",
          filename: path.basename(filePath),
          total_pages: analysis.total_pages,
          classification_summary: decision.summary,
          recommended_method: decision.method,
          reasoning: decision.reasoning,
          pages: (analysis.pages || []).map((p) => ({
            page: p.page_num,
            classification: p.classification,
            text_length: p.text_length,
            image_count: p.image_count,
            image_coverage: p.image_coverage,
          })),
        };
      }

      // OCR mode (single)
      if (!API_KEY) throw new Error("SMART_OCR_API_KEY not configured.");

      // Optional pre-analysis to choose smarter method when session creds available
      let finalMethod = method;
      let routingDecision = null;
      if (method === "auto" && USERNAME && PASSWORD) {
        try {
          await ensureSession();
          const { body: ab, contentType: ac } = buildSingleFileMultipart(filePath);
          const a = await apiRequest("/api/upload", { method: "POST", headers: { "Content-Type": ac }, body: ab, useSession: true });
          const decision = decideOcrMethod(a.analysis || {}, "auto");
          finalMethod = decision.method;
          routingDecision = { method_chosen: decision.method, reasoning: decision.reasoning, page_breakdown: decision.summary };
          try { await apiRequest(`/api/jobs/${a.job_id}`, { method: "DELETE", useSession: true }); } catch {}
        } catch (err) {
          routingDecision = { method_chosen: "auto", reasoning: `Pre-analysis unavailable (${err.message}). Backend auto-routes.` };
        }
      }

      const { body, contentType } = buildSingleFileMultipart(filePath);
      const result = await apiRequest(
        `/api/v1/ocr?pages=${encodeURIComponent(pages)}&method=${finalMethod}&extract_images=${extract_images}`,
        { method: "POST", headers: { "Content-Type": contentType, "X-API-Key": API_KEY }, body }
      );
      return {
        kind: "job",
        ...result,
        routing_decision: routingDecision || {
          method_chosen: finalMethod,
          reasoning: finalMethod === "auto"
            ? "No session auth available for pre-analysis. Backend auto-routes per page."
            : `User forced method: ${finalMethod}`,
        },
        message: `PDF submitted with method=${finalMethod}. Poll with ocr_status(id="${result.job_id}").`,
      };
    }

    // ── Universal Status ──
    case "ocr_status": {
      if (!args.id) throw new Error("id is required.");
      const detected = await detectIdKind(args.id);

      if (detected.kind === "batch") {
        return { kind: "batch", ...detected.data };
      }

      // Job — optionally fetch detailed text
      if (args.include_text && USERNAME && PASSWORD) {
        await ensureSession();
        const full = await apiRequest(`/api/jobs/${args.id}?include_text=true`, { useSession: true });
        return { kind: "job", ...full };
      }

      // job_simple may return HTML string when complete
      if (detected.kind === "job_simple") {
        const result = detected.data;
        if (typeof result === "string") {
          const textContent = result
            .replace(/<style[^>]*>[\s\S]*?<\/style>/gi, "")
            .replace(/<[^>]+>/g, "\n")
            .replace(/\n{3,}/g, "\n\n")
            .trim();
          return {
            kind: "job",
            status: "completed",
            job_id: args.id,
            text_preview: textContent.slice(0, 3000),
            full_length: textContent.length,
            note: textContent.length > 3000 ? "Text truncated. Use ocr_download for full content." : undefined,
          };
        }
        return { kind: "job", ...result };
      }

      return { kind: "job", ...detected.data };
    }

    // ── Universal Download ──
    case "ocr_download": {
      if (!args.id) throw new Error("id is required.");
      const detected = await detectIdKind(args.id);
      const explicitFormat = args.format ? args.format.toLowerCase() : null;
      const outputPath = args.output_path;

      // ── Batch download ──
      if (detected.kind === "batch") {
        const format = explicitFormat || "zip";
        if (!["zip", "json"].includes(format)) {
          throw new Error(`Batch downloads only support format='zip' or 'json' (got '${format}').`);
        }
        const response = await fetch(
          `${BASE_URL}/api/v1/ocr/batch/${args.id}/download?format=${encodeURIComponent(format)}`,
          { headers: API_KEY ? { "X-API-Key": API_KEY } : {} }
        );
        if (!response.ok) {
          let detail = `HTTP ${response.status}`;
          try { const e = await response.json(); detail = e.detail || e.error || detail; } catch {}
          throw new Error(`Batch download error: ${detail}`);
        }
        const buffer = Buffer.from(await response.arrayBuffer());
        const finalPath = outputPath || path.join(process.cwd(), `batch_${args.id}_results.${format === "json" ? "json" : "zip"}`);
        fs.writeFileSync(finalPath, buffer);
        return { kind: "batch", status: "downloaded", batch_id: args.id, format, output_path: finalPath, bytes: buffer.length };
      }

      // ── Single job download ──
      const format = explicitFormat || "txt";
      if (["zip"].includes(format)) {
        throw new Error("format='zip' is only valid for batch ids.");
      }
      await ensureSession();
      const response = await fetch(
        `${BASE_URL}/api/download/${args.id}?format=${encodeURIComponent(format)}`,
        { headers: sessionCookie ? { Cookie: `session=${sessionCookie}` } : {} }
      );
      if (!response.ok) {
        let detail = `HTTP ${response.status}`;
        try { const e = await response.json(); detail = e.detail || e.error || detail; } catch {}
        throw new Error(`Download error: ${detail}`);
      }
      const isBinary = format === "docx";
      if (isBinary) {
        const buffer = Buffer.from(await response.arrayBuffer());
        if (outputPath) {
          fs.writeFileSync(outputPath, buffer);
          return { kind: "job", format, job_id: args.id, output_path: outputPath, bytes: buffer.length };
        }
        return { kind: "job", format, job_id: args.id, bytes: buffer.length, note: "Binary format — provide output_path to save." };
      }
      const text = await response.text();
      if (outputPath) {
        fs.writeFileSync(outputPath, text, "utf8");
        return { kind: "job", format, job_id: args.id, output_path: outputPath, bytes: Buffer.byteLength(text, "utf8") };
      }
      return { kind: "job", format, job_id: args.id, content_preview: text.slice(0, 5000), full_length: text.length };
    }

    // ── Job Management (list/delete) ──
    case "ocr_jobs": {
      const action = args.action || "list";
      await ensureSession();
      if (action === "list") {
        return await apiRequest("/api/jobs", { useSession: true });
      }
      if (action === "delete") {
        if (!args.job_id) throw new Error("job_id required for action='delete'.");
        return await apiRequest(`/api/jobs/${args.job_id}`, { method: "DELETE", useSession: true });
      }
      throw new Error(`Unknown action: ${action}`);
    }

    default:
      throw new Error(`Unknown tool: ${name}`);
  }
}

// ── MCP Server Setup ────────────────────────────────────────────────

const server = new Server(
  { name: "smart-pdf", version: "2.0.0" },
  { capabilities: { tools: {} } }
);

// ── Tool Filtering (env-based) ──────────────────────────────────────
const ENABLED_TOOLS_ENV = process.env.SMART_OCR_ENABLED_TOOLS || "";
const ENABLED_SET = ENABLED_TOOLS_ENV
  ? new Set(ENABLED_TOOLS_ENV.split(",").map((t) => t.trim()))
  : null;
const FILTERED_TOOLS = ENABLED_SET ? TOOLS.filter((t) => ENABLED_SET.has(t.name)) : TOOLS;

server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: FILTERED_TOOLS }));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;
  try {
    const result = await handleTool(name, args || {});
    return {
      content: [{ type: "text", text: typeof result === "string" ? result : JSON.stringify(result, null, 2) }],
    };
  } catch (error) {
    return {
      content: [{ type: "text", text: `Error: ${error.message}` }],
      isError: true,
    };
  }
});

// ── Start ───────────────────────────────────────────────────────────

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error("Smart PDF MCP server v2.0.0 running on stdio (consolidated 5 tools)");
}

main().catch((error) => {
  console.error("Fatal error:", error);
  process.exit(1);
});
