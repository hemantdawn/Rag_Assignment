from __future__ import annotations

import os
import re
from pathlib import Path
from uuid import uuid4


HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def parse_document(path: Path, filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in {".txt", ".md"}:
        return path.read_text(encoding="utf-8-sig")
    if suffix in {".pdf", ".docx"}:
        # Reuse model artifacts downloaded for this workspace on offline runs.
        project_cache = Path(__file__).resolve().parents[1] / ".hf-cache"
        if project_cache.is_dir():
            os.environ.setdefault("HF_HOME", str(project_cache))
        try:
            from docling.document_converter import DocumentConverter
        except ImportError as exc:
            if suffix == ".pdf":
                return _pdf_text_fallback(path)
            raise RuntimeError("DOCX ingestion needs: pip install -e '.[documents]'") from exc
        result = DocumentConverter().convert(str(path))
        return result.document.export_to_markdown()
    raise ValueError(f"Unsupported document type: {suffix}")


def _pdf_text_fallback(path: Path) -> str:
    """Extract text and font-sized headings when Docling is unavailable."""
    try:
        import pdfplumber
    except ImportError as exc:
        raise RuntimeError("PDF ingestion needs Docling or pdfplumber") from exc

    lines: list[str] = []
    with pdfplumber.open(path) as document:
        for page in document.pages:
            words = sorted(
                page.extract_words(extra_attrs=["size"]),
                key=lambda word: (word["top"], word["x0"]),
            )
            groups: list[list[dict]] = []
            for word in words:
                if not groups or abs(word["top"] - groups[-1][0]["top"]) > 2.5:
                    groups.append([])
                groups[-1].append(word)
            for group in groups:
                group.sort(key=lambda word: word["x0"])
                text = " ".join(word["text"] for word in group).strip()
                if not text:
                    continue
                size = max(word["size"] for word in group)
                if size >= 16 and len(text) <= 120:
                    lines.append(f"\n# {text}\n")
                elif size >= 11.5 and len(text) <= 120:
                    lines.append(f"\n## {text}\n")
                else:
                    lines.append(text)
            lines.append("")
    markdown = "\n".join(lines).strip()
    if not markdown:
        raise ValueError("PDF has no extractable text; an OCR-capable parser is required")
    return markdown


def _split_words(text: str, max_chars: int) -> list[str]:
    words = text.split()
    pieces: list[str] = []
    current: list[str] = []
    length = 0
    for word in words:
        if current and length + len(word) + 1 > max_chars:
            pieces.append(" ".join(current))
            current, length = [], 0
        current.append(word)
        length += len(word) + 1
    if current:
        pieces.append(" ".join(current))
    return pieces


def _sections(markdown: str, title: str) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    heading_stack: list[str] = []
    lines: list[str] = []

    def flush() -> None:
        body = "\n".join(lines).strip()
        if body:
            result.append((" / ".join(heading_stack) or title, body))
        lines.clear()

    for line in markdown.splitlines():
        match = HEADING.match(line)
        if match:
            flush()
            depth = len(match.group(1))
            heading_stack[:] = heading_stack[:depth - 1]
            heading_stack.append(match.group(2).strip())
        else:
            lines.append(line)
    flush()
    return result


def _parent_pieces(body: str, max_chars: int = 1600) -> list[str]:
    paragraphs = [re.sub(r"\s+", " ", part).strip() for part in re.split(r"\n\s*\n", body)]
    pieces: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if not paragraph:
            continue
        for part in _split_words(paragraph, max_chars):
            if current and len(current) + len(part) + 2 > max_chars:
                pieces.append(current)
                current = ""
            current = f"{current}\n\n{part}" if current else part
    if current:
        pieces.append(current)
    return pieces


def _child_pieces(text: str, window: int = 80, overlap: int = 15) -> list[str]:
    words = text.split()
    if not words:
        return []
    step = window - overlap
    result = []
    for start in range(0, len(words), step):
        result.append(" ".join(words[start:start + window]))
        if start + window >= len(words):
            break
    return result


def make_chunks(markdown: str, filename: str) -> tuple[list[dict], list[dict]]:
    title = Path(filename).stem
    parents: list[dict] = []
    children: list[dict] = []
    for heading, body in _sections(markdown, title):
        for piece in _parent_pieces(body):
            parent_id = str(uuid4())
            parents.append({
                "id": parent_id, "ordinal": len(parents), "heading": heading, "text": piece,
            })
            prefix = f"Document: {title}. Section: {heading}. "
            for child_text in _child_pieces(piece):
                children.append({
                    "id": str(uuid4()), "parent_id": parent_id,
                    "ordinal": len(children), "text": child_text,
                    "contextual_text": prefix + child_text,
                })
    return parents, children
