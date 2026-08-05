"""Job Manager — In-memory cache + SQLite persistence with WebSocket broadcast."""
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum

from database import (
    delete_job_from_db,
    init_db,
    list_jobs_from_db,
    load_job_dict,
    save_job,
    save_page_result,
)
from fastapi import WebSocket


class JobStatus(StrEnum):
    UPLOADED = "uploaded"
    ANALYZING = "analyzing"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class PageMethod(StrEnum):
    DIGITAL = "digital"
    TESSERACT = "tesseract"
    VISION = "vision"
    SKIPPED = "skipped"


class PageStatus(StrEnum):
    PENDING = "pending"
    ANALYZING = "analyzing"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class PageResult:
    page_num: int
    status: PageStatus = PageStatus.PENDING
    classification: str = ""  # digital, scan_simple, scan_complex
    method: str | None = None
    text: str = ""
    html_text: str = ""  # HTML-formatted OCR output
    confidence: float = 0.0
    time_taken: float = 0.0
    error: str | None = None

    def to_dict(self):
        return {
            "page_num": self.page_num,
            "status": self.status.value,
            "classification": self.classification,
            "method": self.method,
            "text": self.text,
            "html_text": self.html_text,
            "confidence": self.confidence,
            "time_taken": round(self.time_taken, 2),
            "error": self.error,
        }


@dataclass
class Job:
    job_id: str
    filename: str
    filepath: str
    status: JobStatus = JobStatus.UPLOADED
    total_pages: int = 0
    selected_pages: list = field(default_factory=list)
    pages: dict = field(default_factory=dict)  # page_num -> PageResult
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    error: str | None = None

    def to_dict(self, include_text: bool = False):
        pages_data = {}
        for num, page in self.pages.items():
            d = page.to_dict()
            if not include_text:
                d.pop("text", None)
            pages_data[str(num)] = d

        elapsed = 0
        if self.started_at:
            end = self.completed_at or time.time()
            elapsed = round(end - self.started_at, 2)

        return {
            "job_id": self.job_id,
            "filename": self.filename,
            "status": self.status.value,
            "total_pages": self.total_pages,
            "selected_pages": self.selected_pages,
            "pages": pages_data,
            "elapsed_time": elapsed,
            "created_at": self.created_at,
            "error": self.error,
            "summary": self._summary(),
        }

    def _summary(self):
        methods = {"digital": 0, "tesseract": 0, "vision": 0, "skipped": 0}
        completed = 0
        total_conf = 0.0
        conf_count = 0
        for p in self.pages.values():
            if p.status == PageStatus.COMPLETED:
                if p.method:
                    methods[p.method] = methods.get(p.method, 0) + 1
                # Only count non-skipped pages as completed for progress
                if p.method and p.method != "skipped":
                    completed += 1
                    if p.confidence > 0:
                        total_conf += p.confidence
                        conf_count += 1
        total = len(self.selected_pages) if self.selected_pages else self.total_pages
        return {
            "completed": completed,
            "total": total,
            "methods": methods,
            "avg_confidence": round(total_conf / conf_count, 1) if conf_count else 0,
        }


class JobManager:
    def __init__(self):
        self._jobs: dict[str, Job] = {}  # in-memory cache for active jobs
        self._websockets: dict[str, list[WebSocket]] = {}
        init_db()

    def create_job(self, filename: str, filepath: str) -> Job:
        job_id = str(uuid.uuid4())[:8]
        job = Job(job_id=job_id, filename=filename, filepath=filepath)
        self._jobs[job_id] = job
        save_job(job)
        return job

    def get_job(self, job_id: str) -> Job | None:
        """Get from in-memory cache (active jobs only)."""
        return self._jobs.get(job_id)

    def get_job_dict(self, job_id: str, include_text: bool = False) -> dict | None:
        """Get job as dict — tries in-memory first, falls back to SQLite."""
        job = self._jobs.get(job_id)
        if job:
            return job.to_dict(include_text=include_text)
        return load_job_dict(job_id, include_text=include_text)

    def list_jobs(self) -> list[dict]:
        """List all jobs from SQLite (includes historical)."""
        return list_jobs_from_db()

    def delete_job(self, job_id: str) -> bool:
        """Delete from both in-memory and SQLite."""
        self._jobs.pop(job_id, None)
        self._websockets.pop(job_id, None)
        return delete_job_from_db(job_id)

    def persist_job(self, job_id: str) -> None:
        """Save current in-memory job state to SQLite."""
        job = self._jobs.get(job_id)
        if job:
            save_job(job)

    def persist_page(self, job_id: str, page_num: int) -> None:
        """Save a single page result to SQLite."""
        job = self._jobs.get(job_id)
        if job and page_num in job.pages:
            save_page_result(job_id, job.pages[page_num])

    async def add_websocket(self, job_id: str, ws: WebSocket):
        if job_id not in self._websockets:
            self._websockets[job_id] = []
        self._websockets[job_id].append(ws)

    async def remove_websocket(self, job_id: str, ws: WebSocket):
        if job_id in self._websockets:
            self._websockets[job_id] = [w for w in self._websockets[job_id] if w != ws]

    async def broadcast(self, job_id: str, data: dict):
        if job_id not in self._websockets:
            return
        dead = []
        for ws in self._websockets[job_id]:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._websockets[job_id].remove(ws)

    async def update_page(self, job_id: str, page_num: int, **kwargs):
        job = self._jobs.get(job_id)
        if not job or page_num not in job.pages:
            return
        page = job.pages[page_num]
        for k, v in kwargs.items():
            if hasattr(page, k):
                setattr(page, k, v)
        # Persist to SQLite
        save_page_result(job_id, page)
        await self.broadcast(job_id, {
            "type": "page_update",
            "page": page.to_dict(),
            "summary": job._summary(),
        })

    async def update_job_status(self, job_id: str, status: JobStatus):
        job = self._jobs.get(job_id)
        if not job:
            return
        job.status = status
        if status == JobStatus.PROCESSING and not job.started_at:
            job.started_at = time.time()
        if status in (JobStatus.COMPLETED, JobStatus.FAILED):
            job.completed_at = time.time()
        # Persist to SQLite
        save_job(job)
        await self.broadcast(job_id, {
            "type": "job_update",
            "status": status.value,
            "summary": job._summary(),
            "elapsed_time": round((job.completed_at or time.time()) - (job.started_at or time.time()), 2),
        })


job_manager = JobManager()
