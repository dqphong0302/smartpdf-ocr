"""LaTeX compilation helpers for Smart PDF."""

import asyncio
import os
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path


ALLOWED_INPUT_EXTENSIONS = {".tex", ".zip"}
ALLOWED_ENGINES = {"latexmk", "pdflatex", "xelatex", "lualatex"}
DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 300
MAX_EXTRACTED_BYTES = 100 * 1024 * 1024
MAX_ZIP_MEMBERS = 500
LOG_TAIL_CHARS = 8000


@dataclass
class LatexCompileResult:
    pdf_path: Path
    download_name: str
    engine: str
    log_tail: str


class LatexCompileError(Exception):
    """Raised when LaTeX input validation or compilation fails."""

    def __init__(self, message: str, *, engine: str | None = None, exit_code: int | None = None, log_tail: str = ""):
        super().__init__(message)
        self.message = message
        self.engine = engine
        self.exit_code = exit_code
        self.log_tail = log_tail

    def to_detail(self) -> dict:
        return {
            "message": self.message,
            "engine": self.engine,
            "exit_code": self.exit_code,
            "log_tail": self.log_tail,
        }


def clamp_timeout(timeout: int | None) -> int:
    if timeout is None:
        return DEFAULT_TIMEOUT
    return max(1, min(int(timeout), MAX_TIMEOUT))


def get_latex_health() -> dict:
    """Return availability of common LaTeX compilation binaries."""
    binaries = {
        "latexmk": shutil.which("latexmk"),
        "pdflatex": shutil.which("pdflatex"),
        "xelatex": shutil.which("xelatex"),
        "lualatex": shutil.which("lualatex"),
        "biber": shutil.which("biber"),
    }
    return {
        "ok": bool(binaries["latexmk"] or binaries["pdflatex"]),
        **binaries,
    }


def prepare_latex_workspace(job_dir: Path, filename: str, content: bytes, main_file: str | None) -> Path:
    """Write uploaded LaTeX content into a sandboxed job directory."""
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_INPUT_EXTENSIONS:
        raise LatexCompileError("Only .tex and .zip files are accepted")

    job_dir.mkdir(parents=True, exist_ok=True)

    if suffix == ".tex":
        safe_name = Path(filename).name or "main.tex"
        tex_path = job_dir / safe_name
        tex_path.write_bytes(content)
        return tex_path

    return _extract_zip_project(job_dir, content, main_file or "main.tex")


def _extract_zip_project(job_dir: Path, content: bytes, main_file: str) -> Path:
    zip_path = job_dir / "source.zip"
    zip_path.write_bytes(content)

    total_bytes = 0
    with zipfile.ZipFile(zip_path) as zf:
        members = zf.infolist()
        if len(members) > MAX_ZIP_MEMBERS:
            raise LatexCompileError(f"ZIP has too many files. Max {MAX_ZIP_MEMBERS}")

        for member in members:
            if member.is_dir():
                continue
            target = _safe_extract_target(job_dir, member.filename)
            total_bytes += member.file_size
            if total_bytes > MAX_EXTRACTED_BYTES:
                raise LatexCompileError("ZIP content too large after extraction")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)

    entrypoint = _safe_extract_target(job_dir, main_file)
    if not entrypoint.exists() or entrypoint.suffix.lower() != ".tex":
        raise LatexCompileError(f"Main LaTeX file not found: {main_file}")
    return entrypoint


def _safe_extract_target(root: Path, member_name: str) -> Path:
    if not member_name or member_name.startswith(("/", "\\")):
        raise LatexCompileError("ZIP contains an unsafe absolute path")
    target = (root / member_name).resolve()
    root_resolved = root.resolve()
    if os.path.commonpath([str(root_resolved), str(target)]) != str(root_resolved):
        raise LatexCompileError("ZIP contains an unsafe relative path")
    return target


async def compile_latex_project(
    *,
    job_dir: Path,
    main_tex: Path,
    requested_engine: str = "latexmk",
    timeout: int | None = None,
) -> LatexCompileResult:
    """Compile a prepared LaTeX project and return the generated PDF path."""
    engine = (requested_engine or "latexmk").lower()
    if engine not in ALLOWED_ENGINES:
        raise LatexCompileError(f"Unsupported LaTeX engine: {requested_engine}", engine=engine)

    timeout_seconds = clamp_timeout(timeout)
    selected_engine = _select_engine(engine)
    commands = _build_commands(selected_engine, main_tex.name)

    combined_output = []
    exit_code = None
    try:
        for command in commands:
            proc = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(main_tex.parent),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
            text = stdout.decode("utf-8", errors="replace")
            combined_output.append(f"$ {' '.join(command)}\n{text}")
            exit_code = proc.returncode
            if proc.returncode != 0:
                raise LatexCompileError(
                    "LaTeX compilation failed",
                    engine=selected_engine,
                    exit_code=proc.returncode,
                    log_tail=_collect_log_tail(main_tex, combined_output),
                )
    except asyncio.TimeoutError as exc:
        raise LatexCompileError(
            f"LaTeX compilation timed out after {timeout_seconds}s",
            engine=selected_engine,
            exit_code=exit_code,
            log_tail=_collect_log_tail(main_tex, combined_output),
        ) from exc

    pdf_path = main_tex.with_suffix(".pdf")
    if not pdf_path.exists():
        raise LatexCompileError(
            "LaTeX completed but no PDF was produced",
            engine=selected_engine,
            exit_code=exit_code,
            log_tail=_collect_log_tail(main_tex, combined_output),
        )

    return LatexCompileResult(
        pdf_path=pdf_path,
        download_name=f"{main_tex.stem}.pdf",
        engine=selected_engine,
        log_tail=_collect_log_tail(main_tex, combined_output),
    )


def _select_engine(engine: str) -> str:
    if shutil.which(engine):
        return engine
    if engine == "latexmk" and shutil.which("pdflatex"):
        return "pdflatex"
    raise LatexCompileError(f"LaTeX engine is not installed: {engine}", engine=engine)


def _build_commands(engine: str, main_name: str) -> list[list[str]]:
    if engine == "latexmk":
        return [[
            "latexmk",
            "-pdf",
            "-interaction=nonstopmode",
            "-halt-on-error",
            "-no-shell-escape",
            main_name,
        ]]
    base = [engine, "-interaction=nonstopmode", "-halt-on-error", "-no-shell-escape", main_name]
    return [base, base]


def _collect_log_tail(main_tex: Path, command_outputs: list[str]) -> str:
    log_path = main_tex.with_suffix(".log")
    parts = []
    if log_path.exists():
        parts.append(log_path.read_text(encoding="utf-8", errors="replace"))
    parts.extend(command_outputs)
    return "\n".join(parts)[-LOG_TAIL_CHARS:]
