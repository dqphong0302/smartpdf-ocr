"""OCR Engine — Tesseract and Vision AI OCR with smart routing."""
import asyncio
import base64
import io
import os
import time

import httpx
import pytesseract
from PIL import Image
from dotenv import load_dotenv

load_dotenv()

# Global Semaphores for Concurrency Control
_semaphores = {}

def get_vision_semaphore():
    if "vision" not in _semaphores:
        parallel = int(os.getenv("PARALLEL_BATCHES", "4"))
        _semaphores["vision"] = asyncio.Semaphore(parallel)
    return _semaphores["vision"]

def get_tesseract_semaphore():
    if "tesseract" not in _semaphores:
        workers = int(os.getenv("MAX_TESSERACT_WORKERS", "2"))
        _semaphores["tesseract"] = asyncio.Semaphore(workers)
    return _semaphores["tesseract"]

CONFIDENCE_THRESHOLD = int(os.getenv("CONFIDENCE_THRESHOLD", "80"))
NINE_ROUTER_URL = os.getenv("NINE_ROUTER_URL", "http://10.10.10.100:8317/v1")
NINE_ROUTER_API_KEY = os.getenv("NINE_ROUTER_API_KEY", "")
VISION_MODEL = os.getenv("VISION_MODEL", "gpt-5.4-mini")
TESSERACT_LANG = os.getenv("TESSERACT_LANG", "eng+vie")


def _hocr_to_styled_html(hocr: str) -> str:
    """Convert hOCR output to a cleaner styled HTML for preview."""
    # Wrap in a styled container
    return f"""<div style="font-family: serif; line-height: 1.6; padding: 10px;">{hocr}</div>"""


def tesseract_ocr(image: Image.Image, lang: str = None) -> dict:
    """Run Tesseract OCR on an image. Returns text + html_text + confidence."""
    lang = lang or TESSERACT_LANG
    start = time.time()

    # Get text with confidence data
    data = pytesseract.image_to_data(image, lang=lang, output_type=pytesseract.Output.DICT)

    # Calculate average confidence (excluding -1 which means no text)
    confidences = [c for c in data["conf"] if c > 0]
    avg_confidence = sum(confidences) / len(confidences) if confidences else 0

    # Get full text
    text = pytesseract.image_to_string(image, lang=lang).strip()

    # Get hOCR (HTML) output for layout-preserving render
    try:
        hocr_bytes = pytesseract.image_to_pdf_or_hocr(image, lang=lang, extension='hocr')
        html_text = hocr_bytes.decode('utf-8', errors='replace')
    except Exception:
        html_text = f"<pre>{text}</pre>"

    elapsed = time.time() - start
    return {
        "text": text,
        "html_text": html_text,
        "confidence": round(avg_confidence, 1),
        "time_taken": round(elapsed, 2),
        "method": "tesseract",
        "word_count": len([w for w in data["text"] if w.strip()]),
    }


async def vision_ocr(image: Image.Image, prompt: str = None) -> dict:
    """Run Vision AI OCR via 9router.
    
    HTML-first approach: one API call for HTML, derive plain text from it.
    """
    async with get_vision_semaphore():
        start = time.time()

    # Convert image to base64
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    img_b64 = base64.b64encode(buffer.getvalue()).decode()

    html_prompt = (
        "Extract ALL text from this image and output as clean HTML that preserves "
        "the original layout and formatting as closely as possible. "
        "Use appropriate HTML tags: <h1>-<h6> for headings, <p> for paragraphs, "
        "<table> with <thead>/<tbody>/<tr>/<th>/<td> for tables, "
        "<ul>/<ol> for lists, <strong>/<em> for emphasis. "
        "Preserve line breaks and spacing. "
        "Do NOT wrap the output in ```html code blocks. "
        "Output ONLY the raw HTML content, nothing else."
    )

    payload = {
        "model": VISION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": html_prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{img_b64}", "detail": "high"},
                    },
                ],
            }
        ],
        "max_tokens": 4096,
        "temperature": 0,
        "stream": False,
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{NINE_ROUTER_URL}/chat/completions",
            json=payload,
            headers={
                "Authorization": f"Bearer {NINE_ROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    html_text = data["choices"][0]["message"]["content"].strip()
    # Strip markdown code block wrappers if present
    html_text = _strip_code_block(html_text)
    
    # Derive plain text from HTML
    text = _html_to_text(html_text)

    elapsed = time.time() - start
    usage = data.get("usage", {})
    return {
        "text": text,
        "html_text": html_text,
        "confidence": 95.0,
        "time_taken": round(elapsed, 2),
        "method": "vision",
        "tokens_used": usage.get("total_tokens", 0),
    }


def _strip_code_block(text: str) -> str:
    """Strip ```html ... ``` wrapper if the model returns one."""
    stripped = text.strip()
    if stripped.startswith("```"):
        # Remove first line (```html or ```)
        lines = stripped.split("\n", 1)
        if len(lines) > 1:
            stripped = lines[1]
        if stripped.endswith("```"):
            stripped = stripped[:-3].rstrip()
    return stripped


def _html_to_text(html: str) -> str:
    """Convert HTML to readable plain text, extracting tables as simple text."""
    import re
    from html import unescape

    text = html
    # Replace <br> with newlines
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    # Replace block-level closing tags with newlines
    text = re.sub(r'</(?:p|div|h[1-6]|li|tr|thead|tbody)>', '\n', text, flags=re.IGNORECASE)
    # Replace <td>/<th> with tab separator
    text = re.sub(r'<(?:td|th)[^>]*>', '\t', text, flags=re.IGNORECASE)
    text = re.sub(r'</(?:td|th)>', '', text, flags=re.IGNORECASE)
    # Remove all remaining HTML tags
    text = re.sub(r'<[^>]+>', '', text)
    # Unescape HTML entities
    text = unescape(text)
    # Clean up excessive whitespace
    lines = [line.strip() for line in text.split('\n')]
    text = '\n'.join(line for line in lines if line)
    return text.strip()


async def vision_ocr_batch(images: list[tuple[int, Image.Image]]) -> list[dict]:
    """Batch Vision AI OCR: send multiple page images in ONE API call.
    
    HTML-first approach: requests HTML output, derives text from it.
    
    Args:
        images: list of (page_num, PIL Image) tuples (max 4 recommended)
    
    Returns:
        list of result dicts, one per page, in input order
    """
    if len(images) == 1:
        result = await vision_ocr(images[0][1])
        return [result]

    async with get_vision_semaphore():
        start = time.time()

    # Build multi-image content with HTML output instruction
    content_parts = [
        {
            "type": "text",
            "text": (
                f"I'm sending you {len(images)} document page images. "
                f"Extract ALL text from EACH page and output as clean HTML. "
                f"Use appropriate HTML tags: <h1>-<h6> for headings, <p> for paragraphs, "
                f"<table> with <thead>/<tbody>/<tr>/<th>/<td> for tables, "
                f"<ul>/<ol> for lists, <strong>/<em> for emphasis. "
                f"Separate each page's HTML output with the marker: ===PAGE_BREAK=== "
                f"Output the pages in order. "
                f"Do NOT wrap output in ```html code blocks. "
                f"Output ONLY the raw HTML content."
            ),
        }
    ]

    for page_num, image in images:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        img_b64 = base64.b64encode(buffer.getvalue()).decode()
        content_parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{img_b64}", "detail": "high"},
        })

    payload = {
        "model": VISION_MODEL,
        "messages": [{"role": "user", "content": content_parts}],
        "max_tokens": 24576,
        "temperature": 0,
        "stream": False,
    }

    try:
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(
                f"{NINE_ROUTER_URL}/chat/completions",
                json=payload,
                headers={
                    "Authorization": f"Bearer {NINE_ROUTER_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        full_output = data["choices"][0]["message"]["content"].strip()
        elapsed = time.time() - start

        # Split by page break marker
        parts = full_output.split("===PAGE_BREAK===")
        parts = [_strip_code_block(p.strip()) for p in parts if p.strip()]

        # If splitting didn't produce the right count, fallback
        if len(parts) != len(images):
            results = []
            for _, image in images:
                r = await vision_ocr(image)
                results.append(r)
            return results

        results = []
        per_page_time = round(elapsed / len(images), 2)
        for html_text in parts:
            text = _html_to_text(html_text)
            results.append({
                "text": text,
                "html_text": html_text,
                "confidence": 95.0,
                "time_taken": per_page_time,
                "method": "vision",
                "tokens_used": data.get("usage", {}).get("total_tokens", 0) // len(images),
            })
        return results

    except Exception:
        # Fallback: process individually
        results = []
        for _, image in images:
            r = await vision_ocr(image)
            results.append(r)
        return results


async def smart_ocr(
    image: Image.Image,
    classification: str,
    force_method: str = None,
) -> dict:
    """Smart OCR: auto-select engine based on classification.
    
    Args:
        image: PIL Image of the page
        classification: 'digital', 'scan_simple', 'scan_complex'
        force_method: Override auto-selection ('tesseract' or 'vision')
    """
    if force_method == "vision":
        return await vision_ocr(image)
    if force_method == "tesseract":
        async with get_tesseract_semaphore():
            return await asyncio.to_thread(tesseract_ocr, image)

    # Auto-select based on classification
    if classification == "scan_complex":
        # Complex scans go directly to Vision AI
        return await vision_ocr(image)

    # Try Tesseract first for simple scans
    async with get_tesseract_semaphore():
        result = await asyncio.to_thread(tesseract_ocr, image)

    if result["confidence"] >= CONFIDENCE_THRESHOLD:
        return result

    # Confidence too low — fallback to Vision AI
    vision_result = await vision_ocr(image)
    vision_result["fallback_from"] = "tesseract"
    vision_result["tesseract_confidence"] = result["confidence"]
    return vision_result
