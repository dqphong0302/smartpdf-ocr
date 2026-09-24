"""Smart PDF — FastAPI Backend."""
import asyncio
import logging
import os
import secrets
import shutil
import sqlite3
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from auth import authenticate, init_auth_tables, logout, seed_default_user, validate_session
from database import (
    cleanup_expired_api_records,
    get_client_quota,
    init_db,
    load_api_batch,
    load_api_job,
    reconcile_interrupted_jobs,
    save_api_batch,
    save_api_job,
)
from dotenv import load_dotenv
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from book_translator import get_llm_config, translate_pdf_book, glossary_manager
from job_manager import Job, JobStatus, PageResult, PageStatus, job_manager
from latex_compiler import LatexCompileError, compile_latex_project, get_latex_health, prepare_latex_workspace
from ocr_engine import sanitize_ocr_html, smart_ocr, vision_ocr_batch
from pdf_analyzer import (
    analyze_pdf,
    extract_page_images,
    extract_page_markdown,
    extract_page_text,
    extract_pdf_metadata,
    get_page_thumbnail,
    render_page_to_image,
)
from pydantic import BaseModel

load_dotenv()

UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_SIZE = int(os.getenv("MAX_UPLOAD_SIZE_MB", "50")) * 1024 * 1024
MAX_PDF_PAGES = int(os.getenv("MAX_PDF_PAGES", "500"))
JOB_MAX_AGE_DAYS = int(os.getenv("JOB_MAX_AGE_DAYS", "7"))
MAX_CONCURRENT_JOBS = max(1, int(os.getenv("MAX_CONCURRENT_JOBS", "2")))
ENABLE_API_DOCS = os.getenv("ENABLE_API_DOCS", "false").lower() in {"1", "true", "yes", "on"}
LATEX_COMPILE_ENABLED = os.getenv("LATEX_COMPILE_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
API_JOB_RETENTION_SECONDS = max(300, int(os.getenv("API_JOB_RETENTION_SECONDS", "3600")))
SHUTDOWN_GRACE_SECONDS = max(1, int(os.getenv("SHUTDOWN_GRACE_SECONDS", "20")))

logger = logging.getLogger("smart-pdf")
_active_ocr_tasks: set[asyncio.Task] = set()
_draining = False


def _track_ocr_task(coro, label: str) -> asyncio.Task:
    task = asyncio.create_task(coro, name=label)
    _active_ocr_tasks.add(task)

    def _finished(completed: asyncio.Task):
        _active_ocr_tasks.discard(completed)
        if completed.cancelled():
            return
        error = completed.exception()
        if error:
            logger.error(
                "Background task %s failed",
                label,
                exc_info=(type(error), error, error.__traceback__),
            )

    task.add_done_callback(_finished)
    return task


async def _drain_ocr_tasks() -> None:
    pending = {task for task in _active_ocr_tasks if not task.done()}
    if not pending:
        return
    logger.info("Shutdown: waiting up to %ss for %s OCR task(s)", SHUTDOWN_GRACE_SECONDS, len(pending))
    _, pending = await asyncio.wait(pending, timeout=SHUTDOWN_GRACE_SECONDS)
    if pending:
        logger.warning("Shutdown: interrupting %s unfinished OCR task(s)", len(pending))
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _draining
    _draining = False
    init_db()
    recovery = reconcile_interrupted_jobs("Service restarted before OCR completed; submit the document again")
    if any(recovery.values()):
        logger.warning("Recovered interrupted jobs after startup: %s", recovery)
    cleanup_expired_api_records()
    init_auth_tables()
    if not seed_default_user():
        logger.warning("Admin user was not seeded because explicit credentials are missing")
    cleanup_task = asyncio.create_task(_cleanup_scheduler())
    try:
        yield
    finally:
        _draining = True
        await _drain_ocr_tasks()
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="Smart PDF",
    version="1.1.0",
    docs_url="/docs" if ENABLE_API_DOCS else None,
    redoc_url="/redoc" if ENABLE_API_DOCS else None,
    openapi_url="/openapi.json" if ENABLE_API_DOCS else None,
    lifespan=lifespan,
)

COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").lower() in {"1", "true", "yes", "on"}
COOKIE_SAMESITE = os.getenv("COOKIE_SAMESITE", "lax")
CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "*").split(",")
    if origin.strip()
]
if "*" in CORS_ORIGINS:
    logger.warning("CORS_ORIGINS contains '*'; cross-origin credentialed requests are disabled")
    CORS_ORIGINS = []

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_job_semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
_login_attempts: dict[str, deque[float]] = defaultdict(deque)
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_ATTEMPTS = 5


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self' wss:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
    )
    if COOKIE_SECURE:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


async def read_upload_limited(file: UploadFile) -> bytes:
    """Read an upload in bounded chunks and reject oversized bodies early."""
    chunks = []
    total = 0
    while chunk := await file.read(1024 * 1024):
        total += len(chunk)
        if total > MAX_SIZE:
            raise HTTPException(413, f"File too large. Max {MAX_SIZE // (1024 * 1024)}MB")
        chunks.append(chunk)
    return b"".join(chunks)


def safe_upload_name(filename: str | None, allowed_suffixes: set[str]) -> str:
    """Return a normalized basename and enforce the declared file type."""
    safe_name = Path(filename or "").name.strip()
    if not safe_name or Path(safe_name).suffix.lower() not in allowed_suffixes:
        expected = ", ".join(sorted(allowed_suffixes))
        raise HTTPException(400, f"Only {expected} files are accepted")
    return safe_name


# ── Auth ────────────────────────────────────────────────────────────

async def _cleanup_scheduler():
    """Run cleanup every 24 hours to delete expired jobs."""
    from database import cleanup_expired_jobs
    # Run an initial cleanup on startup
    await asyncio.sleep(10)  # wait for app to fully start
    stats = cleanup_expired_jobs(JOB_MAX_AGE_DAYS)
    if stats["deleted_jobs"] > 0:
        logger.info(f"Startup cleanup: {stats['deleted_jobs']} jobs deleted, {stats['bytes_freed_mb']}MB freed")
    last_full_cleanup = time.monotonic()
    while True:
        await asyncio.sleep(300)
        try:
            api_stats = cleanup_expired_api_records()
            for job_id in list(_api_jobs):
                if not load_api_job(job_id):
                    _api_jobs.pop(job_id, None)
            for batch_id in list(_batch_jobs):
                if not load_api_batch(batch_id):
                    _batch_jobs.pop(batch_id, None)
            if any(api_stats.values()):
                logger.info("Expired API results removed: %s", api_stats)
            if time.monotonic() - last_full_cleanup >= 86400:
                stats = cleanup_expired_jobs(JOB_MAX_AGE_DAYS)
                last_full_cleanup = time.monotonic()
                if stats["deleted_jobs"] > 0:
                    logger.info(
                        "Daily cleanup: %s jobs deleted, %sMB freed",
                        stats["deleted_jobs"],
                        stats["bytes_freed_mb"],
                    )
        except Exception as e:
            logger.exception("Cleanup failed: %s", e)


def get_client_ip(request: Request) -> str:
    """Extract proxy headers only when the direct peer is explicitly trusted."""
    peer = request.client.host if request.client and request.client.host else "127.0.0.1"
    trusted = {
        value.strip()
        for value in os.getenv("TRUSTED_PROXY_IPS", "127.0.0.1,::1").split(",")
        if value.strip()
    }
    if peer not in trusted:
        return peer
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip:
        return cf_ip.strip()
    x_forwarded = request.headers.get("x-forwarded-for")
    if x_forwarded:
        parts = [p.strip() for p in x_forwarded.split(",")]
        if parts and parts[0]:
            return parts[0]
    x_real = request.headers.get("x-real-ip")
    if x_real:
        return x_real.strip()
    return peer


def get_client_device_id(request: Request) -> str:
    """
    Extract unique machine/device identifier sent from browser hardware fingerprinting.
    Falls back to IP-based identifier if device header is omitted.
    """
    dev_id = request.headers.get("X-Device-Id") or request.headers.get("X-Client-Id") or request.cookies.get("device_id")
    if dev_id and len(dev_id.strip()) >= 8:
        clean = "".join(c for c in dev_id.strip() if c.isalnum() or c in "_-")
        if len(clean) >= 8:
            return f"dev_{clean[:64]}"
    return f"ip_{get_client_ip(request)}"


def get_current_user_role(request: Request) -> str:
    """Check if request has admin session or valid master API key."""
    token = request.cookies.get("session")
    if token and validate_session(token):
        return "admin"
    api_key = request.headers.get("X-API-Key") or ""
    if OCR_API_KEY and api_key and secrets.compare_digest(api_key, OCR_API_KEY):
        return "admin"
    return "guest"


def require_auth(request: Request):
    """Dependency: require admin session cookie or valid API key."""
    role = get_current_user_role(request)
    if role != "admin":
        raise HTTPException(401, "Chưa đăng nhập quyền quản trị")
    return role


class LoginBody(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
async def login(body: LoginBody, request: Request):
    client_key = get_client_ip(request)
    now = time.time()
    attempts = _login_attempts[client_key]
    while attempts and attempts[0] < now - LOGIN_WINDOW_SECONDS:
        attempts.popleft()
    if len(attempts) >= LOGIN_MAX_ATTEMPTS:
        raise HTTPException(429, "Too many login attempts. Try again later.")

    token = authenticate(body.username, body.password)
    if not token:
        attempts.append(now)
        raise HTTPException(401, "Sai tên đăng nhập hoặc mật khẩu")
    attempts.clear()
    resp = JSONResponse({"status": "ok", "username": body.username})
    resp.set_cookie(
        "session", token,
        httponly=True,
        max_age=30 * 24 * 3600,
        samesite=COOKIE_SAMESITE,
        secure=COOKIE_SECURE,
        path="/",
    )
    return resp


@app.post("/api/auth/logout")
async def logout_endpoint(request: Request):
    token = request.cookies.get("session")
    if token:
        logout(token)
    resp = JSONResponse({"status": "ok"})
    resp.delete_cookie("session", path="/", secure=COOKIE_SECURE, samesite=COOKIE_SAMESITE)
    return resp


@app.get("/api/auth/check")
async def check_auth(request: Request):
    token = request.cookies.get("session")
    username = validate_session(token)
    if not username:
        raise HTTPException(401, "Chưa đăng nhập")
    return {"status": "ok", "username": username}


# ── Client Quota Status Endpoint (Per Device) ──────────────────────
@app.get("/api/quota/status")
async def get_quota_status(request: Request, _user: str = Depends(require_auth)):
    """Return real-time server-side quota status for the requesting machine/device."""
    device_id = get_client_device_id(request)
    stat = get_client_quota(device_id, max_daily=5)
    return stat


# ── Upload PDF ──────────────────────────────────────────────────────
@app.post("/api/upload")
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    _user: str = Depends(require_auth),
):
    filename = safe_upload_name(file.filename, {".pdf"})
    content = await read_upload_limited(file)

    # Save file
    filepath = UPLOAD_DIR / f"{uuid.uuid4().hex[:12]}_{filename}"
    with open(filepath, "wb") as f:
        f.write(content)

    # Create job
    job = job_manager.create_job(filename=filename, filepath=str(filepath))

    # Analyze PDF
    await job_manager.update_job_status(job.job_id, JobStatus.ANALYZING)
    try:
        analysis = analyze_pdf(str(filepath))
        if analysis.total_pages > MAX_PDF_PAGES:
            raise ValueError(f"PDF has {analysis.total_pages} pages; maximum is {MAX_PDF_PAGES}")
    except Exception as e:
        job.error = str(e)
        await job_manager.update_job_status(job.job_id, JobStatus.FAILED)
        raise HTTPException(500, f"PDF analysis failed: {e}") from e

    job.total_pages = analysis.total_pages
    # Initialize page results
    for pa in analysis.pages:
        job.pages[pa.page_num] = PageResult(
            page_num=pa.page_num,
            classification=pa.classification,
        )

    job.status = JobStatus.UPLOADED
    # Persist to SQLite
    job_manager.persist_job(job.job_id)
    for pa in analysis.pages:
        job_manager.persist_page(job.job_id, pa.page_num)

    return {
        "job_id": job.job_id,
        "filename": filename,
        "analysis": analysis.to_dict(),
    }


# ── List All Jobs (Admin only) ──────────────────────────────────────
@app.get("/api/jobs")
async def list_jobs(_user: str = Depends(require_auth)):
    return job_manager.list_jobs()


# ── Get Job Status ──────────────────────────────────────────────────
@app.get("/api/jobs/{job_id}")
async def get_job(
    job_id: str,
    include_text: bool = False,
    _user: str = Depends(require_auth),
):
    data = job_manager.get_job_dict(job_id, include_text=include_text)
    if not data:
        raise HTTPException(404, "Job not found")
    return data


# ── Delete Job (Admin only) ─────────────────────────────────────────
@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str, _user: str = Depends(require_auth)):
    if not job_manager.delete_job(job_id):
        raise HTTPException(404, "Job not found")
    return {"status": "deleted", "job_id": job_id}


# ── Start OCR Processing ───────────────────────────────────────────
@app.post("/api/ocr/{job_id}")
async def start_ocr(
    job_id: str,
    pages: list[int] = Query(default=None, description="Page numbers to process"),
    mode: str = Query(default="all", description="all|odd|even|custom"),
    force_method: str = Query(default=None, description="tesseract|vision|auto"),
    extract_images: bool = Query(default=False, description="Extract original images"),
    _user: str = Depends(require_auth),
):
    if _draining:
        raise HTTPException(503, "Service is restarting; retry shortly")
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.status == JobStatus.PROCESSING:
        raise HTTPException(409, "Job is already processing")

    # Determine which pages to process
    all_pages = list(range(1, job.total_pages + 1))

    if mode == "odd":
        selected = [p for p in all_pages if p % 2 == 1]
    elif mode == "even":
        selected = [p for p in all_pages if p % 2 == 0]
    elif mode == "custom" and pages:
        selected = [p for p in pages if 1 <= p <= job.total_pages]
    else:
        selected = all_pages

    job.selected_pages = selected

    # Mark non-selected pages as skipped and persist to DB
    for p_num in all_pages:
        if p_num not in selected:
            job.pages[p_num].status = PageStatus.COMPLETED
            job.pages[p_num].method = "skipped"
            job_manager.persist_page(job.job_id, p_num)
    # Persist job state (selected_pages) to DB
    job_manager.persist_job(job.job_id)

    # Process in background
    _track_ocr_task(
        _process_pages_limited(job, selected, force_method, extract_images),
        f"ui-ocr-{job_id}",
    )

    return {"job_id": job_id, "selected_pages": selected, "total": len(selected)}


PARALLEL_BATCHES = int(os.getenv("PARALLEL_BATCHES", "4"))  # concurrent vision API requests
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "4"))  # pages per vision API call


def inline_base64_images(html_content: str, job_id: str) -> str:
    """Find all references to /api/extracted-images/{job_id}/{filename} in html_content,
    read them from disk, base64-encode them, and inline them into the src attribute."""
    import base64
    import os
    import re

    # Pattern matches src="/api/extracted-images/{job_id}/{filename}"
    pattern = r'src=["\']/api/extracted-images/([a-zA-Z0-9_\-]+)/([^"\']+)["\']'

    def replace_img(match):
        matched_job_id = match.group(1)
        filename = os.path.basename(match.group(2))
        img_path = UPLOAD_DIR / "extracted_images" / matched_job_id / filename
        if img_path.exists() and img_path.is_file():
            try:
                suffix = img_path.suffix.lstrip(".").lower()
                if suffix in ("jpg", "jpeg"):
                    mime = "image/jpeg"
                elif suffix == "png":
                    mime = "image/png"
                elif suffix == "gif":
                    mime = "image/gif"
                elif suffix == "webp":
                    mime = "image/webp"
                else:
                    mime = f"image/{suffix}"

                with open(img_path, "rb") as f:
                    b64_data = base64.b64encode(f.read()).decode("utf-8")
                return f'src="data:{mime};base64,{b64_data}"'
            except Exception as e:
                logger.error(f"Error encoding extracted image to base64 {img_path}: {e}")
        return match.group(0)

    return re.sub(pattern, replace_img, html_content)


def _enrich_html_with_images(job_id: str, page_num: int, filepath: str, html_text: str, extract_images: bool) -> str:
    """Helper to extract page images using PyMuPDF and append a styled gallery to the page HTML."""
    html_text = sanitize_ocr_html(html_text)
    if not extract_images:
        return html_text
    
    out_dir = UPLOAD_DIR / "extracted_images" / job_id
    try:
        saved_files = extract_page_images(filepath, page_num, str(out_dir))
    except Exception as e:
        logger.error(f"Error extracting images for job {job_id} page {page_num}: {e}")
        saved_files = []
        
    if not saved_files:
        return html_text
        
    # Generate HTML gallery
    gallery_html = '<div class="extracted-images-gallery" data-page="' + str(page_num) + '">'
    for fname in saved_files:
        img_url = f"/api/extracted-images/{job_id}/{fname}"
        gallery_html += (
            f'<div class="extracted-image-item" onclick="window.openLightbox(\'{img_url}\')">'
            f'<img src="{img_url}" alt="Page {page_num} Extracted Image" class="extracted-img" />'
            f'</div>'
        )
    gallery_html += '</div>'
    return f"{html_text}\n{gallery_html}"


async def _process_pages(job: Job, pages: list[int], force_method: str = None, extract_images: bool = False):
    """Background task: process selected pages with smart OCR and optional image extraction.
    
    Phase 1: Process digital/tesseract pages sequentially (fast, no API).
    Phase 2: Collect all vision pages, split into batches of BATCH_SIZE,
             then run up to PARALLEL_BATCHES concurrently via asyncio.gather.
    """
    import time as _time
    await job_manager.update_job_status(job.job_id, JobStatus.PROCESSING)

    # ── Phase 1: Handle digital & tesseract pages (fast, sequential) ──
    vision_pages = []  # collect (page_num, image) for batched vision processing
    
    for page_num in pages:
        page_result = job.pages[page_num]
        classification = page_result.classification

        await job_manager.update_page(
            job.job_id, page_num, status=PageStatus.PROCESSING
        )

        try:
            # Digital pages: extract text directly (no OCR needed)
            if classification == "digital" and force_method != "vision":
                import mistune
                start = _time.time()

                # Try pdf-inspector Markdown extraction first (layout-aware: tables, columns)
                md = extract_page_markdown(job.filepath, page_num)
                if md:
                    text = md  # store Markdown as the text representation
                    raw_html = mistune.html(md)
                else:
                    # Fallback: plain PyMuPDF text wrapped in <pre>
                    text = extract_page_text(job.filepath, page_num)
                    raw_html = f"<pre>{text}</pre>"

                elapsed = _time.time() - start
                enriched_html = _enrich_html_with_images(job.job_id, page_num, job.filepath, raw_html, extract_images)

                await job_manager.update_page(
                    job.job_id,
                    page_num,
                    status=PageStatus.COMPLETED,
                    method="digital",
                    text=text,
                    html_text=enriched_html,
                    confidence=100.0,
                    time_taken=elapsed,
                )
                continue

            # If force_method is tesseract, process individually
            if force_method == "tesseract":
                image = render_page_to_image(job.filepath, page_num)
                result = await smart_ocr(image, classification, force_method="tesseract")
                
                raw_html = result.get("html_text", f"<pre>{result['text']}</pre>")
                enriched_html = _enrich_html_with_images(job.job_id, page_num, job.filepath, raw_html, extract_images)
                
                await job_manager.update_page(
                    job.job_id,
                    page_num,
                    status=PageStatus.COMPLETED,
                    method=result["method"],
                    text=result["text"],
                    html_text=enriched_html,
                    confidence=result["confidence"],
                    time_taken=result["time_taken"],
                )
                continue

            # For auto/vision: try tesseract first on simple scans
            if classification == "scan_simple" and force_method != "vision":
                image = render_page_to_image(job.filepath, page_num)
                result = await smart_ocr(image, classification, force_method=None)
                if result["method"] == "tesseract":
                    # Tesseract was good enough
                    raw_html = result.get("html_text", f"<pre>{result['text']}</pre>")
                    enriched_html = _enrich_html_with_images(job.job_id, page_num, job.filepath, raw_html, extract_images)
                    
                    await job_manager.update_page(
                        job.job_id,
                        page_num,
                        status=PageStatus.COMPLETED,
                        method=result["method"],
                        text=result["text"],
                        html_text=enriched_html,
                        confidence=result["confidence"],
                        time_taken=result["time_taken"],
                    )
                    continue
                # If smart_ocr fell back to vision, it already did the API call
                raw_html = result.get("html_text", f"<pre>{result['text']}</pre>")
                enriched_html = _enrich_html_with_images(job.job_id, page_num, job.filepath, raw_html, extract_images)
                
                await job_manager.update_page(
                    job.job_id,
                    page_num,
                    status=PageStatus.COMPLETED,
                    method=result["method"],
                    text=result["text"],
                    html_text=enriched_html,
                    confidence=result["confidence"],
                    time_taken=result["time_taken"],
                )
                continue

            # Vision-destined pages: render image and collect for parallel batching
            image = render_page_to_image(job.filepath, page_num)
            vision_pages.append((page_num, image))

        except Exception as e:
            await job_manager.update_page(
                job.job_id,
                page_num,
                status=PageStatus.FAILED,
                error=str(e),
            )

    # ── Phase 2: Parallel vision batch processing ──
    if vision_pages:
        # Split into batches of BATCH_SIZE pages each
        batches = [
            vision_pages[i:i + BATCH_SIZE]
            for i in range(0, len(vision_pages), BATCH_SIZE)
        ]

        logger.info(
            f"Job {job.job_id}: {len(vision_pages)} vision pages → "
            f"{len(batches)} batches (size={BATCH_SIZE}), "
            f"parallel={PARALLEL_BATCHES}"
        )

        async def _run_batch(batch):
            """Process a single vision batch and update page results."""
            try:
                results = await vision_ocr_batch(batch)
                for (pn, _img), result in zip(batch, results, strict=True):
                    raw_html = result.get("html_text", f"<pre>{result['text']}</pre>")
                    enriched_html = _enrich_html_with_images(job.job_id, pn, job.filepath, raw_html, extract_images)
                    
                    await job_manager.update_page(
                        job.job_id,
                        pn,
                        status=PageStatus.COMPLETED,
                        method=result["method"],
                        text=result["text"],
                        html_text=enriched_html,
                        confidence=result["confidence"],
                        time_taken=result["time_taken"],
                    )
            except Exception as e:
                for pn, _ in batch:
                    await job_manager.update_page(
                        job.job_id,
                        pn,
                        status=PageStatus.FAILED,
                        error=str(e),
                    )

        # Run batches in parallel waves of PARALLEL_BATCHES
        for wave_start in range(0, len(batches), PARALLEL_BATCHES):
            wave = batches[wave_start:wave_start + PARALLEL_BATCHES]
            await asyncio.gather(*[_run_batch(b) for b in wave])

    await job_manager.update_job_status(job.job_id, JobStatus.COMPLETED)


async def _process_pages_limited(job: Job, pages: list[int], force_method: str = None, extract_images: bool = False):
    try:
        async with _job_semaphore:
            await _process_pages(job, pages, force_method, extract_images)
    except asyncio.CancelledError:
        job.error = "Service restarted before OCR completed; submit the document again"
        await job_manager.update_job_status(job.job_id, JobStatus.INTERRUPTED)
        raise
    except Exception as exc:
        job.error = str(exc)
        await job_manager.update_job_status(job.job_id, JobStatus.FAILED)
        logger.exception("UI OCR job %s failed", job.job_id)



# ── Page Thumbnail ──────────────────────────────────────────────────
@app.get("/api/thumbnail/{job_id}/{page_num}")
async def get_thumbnail(
    job_id: str,
    page_num: int,
    width: int = 200,
    _user: str = Depends(require_auth),
):
    width = max(64, min(width, 1600))
    job = job_manager.get_job(job_id)
    filepath = job.filepath if job else None
    if not filepath:
        # Try DB
        data = job_manager.get_job_dict(job_id)
        if not data:
            raise HTTPException(404, "Job not found")
        # Need to get filepath from DB
        from database import _get_conn
        conn = _get_conn()
        row = conn.execute("SELECT filepath FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        conn.close()
        filepath = row["filepath"] if row else None
    if not filepath:
        raise HTTPException(404, "Job not found")

    png_bytes = get_page_thumbnail(filepath, page_num, width)
    return Response(content=png_bytes, media_type="image/png")


# ── Extracted Images Safe Serving ───────────────────────────────────
@app.get("/api/extracted-images/{job_id}/{filename}")
async def get_extracted_image(
    job_id: str,
    filename: str,
    _user: str = Depends(require_auth),
):
    """Serve an extracted image safely from the uploads directory."""
    import os
    # Sandbox check: prevent directory traversal
    safe_filename = os.path.basename(filename)
    img_path = UPLOAD_DIR / "extracted_images" / job_id / safe_filename
    if not img_path.exists() or not img_path.is_file():
        raise HTTPException(404, "Extracted image not found")
    return FileResponse(str(img_path))


# ── Download Results ────────────────────────────────────────────────
@app.get("/api/download/{job_id}")
async def download_results(
    job_id: str,
    format: str = "txt",
    _user: str = Depends(require_auth),
):
    import urllib.parse
    export_format = (format or "txt").lower()
    if export_format == "text":
        export_format = "txt"
    # Get full data including text
    data = job_manager.get_job_dict(job_id, include_text=True)
    if not data:
        # Fallback to durable API jobs.
        data = _get_api_job(job_id)
        if not data:
            raise HTTPException(404, "Job not found")

    filename = data["filename"]
    pages = data["pages"]  # dict of str(page_num) -> page dict

    if export_format == "json":
        import json
        export = []
        for num in sorted(pages.keys(), key=int):
            p = pages[num]
            export.append({
                "page": int(num),
                "method": p.get("method"),
                "confidence": p.get("confidence", 0),
                "text": p.get("text", ""),
                "html_text": p.get("html_text", ""),
            })
        content = json.dumps(export, ensure_ascii=False, indent=2)
        filename_encoded = urllib.parse.quote(f"{filename}_ocr.json")
        return Response(
            content=content,
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename*=utf-8''{filename_encoded}"},
        )
    elif export_format == "html":
        html_parts = [
            '<!DOCTYPE html>',
            '<html lang="vi">',
            '<head>',
            f'<title>OCR: {filename}</title>',
            '<meta charset="utf-8">',
            '<style>',
            'body { font-family: Georgia, serif; max-width: 900px; margin: 0 auto; padding: 20px; background: #fafafa; color: #333; }',
            '.page { background: white; padding: 30px; margin: 20px 0; border: 1px solid #ddd; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }',
            '.page-header { border-bottom: 2px solid #eee; padding-bottom: 8px; margin-bottom: 16px; font-size: 14px; color: #888; }',
            'table { border-collapse: collapse; width: 100%; }',
            'td, th { border: 1px solid #ccc; padding: 6px 10px; }',
            '.extracted-images-gallery { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 12px; margin: 15px 0; padding: 10px; background: rgba(0, 0, 0, 0.02); border-radius: 8px; border: 1px solid rgba(0, 0, 0, 0.05); }',
            '.extracted-image-item { border-radius: 6px; overflow: hidden; border: 1px solid #eee; background: #fff; display: flex; align-items: center; justify-content: center; }',
            '.extracted-img { max-width: 100%; max-height: 200px; object-fit: contain; display: block; }',
            '</style>',
            '</head>',
            '<body>',
            f'<h1>📄 {filename}</h1>',
        ]
        for num in sorted(pages.keys(), key=int):
            p = pages[num]
            text = p.get("text", "")
            html_text = p.get("html_text", "")
            if text or html_text:
                html_parts.append('<div class="page">')
                html_parts.append(f'<div class="page-header">Trang {num} · {p.get("method", "")} · {p.get("confidence", 0)}%</div>')
                html_parts.append(html_text if html_text else f'<pre>{text}</pre>')
                html_parts.append('</div>')
        html_parts.append('</body></html>')
        content = '\n'.join(html_parts)
        # Inline images to Base64 so it is 100% self-contained
        content = inline_base64_images(content, job_id)
        filename_encoded = urllib.parse.quote(f"{filename}_ocr.html")
        return Response(
            content=content,
            media_type="text/html; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename*=utf-8''{filename_encoded}"},
        )
    elif export_format in {"markdown", "md"}:
        md_parts = [f"# {filename}\n"]
        for num in sorted(pages.keys(), key=int):
            p = pages[num]
            text = p.get("text", "")
            html_text = p.get("html_text", "")
            if text or html_text:
                md_parts.append(f"\n---\n\n## Trang {num} · {p.get('method', '')} · {p.get('confidence', 0)}%\n")
                md_parts.append(_html_to_markdown(html_text) if html_text else text)
        content = "\n".join(md_parts)
        filename_encoded = urllib.parse.quote(f"{filename}_ocr.md")
        return Response(
            content=content,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename*=utf-8''{filename_encoded}"},
        )
    elif export_format == "docx":
        try:
            from docx import Document
        except ImportError as exc:
            raise HTTPException(500, "DOCX export requires python-docx") from exc

        doc = Document()
        doc.add_heading(filename, level=1)
        for num in sorted(pages.keys(), key=int):
            p = pages[num]
            text = p.get("text", "")
            html_text = p.get("html_text", "")
            if text or html_text:
                doc.add_heading(
                    f"Trang {num} · {p.get('method', '')} · {p.get('confidence', 0)}%",
                    level=2,
                )
                body = _html_to_markdown(html_text) if html_text else text
                for line in body.splitlines():
                    if line.strip():
                        doc.add_paragraph(line)
                
                # Check for extracted images of this page and embed them into DOCX
                img_dir = UPLOAD_DIR / "extracted_images" / job_id
                if img_dir.exists() and img_dir.is_dir():
                    try:
                        import glob

                        from docx.shared import Inches
                        pattern = str(img_dir / f"page_{num}_img_*")
                        img_files = glob.glob(pattern)
                        
                        def get_img_idx(path_str):
                            import re
                            m = re.search(r'img_(\d+)\.', path_str)
                            return int(m.group(1)) if m else 0
                            
                        img_files.sort(key=get_img_idx)
                        
                        if img_files:
                            doc.add_heading("Hình ảnh trích xuất", level=3)
                            for img_path in img_files:
                                try:
                                    doc.add_picture(img_path, width=Inches(4.5))
                                    doc.add_paragraph(f"Ảnh: {os.path.basename(img_path)}")
                                except Exception as e:
                                    logger.error(f"Error adding picture {img_path} to docx: {e}")
                    except Exception as e:
                        logger.error(f"Failed to process docx image attachment for page {num}: {e}")
        import io
        buffer = io.BytesIO()
        doc.save(buffer)
        filename_encoded = urllib.parse.quote(f"{filename}_ocr.docx")
        return Response(
            content=buffer.getvalue(),
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f"attachment; filename*=utf-8''{filename_encoded}"},
        )
    elif export_format in {"pdf", "translated_pdf"}:
        job_filepath = data.get("filepath", "")
        output_pdf = Path(job_filepath).parent / f"translated_{filename}"
        if output_pdf.exists():
            filename_encoded = urllib.parse.quote(f"translated_{filename}")
            return FileResponse(
                path=str(output_pdf),
                media_type="application/pdf",
                headers={"Content-Disposition": f"attachment; filename*=utf-8''{filename_encoded}"},
            )
        raise HTTPException(404, "Translated PDF not found. Please run translation first.")
    else:
        lines = []
        for num in sorted(pages.keys(), key=int):
            p = pages[num]
            text = p.get("text", "")
            html_text = p.get("html_text", "")
            body = text or (_html_to_markdown(html_text) if html_text else "")
            if body:
                lines.append(f"--- Page {num} ({p.get('method', '')}, {p.get('confidence', 0)}%) ---")
                lines.append(body)
                lines.append("")
        content = "\n".join(lines)
        filename_encoded = urllib.parse.quote(f"{filename}_ocr.txt")
        return Response(
            content=content,
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename*=utf-8''{filename_encoded}"},
        )


def _html_to_markdown(html: str) -> str:
    """Convert HTML to Markdown (headings, bold, italic, tables, lists)."""
    import re
    from html import unescape

    t = html
    # Headings h1–h6
    for i in range(6, 0, -1):
        t = re.sub(
            rf'<h{i}[^>]*>(.*?)</h{i}>',
            lambda m, _i=i: '#' * _i + ' ' + re.sub(r'<[^>]+>', '', m.group(1)).strip(),
            t, flags=re.IGNORECASE | re.DOTALL
        )
    # Bold / italic
    t = re.sub(r'<(?:strong|b)[^>]*>(.*?)</(?:strong|b)>', r'**\1**', t, flags=re.IGNORECASE | re.DOTALL)
    t = re.sub(r'<(?:em|i)[^>]*>(.*?)</(?:em|i)>', r'*\1*', t, flags=re.IGNORECASE | re.DOTALL)
    # Tables → pipe table
    def _table_to_md(m):
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', m.group(), re.IGNORECASE | re.DOTALL)
        md_rows = []
        for idx, row in enumerate(rows):
            cells = re.findall(r'<(?:td|th)[^>]*>(.*?)</(?:td|th)>', row, re.IGNORECASE | re.DOTALL)
            cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
            md_rows.append('| ' + ' | '.join(cells) + ' |')
            if idx == 0:
                md_rows.append('|' + ' --- |' * len(cells))
        return '\n'.join(md_rows)
    t = re.sub(r'<table[^>]*>.*?</table>', _table_to_md, t, flags=re.IGNORECASE | re.DOTALL)
    # List items
    t = re.sub(r'<li[^>]*>(.*?)</li>', r'- \1', t, flags=re.IGNORECASE | re.DOTALL)
    t = re.sub(r'</?(?:ul|ol)[^>]*>', '', t, flags=re.IGNORECASE)
    # Line breaks and block closings
    t = re.sub(r'<br\s*/?>', '\n', t, flags=re.IGNORECASE)
    t = re.sub(r'</(?:p|div|tr|thead|tbody|h[1-6])>', '\n', t, flags=re.IGNORECASE)
    # Strip all remaining tags
    t = re.sub(r'<[^>]+>', '', t)
    t = unescape(t)
    # Normalise whitespace
    lines = [line.strip() for line in t.split('\n')]
    t = '\n'.join(line for line in lines if line)
    return t.strip()


# ── Book Translation Engine (AegisTrans Layout & Image Preservation) ──────
async def _process_translation_task(
    job: Job,
    mode: str,
    glossary_profile: str,
    model: str | None,
    selected_pages: list[int],
):
    output_dir = Path(job.filepath).parent
    output_pdf = output_dir / f"translated_{job.filename}"

    async def progress_cb(current: int, total: int, msg: str):
        job.status = JobStatus.PROCESSING
        await job_manager.broadcast(
            job.job_id,
            {
                "type": "translation_progress",
                "current": current,
                "total": total,
                "message": msg,
            },
        )

    try:
        page_indices = [p - 1 for p in selected_pages]
        res = await translate_pdf_book(
            input_pdf_path=job.filepath,
            output_pdf_path=output_pdf,
            mode=mode,
            glossary_profile=glossary_profile,
            model=model,
            pages=page_indices,
            progress_callback=progress_cb,
        )
        job.status = JobStatus.COMPLETED
        job.completed_at = time.time()
        job_manager.persist_job(job.job_id)
        await job_manager.broadcast(
            job.job_id,
            {
                "type": "translation_completed",
                "result": res,
                "download_url": f"/api/translate/{job.job_id}/download",
            },
        )
    except Exception as e:
        logger.exception("Translation job %s failed: %s", job.job_id, e)
        job.status = JobStatus.FAILED
        job.error = str(e)
        job_manager.persist_job(job.job_id)
        await job_manager.broadcast(
            job.job_id,
            {
                "type": "translation_failed",
                "error": str(e),
            },
        )


class TranslationPayload(BaseModel):
    mode: str = "inplace"
    glossary_profile: str = "general"
    model: str | None = None
    pages: list[int] | None = None


@app.post("/api/translate/{job_id}")
async def start_book_translation(
    job_id: str,
    payload: TranslationPayload | None = None,
    mode: str = Query(default="inplace", description="inplace | bilingual_dual"),
    glossary_profile: str = Query(default="general", description="general | medical | dental | tech"),
    model: str = Query(default=None, description="GPT model from 9router (e.g. gpt-5.6-luna)"),
    pages: list[int] = Query(default=None, description="Page numbers to translate (1-indexed)"),
    _user: str = Depends(require_auth),
):
    """Start high-fidelity PDF book translation strictly preserving images and layout."""
    if _draining:
        raise HTTPException(503, "Service is restarting; retry shortly")
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.status == JobStatus.PROCESSING:
        raise HTTPException(409, "Job is already processing")

    # Resolve arguments from payload if provided, otherwise fallback to query params
    effective_mode = (payload.mode if payload else None) or mode or "inplace"
    effective_profile = (payload.glossary_profile if payload else None) or glossary_profile or "general"
    effective_model = (payload.model if payload else None) or model
    effective_pages = (payload.pages if payload and payload.pages else None) or pages

    selected = effective_pages if effective_pages else list(range(1, job.total_pages + 1))
    job.selected_pages = selected
    job.status = JobStatus.PROCESSING
    job.started_at = time.time()
    job_manager.persist_job(job.job_id)

    _track_ocr_task(
        _process_translation_task(job, effective_mode, effective_profile, effective_model, selected),
        f"ui-translate-{job_id}",
    )
    return {
        "job_id": job_id,
        "mode": effective_mode,
        "glossary_profile": effective_profile,
        "model": effective_model or os.getenv("SMART_PDF_GPT_MODEL", "gpt-5.6-luna"),
        "selected_pages": selected,
        "total_selected": len(selected),
    }


@app.get("/api/translate/{job_id}/download")
async def download_translated_pdf_file(
    job_id: str,
    inline: bool = Query(default=False),
    _user: str = Depends(require_auth),
):
    """Download translated PDF with layout and images preserved, or view inline in browser."""
    import urllib.parse
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    output_pdf = Path(job.filepath).parent / f"translated_{job.filename}"
    if not output_pdf.exists():
        raise HTTPException(404, "Translated PDF not found or still processing")

    filename_encoded = urllib.parse.quote(f"translated_{job.filename}")
    disposition = "inline" if inline else f"attachment; filename*=utf-8''{filename_encoded}"
    return FileResponse(
        path=str(output_pdf),
        media_type="application/pdf",
        headers={"Content-Disposition": disposition},
    )


@app.get("/api/translate/glossaries")
async def get_available_glossaries():
    """List available domain glossaries and custom terminology files."""
    profiles = [
        {"id": "general", "name": "Học thuật Tổng quát", "desc": "Giữ nguyên cấu trúc câu học thuật và số liệu", "icon": "🌐"},
        {"id": "medical", "name": "Y khoa Lâm sàng", "desc": "Bộ chuẩn MeSH / UMLS / ICD-10 và Giải phẫu học", "icon": "🩺"},
        {"id": "dental", "name": "Nha khoa & Khớp TMD", "desc": "Khớp thái dương hàm, Cắn khớp, Implant, Nha chu", "icon": "🦷"},
        {"id": "tech", "name": "Kỹ thuật & Công nghệ", "desc": "Hệ thống phân tán, AI/ML, Cloud, Vi mạch", "icon": "💻"},
    ]
    cached_profiles = set(glossary_manager._cache.keys())
    existing_ids = {p["id"] for p in profiles}
    for cp in cached_profiles:
        if cp not in existing_ids and not cp.startswith("book_"):
            profiles.append({
                "id": cp,
                "name": cp.capitalize(),
                "desc": f"Từ điển tùy biến ({len(glossary_manager._cache[cp])} thuật ngữ)",
                "icon": "📚",
            })
    return {"glossaries": profiles}


@app.get("/api/translate/{job_id}/glossary")
async def get_job_glossary(job_id: str, _user: str = Depends(require_auth)):
    """Retrieve auto-mined or matched terminology for this document."""
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    book_key = f"book_{Path(job.filepath).stem.lower()}"
    terms = glossary_manager._cache.get(book_key, {})
    return {"job_id": job_id, "terms": terms, "total_terms": len(terms)}


@app.get("/api/v1/models/gpt")
async def list_available_gpt_models():
    """List configured 9router GPT translation models."""
    base_url, api_key, active_model = get_llm_config()
    return {
        "active_model": active_model,
        "base_url": base_url,
        "configured": bool(base_url and api_key),
        "supported_models": [
            "gh/gpt-5.4-mini",
            "gh/gpt-5.4",
            "gpt-5.6-luna",
            "model-chinh",
            "image-vision",
        ],
    }


# ── WebSocket for real-time progress ───────────────────────────────
@app.websocket("/ws/{job_id}")
async def websocket_endpoint(websocket: WebSocket, job_id: str):
    token = websocket.cookies.get("session")
    origin = websocket.headers.get("origin")
    host = websocket.headers.get("host", "")
    same_origin = origin in {f"http://{host}", f"https://{host}"}
    if not validate_session(token) or (origin and not same_origin and origin not in CORS_ORIGINS):
        await websocket.close(code=4401)
        return

    job = job_manager.get_job(job_id)
    if not job:
        await websocket.close(code=4004)
        return

    await websocket.accept()
    await job_manager.add_websocket(job_id, websocket)

    # Send current state
    await websocket.send_json({
        "type": "init",
        "job": job.to_dict(),
    })

    try:
        while True:
            # Keep connection alive, wait for client messages
            await websocket.receive_text()
    except WebSocketDisconnect:
        await job_manager.remove_websocket(job_id, websocket)


# ── Simple OCR API (async job pattern, for MCP / external clients) ─
OCR_API_KEY = os.getenv("OCR_API_KEY", "")


def require_api_key(request: Request):
    """Dependency: require valid API key via X-API-Key header."""
    key = request.headers.get("X-API-Key", "")
    if not OCR_API_KEY or not secrets.compare_digest(key, OCR_API_KEY):
        raise HTTPException(401, "Invalid or missing API key")
    return True


# Hot cache backed by SQLite. Completed results remain available after restart.
_api_jobs: dict[str, dict] = {}
_batch_jobs: dict[str, dict] = {}


def _get_api_job(job_id: str) -> dict | None:
    job = _api_jobs.get(job_id) or load_api_job(job_id)
    if job:
        _api_jobs[job_id] = job
    return job


def _get_api_batch(batch_id: str) -> dict | None:
    batch = _batch_jobs.get(batch_id) or load_api_batch(batch_id)
    if batch:
        _batch_jobs[batch_id] = batch
    return batch


def _finish_api_record(record: dict, status: str, error: str | None = None) -> None:
    now = time.time()
    record["status"] = status
    record["completed_at"] = now
    record["expires_at"] = now + API_JOB_RETENTION_SECONDS
    if error is not None:
        record["error"] = error


def _select_api_pages(spec: str, total_pages: int) -> list[int]:
    all_pages = list(range(1, total_pages + 1))
    if spec == "all":
        return all_pages
    if spec == "odd":
        return [page for page in all_pages if page % 2 == 1]
    if spec == "even":
        return [page for page in all_pages if page % 2 == 0]
    selected = [int(part.strip()) for part in spec.split(",") if part.strip().isdigit()]
    return [page for page in selected if 1 <= page <= total_pages]


@app.post("/api/v1/ocr")
async def simple_ocr_submit(
    file: UploadFile = File(...),
    pages: str = Query(default="all", description="Pages: all, 1,3,5, odd, even"),
    method: str = Query(default="auto", description="auto|tesseract|vision"),
    extract_images: bool = Query(default=False, description="Extract original images"),
    _auth: bool = Depends(require_api_key),
):
    """Submit a PDF for OCR and return a durable polling identifier."""
    if _draining:
        raise HTTPException(503, "Service is restarting; retry shortly")
    if method not in {"auto", "tesseract", "vision"}:
        raise HTTPException(400, "method must be auto, tesseract, or vision")

    filename = safe_upload_name(file.filename, {".pdf"})
    content = await read_upload_limited(file)
    filepath = UPLOAD_DIR / f"api_{uuid.uuid4().hex[:12]}_{filename}"
    with open(filepath, "wb") as output:
        output.write(content)

    try:
        analysis = analyze_pdf(str(filepath))
        if analysis.total_pages > MAX_PDF_PAGES:
            raise ValueError(f"PDF has {analysis.total_pages} pages; maximum is {MAX_PDF_PAGES}")
    except Exception as exc:
        filepath.unlink(missing_ok=True)
        raise HTTPException(400, f"PDF analysis failed: {exc}") from exc

    selected = _select_api_pages(pages, analysis.total_pages)
    if not selected:
        filepath.unlink(missing_ok=True)
        raise HTTPException(400, "No valid pages selected")

    now = time.time()
    job_id = uuid.uuid4().hex[:12]
    job = {
        "status": "processing",
        "filename": filename,
        "filepath": str(filepath),
        "total_pages": len(selected),
        "completed_pages": 0,
        "selected_pages": selected,
        "method": method,
        "pages": {},
        "created_at": now,
        "started_at": now,
        "completed_at": None,
        "expires_at": None,
        "html_result": None,
        "error": None,
    }
    _api_jobs[job_id] = job
    save_api_job(job_id, job)
    _track_ocr_task(
        _run_api_ocr(job_id, str(filepath), filename, selected, method, analysis, extract_images),
        f"api-ocr-{job_id}",
    )

    return {
        "job_id": job_id,
        "status": "processing",
        "filename": filename,
        "total_pages": len(selected),
        "poll_url": f"/api/v1/ocr/{job_id}",
    }


async def _run_api_ocr(job_id, filepath, filename, selected, method, analysis, extract_images):
    async with _job_semaphore:
        await _api_ocr_process(
            job_id, filepath, filename, selected, method, analysis, extract_images
        )


async def _api_ocr_process(
    job_id: str,
    filepath: str,
    filename: str,
    selected: list[int],
    method: str,
    analysis,
    extract_images: bool = False,
):
    """OCR selected pages and persist progress after every completed page."""
    job = _get_api_job(job_id)
    if not job:
        raise RuntimeError(f"API job {job_id} disappeared")
    force = method if method != "auto" else None
    page_results_map = {int(page): result for page, result in job.get("pages", {}).items()}

    def record_page(page_num: int, result: dict) -> None:
        raw_html = result.get("html_text", f"<pre>{result.get('text', '')}</pre>")
        enriched_html = _enrich_html_with_images(
            job_id, page_num, filepath, raw_html, extract_images
        )
        page_results_map[page_num] = {
            "page": page_num,
            "method": result["method"],
            "confidence": result["confidence"],
            "text": result.get("text", ""),
            "html_text": enriched_html,
        }
        job["pages"] = {str(page): value for page, value in page_results_map.items()}
        job["completed_pages"] = len(page_results_map)
        save_api_job(job_id, job)

    try:
        vision_pages = []
        for page_num in selected:
            page_info = next((page for page in analysis.pages if page.page_num == page_num), None)
            classification = page_info.classification if page_info else "scan_complex"

            if classification == "digital" and force != "vision":
                text = extract_page_text(filepath, page_num)
                record_page(
                    page_num,
                    {"method": "digital", "confidence": 100.0, "text": text},
                )
                continue

            if force == "tesseract":
                image = render_page_to_image(filepath, page_num)
                record_page(
                    page_num,
                    await smart_ocr(image, classification, force_method="tesseract"),
                )
                continue

            if classification == "scan_simple" and force != "vision":
                image = render_page_to_image(filepath, page_num)
                record_page(page_num, await smart_ocr(image, classification, force_method=None))
                continue

            vision_pages.append((page_num, render_page_to_image(filepath, page_num)))

        if vision_pages:
            batches = [
                vision_pages[index:index + BATCH_SIZE]
                for index in range(0, len(vision_pages), BATCH_SIZE)
            ]
            logger.info(
                "API job %s: %s vision pages in %s batches (parallel=%s)",
                job_id,
                len(vision_pages),
                len(batches),
                PARALLEL_BATCHES,
            )

            async def run_batch(batch):
                results = await vision_ocr_batch(batch)
                for (page_num, _image), result in zip(batch, results, strict=True):
                    record_page(page_num, result)

            for wave_start in range(0, len(batches), PARALLEL_BATCHES):
                wave = batches[wave_start:wave_start + PARALLEL_BATCHES]
                await asyncio.gather(*(run_batch(batch) for batch in wave))

        page_results = [page_results_map[page] for page in selected if page in page_results_map]
        html_parts = [
            "<!DOCTYPE html>",
            '<html lang="vi"><head>',
            f"<title>OCR: {filename}</title>",
            '<meta charset="utf-8">',
            "<style>body{font-family:Georgia,serif;max-width:900px;margin:0 auto;padding:20px;background:#fafafa;color:#333}"
            ".page{background:white;padding:30px;margin:20px 0;border:1px solid #ddd}"
            ".page-header{border-bottom:2px solid #eee;padding-bottom:8px;margin-bottom:16px;color:#888}"
            "table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:6px 10px}</style>",
            "</head><body>",
            f"<h1>📄 {filename}</h1>",
            f'<p style="color:#888">Pages: {len(page_results)} | Method: {method}</p>',
        ]
        for result in page_results:
            html_parts.extend(
                [
                    '<div class="page">',
                    f'<div class="page-header">Trang {result["page"]} · {result["method"]} · {result["confidence"]}%</div>',
                    result["html_text"],
                    "</div>",
                ]
            )
        html_parts.append("</body></html>")
        html_content = "\n".join(html_parts)
        job["html_result"] = inline_base64_images(html_content, job_id) if extract_images else html_content
        _finish_api_record(job, "completed")
    except asyncio.CancelledError:
        _finish_api_record(
            job,
            "interrupted",
            "Service restarted before OCR completed; submit the document again",
        )
        raise
    except Exception as exc:
        _finish_api_record(job, "failed", str(exc))
        logger.exception("API OCR job %s failed", job_id)
    finally:
        Path(filepath).unlink(missing_ok=True)
        job["filepath"] = ""
        save_api_job(job_id, job)


@app.get("/api/v1/ocr/{job_id}")
async def simple_ocr_status(job_id: str, _auth: bool = Depends(require_api_key)):
    """Poll OCR job status. Completed jobs return the generated HTML."""
    import urllib.parse

    job = _get_api_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found or expired")
    if job["status"] == "processing":
        return {
            "job_id": job_id,
            "status": "processing",
            "progress": f"{job['completed_pages']}/{job['total_pages']}",
        }
    if job["status"] in {"failed", "interrupted"}:
        return {"job_id": job_id, "status": job["status"], "error": job["error"]}

    filename_encoded = urllib.parse.quote(f"{job['filename']}_ocr.html")
    return Response(
        content=job["html_result"] or "",
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=utf-8''{filename_encoded}"},
    )


@app.post("/api/v1/ocr/batch")
async def batch_ocr_submit(
    files: list[UploadFile] = File(...),
    method: str = Query(default="auto", description="auto|tesseract|vision"),
    _auth: bool = Depends(require_api_key),
):
    """Submit multiple PDFs for OCR and return a durable batch identifier."""
    if _draining:
        raise HTTPException(503, "Service is restarting; retry shortly")
    if not files or len(files) > 10:
        raise HTTPException(400, "Batch must contain between 1 and 10 PDF files")
    if method not in {"auto", "tesseract", "vision"}:
        raise HTTPException(400, "method must be auto, tesseract, or vision")

    batch_id = uuid.uuid4().hex[:12]
    saved_files = []
    try:
        for upload in files:
            filename = safe_upload_name(upload.filename, {".pdf"})
            content = await read_upload_limited(upload)
            filepath = UPLOAD_DIR / f"batch_{batch_id}_{uuid.uuid4().hex[:12]}_{filename}"
            with open(filepath, "wb") as output:
                output.write(content)
            saved_files.append((str(filepath), filename))
    except Exception:
        for filepath, _filename in saved_files:
            Path(filepath).unlink(missing_ok=True)
        raise

    now = time.time()
    batch = {
        "status": "processing",
        "total_files": len(saved_files),
        "completed_files": 0,
        "results": [],
        "created_at": now,
        "completed_at": None,
        "expires_at": None,
        "error": None,
    }
    _batch_jobs[batch_id] = batch
    save_api_batch(batch_id, batch)
    _track_ocr_task(_process_batch_task(batch_id, saved_files, method), f"api-batch-{batch_id}")

    return {
        "batch_id": batch_id,
        "status": "processing",
        "total_files": len(saved_files),
        "poll_url": f"/api/v1/ocr/batch/{batch_id}",
    }


async def _process_batch_task(batch_id: str, saved_files: list, method: str):
    batch = _get_api_batch(batch_id)
    if not batch:
        raise RuntimeError(f"API batch {batch_id} disappeared")
    try:
        for filepath, filename in saved_files:
            item_finished = False
            try:
                analysis = analyze_pdf(filepath)
                if analysis.total_pages > MAX_PDF_PAGES:
                    raise ValueError(
                        f"PDF has {analysis.total_pages} pages; maximum is {MAX_PDF_PAGES}"
                    )
                selected = list(range(1, analysis.total_pages + 1))
                now = time.time()
                job_id = uuid.uuid4().hex[:12]
                job = {
                    "status": "processing",
                    "filename": filename,
                    "filepath": filepath,
                    "total_pages": len(selected),
                    "completed_pages": 0,
                    "selected_pages": selected,
                    "method": method,
                    "pages": {},
                    "created_at": now,
                    "started_at": now,
                    "completed_at": None,
                    "expires_at": None,
                    "html_result": None,
                    "error": None,
                }
                _api_jobs[job_id] = job
                save_api_job(job_id, job)
                async with _job_semaphore:
                    await _api_ocr_process(job_id, filepath, filename, selected, method, analysis)
                batch["results"].append(
                    {
                        "job_id": job_id,
                        "filename": filename,
                        "status": job["status"],
                        "html_result": job.get("html_result"),
                        "error": job.get("error"),
                    }
                )
                item_finished = True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Batch %s failed to process %s", batch_id, filename)
                batch["results"].append(
                    {"filename": filename, "status": "failed", "error": str(exc)}
                )
                item_finished = True
            finally:
                if item_finished:
                    batch["completed_files"] += 1
                Path(filepath).unlink(missing_ok=True)
                save_api_batch(batch_id, batch)
        _finish_api_record(batch, "completed")
    except asyncio.CancelledError:
        _finish_api_record(
            batch,
            "interrupted",
            "Service restarted before batch OCR completed; submit the batch again",
        )
        raise
    finally:
        for filepath, _filename in saved_files:
            Path(filepath).unlink(missing_ok=True)
        save_api_batch(batch_id, batch)


@app.get("/api/v1/ocr/batch/{batch_id}")
async def batch_ocr_status(batch_id: str, _auth: bool = Depends(require_api_key)):
    data = _get_api_batch(batch_id)
    if not data:
        raise HTTPException(404, "Batch job not found")
    return {
        "batch_id": batch_id,
        "status": data["status"],
        "total_files": data["total_files"],
        "completed_files": data["completed_files"],
        "error": data["error"],
    }


@app.get("/api/v1/ocr/batch/{batch_id}/download")
async def batch_ocr_download(
    batch_id: str,
    format: str = "zip",
    _auth: bool = Depends(require_api_key),
):
    data = _get_api_batch(batch_id)
    if not data:
        raise HTTPException(404, "Batch job not found")
    if data["status"] != "completed":
        raise HTTPException(400, f"Batch job is {data['status']}")

    if format == "json":
        import json

        return Response(
            content=json.dumps(data["results"], ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="batch_{batch_id}_results.json"'},
        )

    import io
    import zipfile

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "a", zipfile.ZIP_DEFLATED, False) as zip_file:
        for result in data["results"]:
            filename = result["filename"]
            if result["status"] == "completed" and result.get("html_result"):
                zip_file.writestr(f"{filename}.html", result["html_result"].encode("utf-8"))
            else:
                zip_file.writestr(
                    f"{filename}_ERROR.txt",
                    result.get("error", "Unknown error").encode("utf-8"),
                )

    return Response(
        content=zip_buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="batch_{batch_id}_results.zip"'},
    )


@app.post("/api/v1/pdf/metadata")
async def pdf_metadata_endpoint(
    file: UploadFile = File(...),
    _auth: bool = Depends(require_api_key),
):
    """Extract zero-token academic metadata, DOI, arXiv, PMID, and text structure from PDF."""
    filename = safe_upload_name(file.filename, {".pdf"})
    content = await read_upload_limited(file)
    filepath = UPLOAD_DIR / f"meta_{uuid.uuid4().hex[:12]}_{filename}"
    try:
        with open(filepath, "wb") as f:
            f.write(content)
        meta = extract_pdf_metadata(str(filepath))
        return {
            "status": "ok",
            "filename": filename,
            "metadata": meta,
        }
    finally:
        if filepath.exists():
            try:
                os.remove(filepath)
            except OSError:
                pass


@app.post("/api/v1/latex/compile")
async def latex_compile_endpoint(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    main_file: str = Form(default="main.tex"),
    engine: str = Form(default="latexmk"),
    timeout: int = Form(default=120),
    _auth: bool = Depends(require_api_key),
):
    """Compile a LaTeX .tex file or .zip project and return the generated PDF."""
    if not LATEX_COMPILE_ENABLED:
        raise HTTPException(503, "LaTeX compilation is disabled")
    filename = safe_upload_name(file.filename or "main.tex", {".tex", ".zip"})
    content = await read_upload_limited(file)

    job_dir = UPLOAD_DIR / f"latex_{uuid.uuid4().hex[:12]}"
    try:
        main_tex = prepare_latex_workspace(job_dir, filename, content, main_file)
        result = await compile_latex_project(
            job_dir=job_dir,
            main_tex=main_tex,
            requested_engine=engine,
            timeout=timeout,
        )
    except LatexCompileError as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=exc.to_detail()) from exc
    except Exception as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail={"message": str(exc)}) from exc

    background_tasks.add_task(shutil.rmtree, job_dir, True)
    return FileResponse(
        path=result.pdf_path,
        media_type="application/pdf",
        filename=result.download_name,
        headers={
            "X-LaTeX-Engine": result.engine,
        },
        background=background_tasks,
    )


# ── Health Check ────────────────────────────────────────────────────
def collect_health() -> tuple[str, dict]:
    """Collect dependency health without exposing configuration publicly."""
    db_ok = False
    db_error = None
    try:
        from database import DB_PATH
        conn = sqlite3.connect(DB_PATH)
        conn.execute("SELECT 1")
        conn.close()
        db_ok = True
    except Exception as exc:
        db_error = str(exc)

    upload_writable = False
    upload_error = None
    try:
        probe = UPLOAD_DIR / ".healthcheck"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        upload_writable = True
    except Exception as exc:
        upload_error = str(exc)

    tesseract_path = shutil.which("tesseract")
    latex_health = get_latex_health()

    checks = {
        "database": {"ok": db_ok, "error": db_error},
        "uploads": {"ok": upload_writable, "error": upload_error},
        "tesseract": {"ok": bool(tesseract_path)},
        "vision": {
            "ok": bool(os.getenv("OPENAI_BASE_URL") and os.getenv("OPENAI_API_KEY")),
            "model": os.getenv("VISION_MODEL", ""),
        },
        "latex": {
            "ok": not LATEX_COMPILE_ENABLED or latex_health["ok"],
            "enabled": LATEX_COMPILE_ENABLED,
        },
    }
    status = "ok" if all(item["ok"] for item in checks.values()) else "degraded"
    return status, checks


@app.get("/api/health")
async def health():
    status, _checks = collect_health()
    return {
        "status": status,
        "service": "smart-pdf",
        "version": app.version,
    }


@app.get("/api/health/details")
async def health_details(_auth: bool = Depends(require_api_key)):
    status, checks = collect_health()
    return {"status": status, "service": "smart-pdf", "version": app.version, "checks": checks}


# ── Static files (production) ──────────────────────────────────────
DIST_DIR = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if DIST_DIR.exists():
    app.mount("/assets", StaticFiles(directory=DIST_DIR / "assets"), name="static")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        """Serve index.html for all non-API routes (SPA fallback)."""
        if full_path == "docs" or full_path == "redoc" or full_path == "openapi.json" or full_path.startswith("api/"):
            raise HTTPException(404, "Not found")
        file_path = (DIST_DIR / full_path).resolve()
        if full_path and file_path.is_relative_to(DIST_DIR.resolve()) and file_path.exists() and file_path.is_file():
            return FileResponse(file_path)
        return FileResponse(DIST_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
