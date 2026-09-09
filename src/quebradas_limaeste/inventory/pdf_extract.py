"""Bounded text extraction from locally retained PDF documents."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import pymupdf

PDF_SIGNATURE = b"%PDF-"


class PDFExtractionError(RuntimeError):
    """Raised when a local PDF cannot be validated or extracted."""


@dataclass(frozen=True)
class PDFExtractionResult:
    """Normalized per-page text and extraction review state."""

    page_count: int
    page_texts: tuple[str, ...]
    full_text: str
    text_path: Path
    extraction_status: str
    ocr_required: bool
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "page_count": self.page_count,
            "text_path": str(self.text_path),
            "extraction_status": self.extraction_status,
            "ocr_required": self.ocr_required,
            "warnings": list(self.warnings),
        }


def extract_pdf_text(
    pdf_path: Path,
    *,
    text_path: Path,
    minimum_text_characters: int,
    allowed_root: Path,
    source_root: Path | None = None,
) -> PDFExtractionResult:
    """Extract normalized text without interpreting or executing its contents."""
    if minimum_text_characters < 1:
        raise PDFExtractionError("minimum_text_characters must be positive")
    source = _safe_path(
        pdf_path,
        allowed_root=source_root or allowed_root,
        must_exist=True,
    )
    destination = _safe_path(text_path, allowed_root=allowed_root)
    if source == destination:
        raise PDFExtractionError("PDF and text paths must be different")
    if source.is_symlink() or destination.is_symlink():
        raise PDFExtractionError("symlink paths are not allowed")
    with source.open("rb") as pdf_file:
        if pdf_file.read(len(PDF_SIGNATURE)) != PDF_SIGNATURE:
            raise PDFExtractionError("local file does not have a PDF signature")

    try:
        with pymupdf.open(source) as document:
            if not document.is_pdf:
                raise PDFExtractionError("local file is not a PDF document")
            page_texts = tuple(
                _normalize_page_text(page.get_text("text", sort=True))
                for page in document
            )
    except PDFExtractionError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise PDFExtractionError("PyMuPDF could not extract the document") from exc

    full_text = "\n\n".join(text for text in page_texts if text)
    extracted_characters = sum(not char.isspace() for char in full_text)
    ocr_required = extracted_characters < minimum_text_characters
    extraction_status = "empty" if ocr_required else "success"
    warnings = ("insufficient_text_for_extraction",) if ocr_required else ()
    _write_text(destination, "\n\f\n".join(page_texts))
    return PDFExtractionResult(
        page_count=len(page_texts),
        page_texts=page_texts,
        full_text=full_text,
        text_path=destination,
        extraction_status=extraction_status,
        ocr_required=ocr_required,
        warnings=warnings,
    )


def _normalize_page_text(value: str) -> str:
    lines = []
    for line in value.replace("\x00", "").splitlines():
        normalized = re.sub(r"[ \t\v]+", " ", line).strip()
        if normalized:
            lines.append(normalized)
    return "\n".join(lines)


def _safe_path(path: Path, *, allowed_root: Path, must_exist: bool = False) -> Path:
    root = allowed_root.resolve()
    if path.is_symlink():
        raise PDFExtractionError("symlink paths are not allowed")
    candidate = path.resolve(strict=must_exist)
    if not candidate.is_relative_to(root):
        raise PDFExtractionError("path is outside the allowed root")
    if must_exist and not candidate.is_file():
        raise PDFExtractionError("PDF path is not a regular file")
    return candidate


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part_path = path.with_name(f"{path.name}.part")
    if part_path.exists() or part_path.is_symlink():
        raise PDFExtractionError("staging text path already exists")
    try:
        with part_path.open("x", encoding="utf-8", newline="\n") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        part_path.replace(path)
    finally:
        if part_path.exists():
            part_path.unlink()
