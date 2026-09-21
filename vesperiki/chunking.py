"""Deterministic, section-aware page chunking for embeddings."""
from __future__ import annotations

from .sections import parse_sections

TARGET_CHARS = 1000


def _paragraph_chunks(text: str, target: int = TARGET_CHARS) -> list[str]:
    if not text:
        return []
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = paragraph if not current else current + "\n\n" + paragraph
        if current and len(candidate) > target:
            chunks.append(current)
            current = paragraph
        else:
            current = candidate
    if current:
        chunks.append(current)
    # A single overlong paragraph cannot be split on a paragraph boundary;
    # split only at newline/character boundaries, which keeps fenced code intact.
    final: list[str] = []
    for chunk in chunks:
        if len(chunk) <= target or chunk.count("```") % 2:
            # A fenced block is an atomic unit. It may exceed the soft target,
            # but splitting it would produce invalid Markdown/code.
            final.append(chunk)
            continue
        start = 0
        while start < len(chunk):
            end = min(start + target, len(chunk))
            if end < len(chunk):
                newline = chunk.rfind("\n", start, end)
                if newline > start:
                    end = newline
            final.append(chunk[start:end])
            start = end
    return final


def chunk_page(body: str) -> list[dict[str, object]]:
    """Return stable ``seq``, ``heading_path`` and ``body`` chunk records."""
    if not body:
        return []
    result: list[dict[str, object]] = []
    seq = 0
    for section in parse_sections(body):
        section_body = section["content"]
        # parse_sections includes the heading in section content only by its
        # range semantics; the first line is omitted from content, so this is
        # exactly the text to embed.
        for text in _paragraph_chunks(section_body):
            if text:
                result.append({
                    "seq": seq,
                    "heading_path": section["heading"] or "",
                    "body": text,
                    "char_count": len(text),
                })
                seq += 1
    return result
