"""Re-reads a generated PDF like an ATS would (SPEC section 8.3): extractable text, reading
order, required skills present, page count."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel
from pypdf import PdfReader
from pypdf.errors import PdfReadError

MIN_TEXT_CHARS = 200
SNIPPET_CHARS = 40


class AtsReport(BaseModel):
    pages: int
    max_pages: int
    text_chars: int
    extractable: bool
    order_ok: bool
    missing_snippets: list[str]
    required_present: list[str]
    required_missing: list[str]

    @property
    def ok(self) -> bool:
        return (
            self.extractable
            and self.order_ok
            and not self.missing_snippets
            and self.pages <= self.max_pages
            and not self.required_missing
        )

    @property
    def warnings(self) -> list[str]:
        warnings = []
        if not self.extractable:
            warnings.append("Texte peu ou pas extractible : un ATS ne pourra pas lire ce PDF.")
        if self.pages > self.max_pages:
            warnings.append(f"{self.pages} pages pour un maximum de {self.max_pages}.")
        if not self.order_ok:
            warnings.append("L'ordre de lecture extrait ne suit pas l'ordre du document.")
        if self.missing_snippets:
            warnings.append(
                f"{len(self.missing_snippets)} passage(s) introuvable(s) dans le texte."
            )
        if self.required_missing:
            missing = ", ".join(self.required_missing)
            warnings.append(f"Compétences requises que tu possèdes mais absentes : {missing}.")
        return warnings


def _normalize(text: str) -> str:
    return " ".join(text.split()).casefold()


def extract_text(pdf_path: Path) -> tuple[str, int]:
    try:
        reader = PdfReader(pdf_path)
        return "\n".join(page.extract_text() or "" for page in reader.pages), len(reader.pages)
    except (PdfReadError, OSError):
        return "", 0


def check_pdf(
    pdf_path: Path,
    *,
    snippets: list[str],
    max_pages: int,
    required_terms: list[str] | None = None,
) -> AtsReport:
    """`snippets` are passages expected in this order (name, titles, bullet starts...)."""
    raw, pages = extract_text(pdf_path)
    text = _normalize(raw)

    missing, order_ok, cursor = [], True, 0
    for snippet in snippets:
        needle = _normalize(snippet)[:SNIPPET_CHARS]
        if not needle:
            continue
        position = text.find(needle, cursor)
        if position == -1:
            if needle in text:
                order_ok = False  # present, but before the previous snippet
            else:
                missing.append(snippet)
            continue
        cursor = position + len(needle)

    required = required_terms or []
    present = [t for t in required if _normalize(t) in text]
    return AtsReport(
        pages=pages,
        max_pages=max_pages,
        text_chars=len(text),
        extractable=len(text) >= MIN_TEXT_CHARS,
        order_ok=order_ok,
        missing_snippets=missing,
        required_present=present,
        required_missing=[t for t in required if t not in present],
    )
