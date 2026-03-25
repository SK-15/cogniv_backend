from __future__ import annotations

from io import BytesIO


class ResumeTextExtractionError(Exception):
    pass


def extract_resume_text(*, filename: str | None, data: bytes) -> str:
    """
    Extract plain text from an uploaded resume file in-memory.

    Supported:
      - .pdf  -> PyMuPDF (pymupdf)
      - .docx -> python-docx
    """
    if not data:
        raise ResumeTextExtractionError("Resume file is empty.")

    suffix = ""
    if filename:
        suffix = filename.lower().rsplit(".", 1)[-1]

    # Use suffix derived from filename only (caller enforces allowlist).
    if not filename or "." not in filename:
        raise ResumeTextExtractionError("Missing or invalid resume filename.")

    ext = "." + suffix
    if ext == ".pdf":
        import fitz  # PyMuPDF

        try:
            doc = fitz.open(stream=data, filetype="pdf")
        except Exception as e:
            raise ResumeTextExtractionError(f"Invalid PDF: {e}") from e

        parts: list[str] = []
        for page in doc:
            try:
                parts.append(page.get_text("text") or "")
            except Exception:
                # If a page fails, continue extracting from others.
                continue
        text = "\n".join(parts).strip()
        if not text:
            raise ResumeTextExtractionError("Could not extract text from PDF.")
        return _normalize_text(text)

    if ext == ".docx":
        from docx import Document

        try:
            document = Document(BytesIO(data))
        except Exception as e:
            raise ResumeTextExtractionError(f"Invalid DOCX: {e}") from e

        parts: list[str] = []
        for p in document.paragraphs:
            if p.text:
                parts.append(p.text)
        text = "\n".join(parts).strip()
        if not text:
            raise ResumeTextExtractionError("Could not extract text from DOCX.")
        return _normalize_text(text)

    raise ResumeTextExtractionError(f"Unsupported resume extension: {ext}")


def _normalize_text(text: str) -> str:
    # Collapse repeated newlines but keep paragraph boundaries readable.
    lines = [ln.strip() for ln in text.splitlines()]
    filtered: list[str] = []
    for ln in lines:
        if not ln:
            # Keep at most one empty line in a row.
            if filtered and filtered[-1] != "":
                filtered.append("")
            continue
        filtered.append(ln)

    return "\n".join(filtered).strip()

