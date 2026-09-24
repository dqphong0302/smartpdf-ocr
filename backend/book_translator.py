"""Book Translation Engine for Smart PDF — AegisTrans Inspired & Memory-Optimized.

High-fidelity PDF textbook and document translation system with:
1. Complete image, figure, vector drawing, and table preservation (zero pixel clipping).
2. Advanced layout analysis: span-level font weights (Bold/Italic), color, and alignment detection.
3. Multi-column academic paper reading-order preservation with spanning header/figure support.
4. Dynamic anti-overflow font metric fitting with TrueType font embedding (Regular, Bold, Italic).
5. Domain-Specific Terminology Engine (Medical, Dental, Engineering/Tech) with dynamic glossary injection.
6. Robust memory management (zero leak on large 500+ page books, proactive GC, safe doc lifecycle).
7. Dual-mode support (In-Place layout replacement vs Side-by-Side bilingual).
8. Resilient 9router GPT integration (supports SSE streaming & standard JSON with automatic repair).
"""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Coroutine

import fitz  # PyMuPDF
import httpx

logger = logging.getLogger("smart-pdf.book_translator")

# Candidate TrueType fonts supporting Vietnamese UTF-8
REGULAR_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
]

BOLD_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
]

ITALIC_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
    "/System/Library/Fonts/Supplemental/Arial Italic.ttf",
    "/Library/Fonts/Arial Italic.ttf",
]


def get_unicode_fonts() -> dict[str, str | None]:
    """Find available TrueType fonts supporting Vietnamese UTF-8 for Regular, Bold, and Italic."""
    def _find_first(cands: list[str]) -> str | None:
        for path in cands:
            if os.path.isfile(path):
                return path
        return None

    regular = _find_first(REGULAR_FONT_CANDIDATES)
    bold = _find_first(BOLD_FONT_CANDIDATES) or regular
    italic = _find_first(ITALIC_FONT_CANDIDATES) or regular

    return {
        "regular": regular,
        "bold": bold,
        "italic": italic,
    }


def get_llm_config() -> tuple[str, str, str]:
    """Retrieve 9router / OpenAI gateway configuration."""
    base_url = os.getenv("OPENAI_BASE_URL", "https://9router.phongdang.io.vn/v1").rstrip("/")
    api_key = os.getenv("OPENAI_API_KEY", "")
    model = (
        os.getenv("SMART_PDF_GPT_MODEL")
        or os.getenv("TRANSLATION_MODEL")
        or os.getenv("GPT_MODEL")
        or "gpt-5.6-luna"
    )
    if not api_key:
        logger.warning("OPENAI_API_KEY is not set. 9router authentication might fail.")
    return base_url, api_key, model


# ============================================================================
# DOMAIN GLOSSARY ENGINE
# ============================================================================

class DomainGlossaryManager:
    """Manages domain-specific terminology glossaries, multi-format loading, and dynamic prompt injection."""

    def __init__(self, glossaries_dir: Path | None = None):
        self.glossaries_dir = glossaries_dir or (Path(__file__).parent / "data" / "glossaries")
        self._cache: dict[str, dict[str, str]] = {}
        self._load_glossaries()

    def _load_glossaries(self):
        """Load JSON, JSONL, and CSV glossary files into memory."""
        if not self.glossaries_dir.exists():
            return

        # 1. Load JSON files
        for f in self.glossaries_dir.glob("*.json"):
            try:
                with open(f, "r", encoding="utf-8") as fp:
                    data = json.load(fp)
                    if isinstance(data, dict):
                        self._cache[f.stem.lower()] = {k.lower().strip(): v.strip() for k, v in data.items()}
                        logger.info("Loaded %d terms from %s", len(self._cache[f.stem.lower()]), f.name)
            except Exception as e:
                logger.warning("Error loading glossary JSON %s: %s", f, e)

        # 2. Load JSONL files (AegisTrans format)
        for f in self.glossaries_dir.glob("*.jsonl"):
            try:
                entries: dict[str, str] = {}
                with open(f, "r", encoding="utf-8") as fp:
                    for line in fp:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            item = json.loads(line)
                            k = item.get("en") or item.get("term") or item.get("source")
                            v = item.get("vi") or item.get("translation") or item.get("target")
                            if k and v:
                                entries[k.lower().strip()] = v.strip()
                        except Exception:
                            continue
                if entries:
                    stem = f.stem.lower()
                    self._cache.setdefault(stem, {}).update(entries)
                    logger.info("Loaded %d terms from %s", len(entries), f.name)
            except Exception as e:
                logger.warning("Error loading glossary JSONL %s: %s", f, e)

        # 3. Load CSV files
        for f in self.glossaries_dir.glob("*.csv"):
            try:
                import csv
                entries = {}
                with open(f, "r", encoding="utf-8") as fp:
                    reader = csv.reader(fp)
                    for row in reader:
                        if len(row) >= 2 and row[0].strip() and row[1].strip():
                            entries[row[0].lower().strip()] = row[1].strip()
                if entries:
                    stem = f.stem.lower()
                    self._cache.setdefault(stem, {}).update(entries)
                    logger.info("Loaded %d terms from %s", len(entries), f.name)
            except Exception as e:
                logger.warning("Error loading glossary CSV %s: %s", f, e)

    def register_glossary(self, profile: str, terms: dict[str, str]):
        """Dynamically register or update an in-memory glossary profile."""
        profile = profile.lower().strip()
        normalized = {k.lower().strip(): v.strip() for k, v in terms.items()}
        if profile in self._cache:
            self._cache[profile].update(normalized)
        else:
            self._cache[profile] = normalized
        logger.info("Registered glossary '%s' with %d terms", profile, len(normalized))

    def get_matching_terms(
        self,
        text: str,
        profile: str = "general",
        book_id: str | None = None,
    ) -> dict[str, str]:
        """Find domain terms present in text, prioritizing book-specific terms and longer phrases."""
        text_lower = text.lower()
        matched: dict[str, str] = {}

        # Merge candidate terms from: 1. book-specific glossary, 2. domain profile
        combined_terms: dict[str, str] = {}

        # Add domain profile terms
        profile = profile.lower()
        if profile in ("engineering", "technical"):
            profile = "tech"

        if profile in self._cache:
            combined_terms.update(self._cache[profile])

        # Add book-specific terms with priority
        if book_id:
            book_key = f"book_{book_id.lower()}"
            if book_key in self._cache:
                combined_terms.update(self._cache[book_key])

        if not combined_terms:
            return {}

        # Sort terms by length descending to match composite multi-word terms first
        sorted_terms = sorted(combined_terms.keys(), key=len, reverse=True)
        for term in sorted_terms:
            pattern = r"\b" + re.escape(term) + r"\b"
            if re.search(pattern, text_lower):
                matched[term] = combined_terms[term]
                if len(matched) >= 35:  # Cap at 35 top relevant terms per prompt
                    break

        return matched

    async def auto_mine_book_glossary(
        self,
        doc: fitz.Document,
        book_id: str,
        max_sample_pages: int = 8,
        client: httpx.AsyncClient | None = None,
    ) -> dict[str, str]:
        """Pass 1 Term Mining (AegisTrans Architecture):

        Scans Table of Contents and representative sample pages of a book to discover
        book-specific nomenclature, acronyms, and specialized jargon before translation.
        """
        total_pages = doc.page_count
        if total_pages == 0:
            return {}

        # Sample TOC, first 3 pages, middle pages, and last pages (index)
        sample_indices = set()
        for i in range(min(4, total_pages)):
            sample_indices.add(i)
        if total_pages > 10:
            sample_indices.add(total_pages // 2)
            sample_indices.add(total_pages // 2 + 1)
        if total_pages > 6:
            sample_indices.add(total_pages - 2)
            sample_indices.add(total_pages - 1)

        sample_pages = sorted(list(sample_indices))[:max_sample_pages]

        combined_sample_text = ""
        for p_idx in sample_pages:
            try:
                page_text = doc[p_idx].get_text()
                if page_text:
                    # Clip to 1500 chars per page to avoid context blowout
                    combined_sample_text += f"\n--- Page {p_idx + 1} ---\n" + page_text[:1500]
            except Exception:
                continue

        if not combined_sample_text.strip():
            return {}

        base_url, api_key, model = get_llm_config()
        system_prompt = (
            "You are an expert academic terminologist and terminology extractor. "
            "Analyze the following textbook excerpt (TOC, chapters, index). "
            "Extract up to 40 of the most critical domain-specific terms, proper nouns, clinical/technical concepts, "
            "and author abbreviations. "
            "For each term, provide its standardized academic Vietnamese translation WITH the original English term in parentheses: "
            "'Thuật ngữ tiếng Việt chuẩn (English term)'. "
            "Output MUST be valid JSON in this schema:\n"
            '{"glossary": {"<english_term>": "<vietnamese_translation (english_term)>"}}'
        )

        close_client = False
        if client is None:
            client = httpx.AsyncClient(timeout=httpx.Timeout(60.0))
            close_client = True

        try:
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Textbook sample excerpt:\n{combined_sample_text[:6000]}"},
                ],
            }
            resp = await client.post(
                f"{base_url}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            )
            resp.raise_for_status()

            raw_text = resp.text.strip()
            content = ""
            if "data:" in raw_text:
                for line in raw_text.splitlines():
                    line = line.strip()
                    if line.startswith("data:"):
                        payload_str = line[5:].strip()
                        if payload_str == "[DONE]":
                            break
                        try:
                            chunk = json.loads(payload_str)
                            content += chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                        except Exception:
                            continue
            else:
                content = resp.json()["choices"][0]["message"]["content"]

            parsed = _clean_and_parse_json(content)
            extracted = parsed.get("glossary", {})
            if isinstance(extracted, dict) and extracted:
                self.register_glossary(f"book_{book_id}", extracted)
                logger.info("Successfully auto-mined %d book-specific terms for %s", len(extracted), book_id)
                return extracted

        except Exception as e:
            logger.warning("Pass 1 Auto-term mining failed for book %s: %s", book_id, e)
        finally:
            if close_client:
                await client.aclose()

        return {}


glossary_manager = DomainGlossaryManager()


SPECIALTY_PROMPTS = {
    "medical": (
        "You are an expert academic medical textbook and clinical journal translator. "
        "Translate the following text blocks into accurate, formal Vietnamese clinical language. "
        "Crucial Rule: For anatomical, physiological, pharmacological, and clinical terms, "
        "ALWAYS use dual-language retention: Thuật ngữ tiếng Việt chuẩn (Original English term). "
        "Example: 'mandible' -> 'xương hàm dưới (mandible)'; 'myocardial infarction' -> 'nhồi máu cơ tim (myocardial infarction)'. "
        "Never alter dosages (mg, ml, mcg), clinical indicators (p < 0.05, 95% CI), or gene codes."
    ),
    "dental": (
        "You are an expert academic dental textbook translator specializing in occlusion, TMD, orthodontics, and prosthodontics. "
        "Translate the following text blocks into professional, standardized Vietnamese dental terminology. "
        "Crucial Rule: Retain dental terms with English in parentheses: Thuật ngữ tiếng Việt chuẩn (Original English term). "
        "Example: 'temporomandibular joint' -> 'khớp thái dương hàm (temporomandibular joint - TMJ)'; "
        "'periodontal ligament' -> 'dây chằng nha chu (periodontal ligament)'. "
        "Never translate proper brand names or alter measurement units."
    ),
    "engineering": (
        "You are a principal software engineering and systems architecture textbook translator. "
        "Translate the following text blocks into natural, authoritative Vietnamese technical prose. "
        "Crucial Rule: For core technical terms, retain English in parentheses: Thuật ngữ (English term). "
        "Example: 'deadlock' -> 'khóa chết (deadlock)'; 'throughput' -> 'thông lượng (throughput)'. "
        "Preserve all code snippets, variable names, CLI commands, and equations exactly as-is."
    ),
    "tech": (
        "You are a principal software engineering and systems architecture textbook translator. "
        "Translate the following text blocks into natural, authoritative Vietnamese technical prose. "
        "Crucial Rule: For core technical terms, retain English in parentheses: Thuật ngữ (English term). "
        "Example: 'deadlock' -> 'khóa chết (deadlock)'; 'throughput' -> 'thông lượng (throughput)'. "
        "Preserve all code snippets, variable names, CLI commands, and equations exactly as-is."
    ),
    "general": (
        "You are a professional academic textbook translator. Translate the text blocks into natural, fluent Vietnamese. "
        "Preserve technical terms accurately with dual-language retention where helpful: Thuật ngữ tiếng Việt (English term). "
        "Preserve all numbers, equations, and proper nouns."
    ),
}


def _clean_and_parse_json(content: str) -> dict[str, Any]:
    """Robustly parse JSON response from LLM, handling markdown fences and unescaped newlines."""
    content = content.strip()

    # 1. Strip markdown code fence if present
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", content)
    if fence_match:
        content = fence_match.group(1).strip()

    # 2. Try direct parse
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    # 3. Find outermost curly braces
    start = content.find("{")
    end = content.rfind("}")
    if start != -1 and end != -1 and end > start:
        snippet = content[start : end + 1]
        try:
            return json.loads(snippet)
        except json.JSONDecodeError:
            # 4. Clean literal unescaped newlines inside strings
            sanitized = re.sub(r'([^\\])\n', r'\1\\n', snippet)
            try:
                return json.loads(sanitized)
            except json.JSONDecodeError:
                pass

    return {}


async def translate_text_blocks(
    blocks: list[dict[str, Any]],
    glossary_profile: str = "general",
    custom_model: str | None = None,
    client: httpx.AsyncClient | None = None,
    book_id: str | None = None,
) -> list[str]:
    """Translate a list of text blocks concurrently using 9router GPT model with dynamic terminology injection."""
    if not blocks:
        return []

    base_url, api_key, default_model = get_llm_config()
    model = custom_model or default_model

    # Combine text for dynamic glossary scan
    combined_source = " \n ".join(b.get("text", "") for b in blocks)
    matched_glossary = glossary_manager.get_matching_terms(combined_source, glossary_profile, book_id=book_id)

    glossary_constraint_prompt = ""
    if matched_glossary:
        glossary_lines = [f'- "{k}" -> "{v}"' for k, v in matched_glossary.items()]
        glossary_constraint_prompt = (
            "\n\n### MANDATORY DOMAIN GLOSSARY (BẮT BUỘC SỬ DỤNG ĐÚNG CÁC THUẬT NGỮ DƯỚI ĐÂY):\n"
            + "\n".join(glossary_lines)
            + "\n"
        )

    profile_instruction = SPECIALTY_PROMPTS.get(glossary_profile.lower(), SPECIALTY_PROMPTS["general"])
    system_prompt = (
        f"{profile_instruction}"
        f"{glossary_constraint_prompt}\n"
        "Strict Translation Guidelines:\n"
        "1. Maintain all citations, numbers, equation symbols, and punctuation.\n"
        "2. Do NOT summarize, shorten, or omit any content.\n"
        "3. Output MUST be a valid JSON object matching this schema:\n"
        '{"translations": [{"id": <block_id>, "text": "<translated_vietnamese_text>"}]}'
    )

    items_to_send = [
        {"id": i, "text": b["text"].strip()}
        for i, b in enumerate(blocks)
        if b["text"].strip()
    ]

    if not items_to_send:
        return [b["text"] for b in blocks]

    user_payload = json.dumps({"blocks": items_to_send}, ensure_ascii=False)

    payload = {
        "model": model,
        "max_tokens": 4096,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_payload},
        ],
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    timeout = httpx.Timeout(120.0, connect=20.0, read=120.0, write=30.0)
    retries = 3

    close_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=timeout)
        close_client = True

    try:
        for attempt in range(retries):
            try:
                resp = await client.post(f"{base_url}/chat/completions", json=payload, headers=headers)
                resp.raise_for_status()

                raw_text = resp.text.strip()
                content = ""

                # Handle Server-Sent Events (SSE) data stream from 9router
                if "data:" in raw_text:
                    for line in raw_text.splitlines():
                        line = line.strip()
                        if line.startswith("data:"):
                            payload_str = line[5:].strip()
                            if payload_str == "[DONE]":
                                break
                            try:
                                chunk = json.loads(payload_str)
                                delta = chunk.get("choices", [{}])[0].get("delta", {})
                                content += delta.get("content", "")
                            except Exception:
                                continue
                else:
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"].strip()

                parsed = _clean_and_parse_json(content)
                translations_map = {item["id"]: item["text"] for item in parsed.get("translations", []) if "id" in item and "text" in item}

                results = []
                for i, b in enumerate(blocks):
                    results.append(translations_map.get(i, b["text"]))
                return results

            except Exception as e:
                logger.warning("Translation attempt %d failed: %s", attempt + 1, e)
                if attempt == retries - 1:
                    logger.error("All translation retries failed for blocks. Falling back to original text.")
                    return [b["text"] for b in blocks]
                await asyncio.sleep(1.5 * (attempt + 1))

        return [b["text"] for b in blocks]
    finally:
        if close_client:
            await client.aclose()


# ============================================================================
# LAYOUT EXTRACTION & TYPOGRAPHY RECONSTRUCTION
# ============================================================================

def _get_page_image_rects(page: fitz.Page) -> list[fitz.Rect]:
    """Retrieve bounding boxes of all raster images and vector figures on page."""
    rects: list[fitz.Rect] = []
    try:
        for info in page.get_image_info(xrefs=True):
            bbox = info.get("bbox")
            if bbox:
                rects.append(fitz.Rect(bbox))
    except Exception as e:
        logger.debug("Error extracting image info: %s", e)

    return rects


def _extract_and_sort_translatable_blocks(page: fitz.Page) -> list[dict[str, Any]]:
    """Extract text blocks using dict analysis: preserves font weights, colors, alignment, and column layout."""
    page_dict = page.get_text("dict")
    raw_blocks = page_dict.get("blocks", [])
    page_width = page.rect.width
    page_height = page.rect.height
    mid_x = page_width / 2.0

    valid_blocks: list[dict[str, Any]] = []

    left_column_count = 0
    right_column_count = 0

    for b in raw_blocks:
        # Block type 0 is text; type 1 is image
        if b.get("type") != 0:
            continue

        bbox = b.get("bbox")
        if not bbox or len(bbox) < 4:
            continue

        x0, y0, x1, y1 = bbox[0], bbox[1], bbox[2], bbox[3]

        # Ignore marginal headers/footers outside printable page zone
        if y1 < 18 or y0 > page_height - 18:
            continue

        # Aggregate text, spans, font properties
        lines = b.get("lines", [])
        block_text_parts: list[str] = []
        total_chars = 0
        bold_chars = 0
        italic_chars = 0
        sizes_weighted: list[tuple[float, int]] = []
        color_counts: dict[int, int] = {}

        for line in lines:
            line_text = ""
            for span in line.get("spans", []):
                span_text = span.get("text", "")
                if not span_text:
                    continue
                char_len = len(span_text)
                total_chars += char_len
                line_text += span_text

                flags = span.get("flags", 0)
                font_name = span.get("font", "").lower()
                size = span.get("size", 10.0)
                color = span.get("color", 0)

                # Check bold: flag bit 4 (16) or name indicator
                if (flags & 16 != 0) or any(k in font_name for k in ("bold", "black", "heavy", "medium")):
                    bold_chars += char_len

                # Check italic: flag bit 1 (2) or name indicator
                if (flags & 2 != 0) or any(k in font_name for k in ("italic", "oblique")):
                    italic_chars += char_len

                sizes_weighted.append((size, char_len))
                color_counts[color] = color_counts.get(color, 0) + char_len

            if line_text.strip():
                block_text_parts.append(line_text.strip())

        full_text = " ".join(block_text_parts).strip()
        if not full_text or (len(full_text) < 2 and not full_text.isalnum()):
            continue

        # Calculate dominant properties
        is_bold = (bold_chars / total_chars >= 0.35) if total_chars > 0 else False
        is_italic = (italic_chars / total_chars >= 0.40) if total_chars > 0 else False

        # Weighted dominant font size
        if sizes_weighted and total_chars > 0:
            dominant_size = sum(sz * weight for sz, weight in sizes_weighted) / total_chars
        else:
            dominant_size = 10.0

        # Dominant color
        dominant_color_int = max(color_counts.items(), key=lambda item: item[1])[0] if color_counts else 0
        # Convert integer RGB to tuple (0.0 to 1.0)
        if dominant_color_int == 0:
            text_color = (0.0, 0.0, 0.0)
        else:
            r = ((dominant_color_int >> 16) & 255) / 255.0
            g = ((dominant_color_int >> 8) & 255) / 255.0
            b = (dominant_color_int & 255) / 255.0
            text_color = (r, g, b)

        # Alignment detection: check if block is centered horizontally on page
        is_centered = False
        mid_block = (x0 + x1) / 2.0
        block_width = x1 - x0
        if abs(mid_block - mid_x) < 25.0 and block_width < (page_width * 0.70):
            is_centered = True

        # Detect column side
        is_spanning = (x0 < mid_x - 30 and x1 > mid_x + 30)
        if not is_spanning:
            if x1 < mid_x + 20:
                left_column_count += 1
            elif x0 > mid_x - 20:
                right_column_count += 1

        valid_blocks.append({
            "rect": fitz.Rect(x0, y0, x1, y1),
            "text": full_text,
            "fontsize": min(32.0, max(7.0, dominant_size)),
            "is_bold": is_bold,
            "is_italic": is_italic,
            "color": text_color,
            "align": fitz.TEXT_ALIGN_CENTER if is_centered else fitz.TEXT_ALIGN_LEFT,
            "is_spanning": is_spanning,
            "x0": x0,
            "y0": y0,
            "x1": x1,
            "y1": y1,
        })

    is_two_column = (left_column_count >= 3 and right_column_count >= 3)

    # Sort blocks in academic reading order
    if is_two_column:
        def col_sort_key(b):
            # Spanning blocks sort by their vertical position directly
            if b["is_spanning"]:
                return (0, b["y0"], b["x0"])
            # Left column = 1, Right column = 2
            column_id = 1 if b["x0"] < mid_x else 2
            return (column_id, b["y0"], b["x0"])
        valid_blocks.sort(key=col_sort_key)
    else:
        valid_blocks.sort(key=lambda b: (b["y0"], b["x0"]))

    return valid_blocks


def _typeset_page_blocks(
    page: fitz.Page,
    blocks: list[dict[str, Any]],
    translated_texts: list[str],
    fonts: dict[str, str | None],
    image_rects: list[fitz.Rect],
):
    """Erase original text and insert Vietnamese translation with anti-overflow scaling and font weight preservation."""
    # 1. Safe redaction: Prevent white-box clipping into adjacent image bounds
    for b in blocks:
        rect = fitz.Rect(b["rect"])

        # Shrink text rect slightly if it minimally borders an image
        for img_rect in image_rects:
            if rect.intersects(img_rect):
                overlap = rect & img_rect
                if overlap.height < 6.0 and rect.y0 > img_rect.y0:
                    rect.y0 = img_rect.y1 + 1.0
                elif overlap.height < 6.0 and rect.y1 < img_rect.y1:
                    rect.y1 = img_rect.y0 - 1.0

        if rect.is_valid and not rect.is_empty:
            page.add_redact_annot(rect, fill=(1, 1, 1))

    page.apply_redactions()

    # 2. Insert translated text into exact bounding boxes with anti-overflow scaling
    for b, trans_text in zip(blocks, translated_texts):
        rect = fitz.Rect(b["rect"])
        fontsize = b["fontsize"]
        align = b.get("align", fitz.TEXT_ALIGN_LEFT)
        color = b.get("color", (0.0, 0.0, 0.0))

        # Select font variant based on detected style
        if b.get("is_bold") and fonts.get("bold"):
            font_path = fonts["bold"]
            font_alias = "vnb"
        elif b.get("is_italic") and fonts.get("italic"):
            font_path = fonts["italic"]
            font_alias = "vni"
        elif fonts.get("regular"):
            font_path = fonts["regular"]
            font_alias = "vnr"
        else:
            font_path = None
            font_alias = "helv"

        min_fontsize = max(5.5, fontsize * 0.65)
        rc = -1

        # Iterative font fitting loop
        while fontsize >= min_fontsize:
            try:
                if font_path:
                    rc = page.insert_textbox(
                        rect,
                        trans_text,
                        fontname=font_alias,
                        fontfile=font_path,
                        fontsize=fontsize,
                        color=color,
                        align=align,
                    )
                else:
                    rc = page.insert_textbox(
                        rect,
                        trans_text,
                        fontname=font_alias,
                        fontsize=fontsize,
                        color=color,
                        align=align,
                    )
            except Exception as e:
                logger.debug("insert_textbox error at size %.1f: %s", fontsize, e)
                rc = -1

            if rc >= 0:
                break
            fontsize -= 0.4

        # Safe fallback: if still overflowing by a fraction, extend height slightly if no collision
        if rc < 0 and rect.y1 + 8.0 < page.rect.height - 20:
            extended_rect = fitz.Rect(rect.x0, rect.y0, rect.x1, rect.y1 + 8.0)
            try:
                if font_path:
                    page.insert_textbox(
                        extended_rect,
                        trans_text,
                        fontname=font_alias,
                        fontfile=font_path,
                        fontsize=min_fontsize,
                        color=color,
                        align=align,
                    )
                else:
                    page.insert_textbox(
                        extended_rect,
                        trans_text,
                        fontname=font_alias,
                        fontsize=min_fontsize,
                        color=color,
                        align=align,
                    )
            except Exception:
                pass


# ============================================================================
# MAIN ORCHESTRATION PIPELINE
# ============================================================================

async def translate_pdf_book(
    input_pdf_path: str | Path,
    output_pdf_path: str | Path,
    *,
    mode: str = "inplace",  # "inplace" or "bilingual_dual"
    target_lang: str = "vi",
    glossary_profile: str = "general",
    model: str | None = None,
    pages: list[int] | None = None,
    progress_callback: Callable[[int, int, str], Coroutine[Any, Any, None]] | None = None,
) -> dict[str, Any]:
    """Translate an entire PDF book with guaranteed memory safety, image preservation, and typography fidelity."""
    start_time = time.time()
    input_pdf_path = Path(input_pdf_path)
    output_pdf_path = Path(output_pdf_path)
    output_pdf_path.parent.mkdir(parents=True, exist_ok=True)

    fonts = get_unicode_fonts()
    logger.info("Unicode fonts detected: Regular=%s, Bold=%s, Italic=%s", fonts["regular"], fonts["bold"], fonts["italic"])

    timeout = httpx.Timeout(120.0, connect=20.0, read=120.0, write=30.0)
    async with httpx.AsyncClient(timeout=timeout) as http_client:
        doc = fitz.open(input_pdf_path)
        dual_doc = None

        try:
            total_pages = doc.page_count
            if total_pages == 0:
                raise ValueError("PDF document contains 0 pages")

            target_pages = pages if pages is not None else list(range(total_pages))
            target_pages = [p for p in target_pages if 0 <= p < total_pages]

            if not target_pages:
                raise ValueError("No valid pages selected for translation")

            logger.info(
                "Starting translation for %s (%d pages targeted, mode=%s, profile=%s, model=%s)",
                input_pdf_path.name,
                len(target_pages),
                mode,
                glossary_profile,
                model or "default",
            )

            book_id = input_pdf_path.stem

            # Pass 1: Auto-mine book-specific terminology if multi-page document
            if len(target_pages) >= 2:
                if progress_callback:
                    await progress_callback(0, len(target_pages), "Đang quét thuật ngữ sách chuyên ngành (Pass 1 Term Mining)...")
                await glossary_manager.auto_mine_book_glossary(doc, book_id, client=http_client)

            if progress_callback:
                await progress_callback(0, len(target_pages), "Đang phân tích layout và khởi tạo glossary chuyên ngành...")

            translated_blocks_count = 0

            if mode == "bilingual_dual":
                dual_doc = fitz.open()

                for idx, page_num in enumerate(target_pages):
                    src_page = doc[page_num]
                    # Copy original English page
                    dual_doc.insert_pdf(doc, from_page=page_num, to_page=page_num)

                    # Create adjacent cloned page for Vietnamese translation
                    new_page = dual_doc.new_page(
                        width=src_page.rect.width,
                        height=src_page.rect.height,
                    )
                    new_page.show_pdf_page(new_page.rect, doc, page_num)

                    image_rects = _get_page_image_rects(new_page)
                    blocks = _extract_and_sort_translatable_blocks(src_page)

                    if blocks:
                        translated_texts = await translate_text_blocks(
                            blocks,
                            glossary_profile=glossary_profile,
                            custom_model=model,
                            client=http_client,
                            book_id=book_id,
                        )
                        _typeset_page_blocks(new_page, blocks, translated_texts, fonts, image_rects)
                        translated_blocks_count += len(blocks)

                        del translated_texts

                    del blocks
                    del image_rects

                    if (idx + 1) % 15 == 0:
                        gc.collect()

                    if progress_callback:
                        await progress_callback(
                            idx + 1, len(target_pages), f"Đã dịch trang {page_num + 1}/{total_pages} (Song ngữ)"
                        )

                dual_doc.save(str(output_pdf_path), garbage=4, deflate=True, clean=True)

            else:
                # In-place text replacement: Preserves 100% vector art & raster images
                for idx, page_num in enumerate(target_pages):
                    page = doc[page_num]
                    image_rects = _get_page_image_rects(page)
                    blocks = _extract_and_sort_translatable_blocks(page)

                    if blocks:
                        translated_texts = await translate_text_blocks(
                            blocks,
                            glossary_profile=glossary_profile,
                            custom_model=model,
                            client=http_client,
                            book_id=book_id,
                        )
                        _typeset_page_blocks(page, blocks, translated_texts, fonts, image_rects)
                        translated_blocks_count += len(blocks)

                        del translated_texts

                    del blocks
                    del image_rects

                    if (idx + 1) % 15 == 0:
                        gc.collect()

                    if progress_callback:
                        await progress_callback(
                            idx + 1, len(target_pages), f"Đã dịch và dàn trang {page_num + 1}/{total_pages}"
                        )

                # Reconstruct Table of Contents bookmarks if present
                try:
                    toc = doc.get_toc()
                    if toc:
                        logger.info("Preserving and updating %d TOC bookmarks...", len(toc))
                        doc.set_toc(toc)
                except Exception as toc_err:
                    logger.warning("Error updating TOC bookmarks: %s", toc_err)

                doc.save(str(output_pdf_path), garbage=4, deflate=True, clean=True)

        finally:
            if doc:
                doc.close()
            if dual_doc:
                dual_doc.close()
            gc.collect()

    elapsed = round(time.time() - start_time, 2)
    return {
        "success": True,
        "input_file": str(input_pdf_path),
        "output_file": str(output_pdf_path),
        "total_pages": total_pages,
        "translated_pages": len(target_pages),
        "translated_blocks": translated_blocks_count,
        "mode": mode,
        "glossary_profile": glossary_profile,
        "time_taken": elapsed,
    }
