import assert from "node:assert/strict";
import { realpathSync } from "node:fs";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";

import { createRuntime, createSmartPdfServer, loadConfig, TOOL_NAMES } from "../index.js";

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

async function makeTempDir(t) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "smartpdf-mcp-"));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  return directory;
}

test("loadConfig validates authentication, paths, limits, and explicit tools", () => {
  const config = loadConfig({
    SMART_OCR_URL: "https://ocr.example.test///?ignored=true",
    SMART_OCR_API_KEY: "test-placeholder", // pragma: allowlist secret
    SMART_OCR_ALLOWED_ROOTS: "/tmp,/var/tmp",
    SMART_OCR_MAX_FILE_MB: "12",
    SMART_OCR_MAX_BATCH_FILES: "25",
    SMART_OCR_REQUEST_TIMEOUT_MS: "9000",
    SMART_OCR_ENABLED_TOOLS: "ocr_health,ocr_submit,ocr_status,ocr_download",
  });

  assert.equal(config.baseUrl, "https://ocr.example.test");
  assert.equal(config.maxFileBytes, 12 * 1024 * 1024);
  assert.equal(config.maxBatchFiles, 25);
  assert.equal(config.requestTimeoutMs, 9000);
  assert.deepEqual(config.allowedRoots, [realpathSync("/tmp"), realpathSync("/var/tmp")]);
  assert.throws(
    () => loadConfig({ SMART_OCR_USERNAME: "admin" }),
    /must be configured together/,
  );
  assert.throws(
    () => loadConfig({ SMART_OCR_ENABLED_TOOLS: "ocr_jobs" }),
    /lack required authentication/,
  );
  assert.throws(
    () => loadConfig({ SMART_OCR_ENABLED_TOOLS: "not_a_tool" }),
    /Unknown SMART_OCR_ENABLED_TOOLS/,
  );
});

test("MCP handshake exposes strict schemas, structured output, and annotations", async (t) => {
  const config = loadConfig({
    SMART_OCR_URL: "https://ocr.example.test",
    SMART_OCR_API_KEY: "test-placeholder", // pragma: allowlist secret
    SMART_OCR_USERNAME: "admin",
    SMART_OCR_PASSWORD: "test-placeholder", // pragma: allowlist secret
  });
  const fetchImpl = async (url) => {
    if (url.endsWith("/api/health")) return jsonResponse({ status: "ok", service: "smart-pdf" });
    throw new Error(`Unexpected request: ${url}`);
  };
  const { server } = createSmartPdfServer(config, { fetchImpl });
  const client = new Client({ name: "smart-pdf-test", version: "1.0.0" });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  t.after(async () => {
    await client.close();
    await server.close();
  });
  await Promise.all([client.connect(clientTransport), server.connect(serverTransport)]);

  const listed = await client.listTools();
  assert.deepEqual(listed.tools.map((tool) => tool.name), TOOL_NAMES);
  for (const tool of listed.tools) {
    assert.equal(tool.inputSchema.additionalProperties, false);
    assert.ok(tool.outputSchema, `${tool.name} must publish outputSchema`);
    assert.equal(typeof tool.annotations?.readOnlyHint, "boolean");
    assert.equal(typeof tool.annotations?.destructiveHint, "boolean");
  }

  const health = await client.callTool({ name: "ocr_health", arguments: {} });
  assert.equal(health.isError, undefined);
  assert.equal(health.structuredContent.status, "ok");
  assert.deepEqual(health.structuredContent.available_tools, TOOL_NAMES);

  const invalid = await client.callTool({
    name: "ocr_submit",
    arguments: { file_path: "/tmp/a.pdf", file_paths: ["/tmp/a.pdf", "/tmp/b.pdf"] },
  });
  assert.equal(invalid.isError, true);
});

test("API-key job status and download never fall through to session endpoints", async (t) => {
  const directory = await makeTempDir(t);
  const outputPath = path.join(directory, "result.html");
  const calls = [];
  const fetchImpl = async (url) => {
    calls.push(url);
    if (url.includes("/api/v1/ocr/batch/job-123")) return jsonResponse({ detail: "not found" }, 404);
    if (url.includes("/api/v1/ocr/job-123")) {
      return new Response("<html><body><h1>Internal result</h1></body></html>", {
        headers: { "content-type": "text/html" },
      });
    }
    throw new Error(`Unexpected request: ${url}`);
  };
  const config = loadConfig({
    SMART_OCR_URL: "https://ocr.example.test",
    SMART_OCR_API_KEY: "test-placeholder", // pragma: allowlist secret
    SMART_OCR_ALLOWED_ROOTS: directory,
  });
  const runtime = createRuntime(config, { fetchImpl });

  const status = await runtime.handleTool("ocr_status", { id: "job-123", include_text: true });
  assert.equal(status.status, "completed");
  assert.equal(status.text, "Internal result");

  const downloaded = await runtime.handleTool("ocr_download", {
    id: "job-123",
    format: "html",
    output_path: outputPath,
  });
  assert.equal(downloaded.output_path, path.join(realpathSync(directory), "result.html"));
  assert.match(await fs.readFile(outputPath, "utf8"), /Internal result/);
  assert.equal(calls.some((url) => url.includes("/api/auth/login") || url.includes("/api/download/")), false);
});

test("batch submit validates files and splits more than ten PDFs", async (t) => {
  const directory = await makeTempDir(t);
  const filePaths = [];
  for (let index = 0; index < 11; index += 1) {
    const filePath = path.join(directory, `input-${index}.pdf`);
    await fs.writeFile(filePath, "%PDF-1.4\n%%EOF\n");
    filePaths.push(filePath);
  }

  const chunkSizes = [];
  const fetchImpl = async (url, options) => {
    if (!url.includes("/api/v1/ocr/batch")) throw new Error(`Unexpected request: ${url}`);
    chunkSizes.push(options.body.getAll("files").length);
    return jsonResponse({ batch_id: `batch-${chunkSizes.length}`, status: "processing" });
  };
  const config = loadConfig({
    SMART_OCR_URL: "https://ocr.example.test",
    SMART_OCR_API_KEY: "test-placeholder", // pragma: allowlist secret
    SMART_OCR_ALLOWED_ROOTS: directory,
  });
  const runtime = createRuntime(config, { fetchImpl });
  const result = await runtime.handleTool("ocr_submit", { file_paths: filePaths, method: "auto" });

  assert.equal(result.kind, "batch_group");
  assert.deepEqual(result.batch_ids, ["batch-1", "batch-2"]);
  assert.deepEqual(chunkSizes, [10, 1]);
});

test("filesystem guard blocks traversal through symlinks and unsafe output names", async (t) => {
  const allowed = await makeTempDir(t);
  const outside = await makeTempDir(t);
  const outsidePdf = path.join(outside, "internal.pdf");
  const linkPdf = path.join(allowed, "linked.pdf");
  await fs.writeFile(outsidePdf, "%PDF-1.4\n%%EOF\n");
  await fs.symlink(outsidePdf, linkPdf);

  const config = loadConfig({
    SMART_OCR_API_KEY: "test-placeholder", // pragma: allowlist secret
    SMART_OCR_ALLOWED_ROOTS: allowed,
  });
  const runtime = createRuntime(config, {
    fetchImpl: async (url) => url.includes("/batch/")
      ? jsonResponse({ detail: "not found" }, 404)
      : new Response("<p>result</p>", { headers: { "content-type": "text/html" } }),
  });
  await assert.rejects(runtime.validateInputPdf(linkPdf), /outside SMART_OCR_ALLOWED_ROOTS/);
  await assert.rejects(
    runtime.handleTool("ocr_download", { id: "job", output_path: path.join(allowed, ".env") }),
    /secret-like filename/,
  );
});
