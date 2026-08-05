"""PDF Analyzer — Detect digital vs scanned pages, classify complexity."""
import fitz  # PyMuPDF
from dataclasses import dataclass
import logging

logger = logging.getLogger("uvicorn")

try:
    import pdf_inspector
    HAS_PDF_INSPECTOR = True
except ImportError:
    HAS_PDF_INSPECTOR = False
    pdf_inspector = None


@dataclass
class PageAnalysis:
    page_num: int
    width: float
    height: float
    has_text: bool
    text_length: int
    has_images: bool
    image_count: int
    image_coverage: float  # % of page covered by images
    classification: str  # digital, scan_simple, scan_complex
    text_preview: str = ""

    def to_dict(self):
        return {
            "page_num": self.page_num,
            "width": round(self.width),
            "height": round(self.height),
            "has_text": self.has_text,
            "text_length": self.text_length,
            "has_images": self.has_images,
            "image_count": self.image_count,
            "image_coverage": round(self.image_coverage, 1),
            "classification": self.classification,
            "text_preview": self.text_preview[:200] if self.text_preview else "",
        }


@dataclass
class PdfAnalysis:
    total_pages: int
    file_size_mb: float
    pages: list  # list of PageAnalysis
    summary: dict  # counts per classification

    def to_dict(self):
        return {
            "total_pages": self.total_pages,
            "file_size_mb": round(self.file_size_mb, 2),
            "pages": [p.to_dict() for p in self.pages],
            "summary": self.summary,
        }


def analyze_pdf(filepath: str) -> PdfAnalysis:
    """Analyze PDF: detect digital vs scanned pages, classify complexity."""
    import os

    doc = fitz.open(filepath)
    file_size_mb = os.path.getsize(filepath) / (1024 * 1024)
    pages = []
    summary = {"digital": 0, "scan_simple": 0, "scan_complex": 0}

    # Pre-inspect with pdf-inspector if available
    pages_needing_ocr = None
    pdf_type = None
    if HAS_PDF_INSPECTOR:
        try:
            inspector_res = pdf_inspector.classify_pdf(filepath)
            if inspector_res:
                pages_needing_ocr = set(getattr(inspector_res, "pages_needing_ocr", []))
                pdf_type = getattr(inspector_res, "pdf_type", None)
                logger.info(f"pdf-inspector classified {filepath}: type={pdf_type}, ocr_pages={pages_needing_ocr}")
        except Exception as e:
            logger.debug(f"pdf-inspector classification skipped/failed: {e}")

    for page_num in range(len(doc)):
        page = doc[page_num]
        rect = page.rect

        # Extract text
        text = page.get_text("text").strip()
        text_length = len(text)
        has_text = text_length > 50  # more than 50 chars = meaningful text

        # Count images
        image_list = page.get_images(full=True)
        image_count = len(image_list)
        has_images = image_count > 0

        # Calculate image coverage
        image_coverage = 0.0
        if has_images:
            page_area = rect.width * rect.height
            total_image_area = 0
            for img in image_list:
                xref = img[0]
                try:
                    img_rects = page.get_image_rects(xref)
                    for img_rect in img_rects:
                        total_image_area += img_rect.width * img_rect.height
                except Exception:
                    pass
            if page_area > 0:
                image_coverage = min(100, (total_image_area / page_area) * 100)

        # Classify page (Combining pdf-inspector classification with PyMuPDF metrics)
        if pages_needing_ocr is not None and page_num in pages_needing_ocr:
            # pdf-inspector marked this page as requiring OCR
            if image_coverage > 50 or image_count > 2:
                classification = "scan_complex"
            else:
                classification = "scan_simple"
        elif pdf_type == "text_based" and has_text:
            classification = "digital"
        elif has_text and text_length > 200:
            classification = "digital"
        elif has_images and image_coverage > 70:
            # Mostly image — likely a scan
            if image_count <= 2 and text_length < 50:
                classification = "scan_simple"
            else:
                classification = "scan_complex"
        elif has_images and not has_text:
            classification = "scan_simple"
        elif has_text and has_images and image_coverage > 30:
            classification = "scan_complex"
        else:
            classification = "digital" if has_text else "scan_simple"

        summary[classification] = summary.get(classification, 0) + 1

        pages.append(PageAnalysis(
            page_num=page_num + 1,  # 1-indexed
            width=rect.width,
            height=rect.height,
            has_text=has_text,
            text_length=text_length,
            has_images=has_images,
            image_count=image_count,
            image_coverage=image_coverage,
            classification=classification,
            text_preview=text[:200] if text else "",
        ))

    doc.close()
    return PdfAnalysis(
        total_pages=len(pages),
        file_size_mb=file_size_mb,
        pages=pages,
        summary=summary,
    )


def render_page_to_image(filepath: str, page_num: int, dpi: int = 200):
    """Render a PDF page to PIL Image for OCR."""
    from PIL import Image
    import io

    doc = fitz.open(filepath)
    page = doc[page_num - 1]  # 0-indexed
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat)
    img_data = pix.tobytes("png")
    doc.close()
    return Image.open(io.BytesIO(img_data))


def extract_page_text(filepath: str, page_num: int) -> str:
    """Extract text from a digital PDF page via PyMuPDF.

    Note: pdf-inspector's extract_text() operates on the full document and is
    not suitable for per-page extraction. We use PyMuPDF here (which is already
    fast and accurate for digital text). pdf-inspector is used upstream in
    analyze_pdf() as a per-document classifier only.
    """
    doc = fitz.open(filepath)
    page = doc[page_num - 1]
    text = page.get_text("text").strip()
    doc.close()
    return text


def extract_page_markdown(filepath: str, page_num: int) -> str | None:
    """Extract layout-aware Markdown from a digital PDF page using pdf-inspector.

    Uses pdf-inspector's per-page extraction which preserves:
    - Table structure (renders as Markdown tables)
    - Multi-column reading order
    - Header / paragraph hierarchy

    Returns:
        Markdown string if the page is text-based (no OCR needed).
        None if pdf-inspector is unavailable OR the page is scanned (needs OCR).
        Caller should fall back to extract_page_text() / OCR when None is returned.
    """
    if not HAS_PDF_INSPECTOR:
        return None
    try:
        # extract_pages_markdown accepts 0-indexed page list
        res = pdf_inspector.extract_pages_markdown(filepath, pages=[page_num - 1])
        if not res or not res.pages:
            return None
        pg = res.pages[0]
        if pg.needs_ocr:
            # pdf-inspector flagged this page as requiring real OCR — signal caller
            return None
        md = pg.markdown.strip() if pg.markdown else ""
        return md if md else None
    except Exception as e:
        logger.debug(f"pdf-inspector extract_page_markdown failed (page {page_num}): {e}")
        return None


def get_page_thumbnail(filepath: str, page_num: int, width: int = 200) -> bytes:
    """Get a thumbnail of a PDF page as PNG bytes."""
    doc = fitz.open(filepath)
    page = doc[page_num - 1]
    # Scale to desired width
    scale = width / page.rect.width
    mat = fitz.Matrix(scale, scale)
    pix = page.get_pixmap(matrix=mat)
    png_bytes = pix.tobytes("png")
    doc.close()
    return png_bytes


def extract_page_images(filepath: str, page_num: int, output_dir: str) -> list[str]:
    """Extract all images from a specific page and save them to output_dir.
    
    Filters out tiny images (width/height < 30px) to avoid cluttering with icons.
    Returns a list of saved filenames.
    """
    import os
    from pathlib import Path
    
    doc = fitz.open(filepath)
    page = doc[page_num - 1]  # page_num is 1-indexed
    image_list = page.get_images(full=True)
    
    saved_files = []
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    
    seen_xrefs = set()
    img_idx = 1
    
    for img_info in image_list:
        xref = img_info[0]
        width = img_info[2]
        height = img_info[3]
        
        # Filter out tiny icon-like images
        if width < 30 or height < 30:
            continue
            
        if xref in seen_xrefs:
            continue
        seen_xrefs.add(xref)
        
        try:
            base_image = doc.extract_image(xref)
            image_bytes = base_image["image"]
            image_ext = base_image["ext"]  # e.g., 'png', 'jpeg'
            
            filename = f"page_{page_num}_img_{img_idx}.{image_ext}"
            file_filepath = out_path / filename
            
            with open(file_filepath, "wb") as f:
                f.write(image_bytes)
                
            saved_files.append(filename)
            img_idx += 1
        except Exception as e:
            logger.error(f"Failed to extract image xref {xref} on page {page_num}: {e}")
            
    doc.close()
    return saved_files

