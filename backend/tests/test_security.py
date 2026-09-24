import hashlib
import io
import os
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ["SMART_PDF_ADMIN_USER"] = "test-admin"
os.environ["SMART_PDF_ADMIN_PASSWORD"] = "test-password-long"  # pragma: allowlist secret
os.environ["OCR_API_KEY"] = "test-api-key-long"  # pragma: allowlist secret
os.environ["OPENAI_BASE_URL"] = "https://openai.example.test/v1"
os.environ["OPENAI_API_KEY"] = "test-openai-key-long"  # pragma: allowlist secret
os.environ["CORS_ORIGINS"] = "https://smartpdf.example.test"
os.environ["COOKIE_SECURE"] = "true"
os.environ["ENABLE_API_DOCS"] = "false"
os.environ["LATEX_COMPILE_ENABLED"] = "false"

import auth
import database
import main
from latex_compiler import LatexCompileError, _safe_extract_target, prepare_latex_workspace
from ocr_engine import sanitize_ocr_html


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "data" / "test.db"
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(main, "UPLOAD_DIR", upload_dir)
    monkeypatch.setattr(main, "OCR_API_KEY", "test-api-key-long")
    main.job_manager._jobs.clear()
    main.job_manager._websockets.clear()
    main._api_jobs.clear()
    main._batch_jobs.clear()
    database.init_db()
    auth.init_auth_tables()
    auth.seed_default_user("test-admin", "test-password-long")
    with TestClient(main.app, base_url="https://smartpdf.example.test") as test_client:
        yield test_client


def test_public_health_is_minimal_and_hardened(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert set(response.json()) == {"status", "service", "version"}
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert client.get("/docs").status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/api/jobs",
        "/api/jobs/not-a-job",
        "/api/thumbnail/not-a-job/1",
        "/api/extracted-images/not-a-job/file.png",
        "/api/download/not-a-job",
    ],
)
def test_job_content_requires_login(client, path):
    assert client.get(path).status_code == 401


def test_login_uses_secure_cookie_and_unlocks_jobs(client):
    response = client.post(
        "/api/auth/login",
        json={"username": "test-admin", "password": "test-password-long"},  # pragma: allowlist secret
    )
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "secure" in cookie
    assert "samesite=lax" in cookie
    assert client.get("/api/jobs").status_code == 200


def test_programmatic_routes_require_api_key(client):
    assert client.post(
        "/api/v1/ocr",
        files={"file": ("test.pdf", b"not-a-pdf", "application/pdf")},
    ).status_code == 401
    assert client.get("/api/health/details").status_code == 401
    response = client.get(
        "/api/health/details", headers={"X-API-Key": "test-api-key-long"}
    )
    assert response.status_code == 200
    assert response.json()["checks"]["vision"]["ok"] is True


def test_api_result_survives_memory_cache_loss(client):
    now = main.time.time()
    database.save_api_job(
        "durable-job",
        {
            "filename": "durable.pdf",
            "filepath": "",
            "status": "completed",
            "total_pages": 1,
            "completed_pages": 1,
            "selected_pages": [1],
            "method": "digital",
            "pages": {
                "1": {
                    "text": "durable text",
                    "html_text": "<p>durable text</p>",
                    "method": "digital",
                    "confidence": 100,
                }
            },
            "html_result": "<html><body>durable text</body></html>",
            "created_at": now,
            "started_at": now,
            "completed_at": now,
            "expires_at": now + 3600,
            "error": None,
        },
    )
    main._api_jobs.clear()

    response = client.get(
        "/api/v1/ocr/durable-job",
        headers={"X-API-Key": "test-api-key-long"},
    )
    assert response.status_code == 200
    assert "durable text" in response.text


def test_restart_reconciliation_and_active_elapsed(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "recovery.db")
    database.init_db()
    conn = database._get_conn()
    now = main.time.time()
    conn.execute(
        """
        INSERT INTO jobs (
            job_id, filename, filepath, status, total_pages, selected_pages,
            created_at, started_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("ui-active", "active.pdf", "", "processing", 1, "[1]", now - 10, now - 5),
    )
    conn.execute(
        """
        INSERT INTO api_jobs (
            job_id, filename, status, total_pages, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        ("api-active", "active.pdf", "processing", 1, now),
    )
    conn.commit()
    conn.close()

    active = database.list_jobs_from_db()[0]
    assert active["elapsed_time"] >= 0
    recovered = database.reconcile_interrupted_jobs("restart test")
    assert recovered == {"ui": 1, "api": 1, "batch": 0}
    assert database.load_job_dict("ui-active")["status"] == "interrupted"
    assert database.load_api_job("api-active")["status"] == "interrupted"


def test_latex_is_disabled_by_default(client):
    response = client.post(
        "/api/v1/latex/compile",
        headers={"X-API-Key": "test-api-key-long"},
        files={"file": ("main.tex", b"hello", "text/plain")},
    )
    assert response.status_code == 503


def test_filename_and_html_sanitization():
    assert main.safe_upload_name("../../report.pdf", {".pdf"}) == "report.pdf"
    with pytest.raises(main.HTTPException):
        main.safe_upload_name("report.exe", {".pdf"})
    cleaned = sanitize_ocr_html('<p onclick="x()">safe</p><script>alert(1)</script>')
    assert "onclick" not in cleaned
    assert "<script" not in cleaned
    assert "safe" in cleaned


def test_argon2_and_legacy_password_verification():
    encoded = auth._hash_password("strong-password")
    assert encoded.startswith("$argon2id$")
    assert auth._verify_password("strong-password", encoded)
    salt = "legacy-salt"
    legacy = f"{salt}:{hashlib.sha256(f'{salt}:strong-password'.encode()).hexdigest()}"
    assert auth._verify_password("strong-password", legacy)
    assert not auth._verify_password("wrong", legacy)


def test_zip_traversal_is_rejected(tmp_path):
    with pytest.raises(LatexCompileError):
        _safe_extract_target(tmp_path, "../outside.tex")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../outside.tex", "bad")
    with pytest.raises(LatexCompileError):
        prepare_latex_workspace(tmp_path / "job", "project.zip", buffer.getvalue(), "main.tex")
