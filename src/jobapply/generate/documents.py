"""From generated (or edited) content to a stored document: render, ATS check, payload.

Also defines which texts can be edited by hand in the interface, and how edits are applied.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from jobapply.ats.check import check_pdf
from jobapply.config import AppConfig
from jobapply.generate.render import build_cv_data, build_letter_data, render_pdf
from jobapply.generate.validator import Issue
from jobapply.matching.scorer import ProfileVocabulary
from jobapply.models.db import DocumentKind
from jobapply.models.offer import JobOffer
from jobapply.models.profile import Profile
from jobapply.models.tailored import CoverLetter, TailoredCV

TEMPLATES = {DocumentKind.CV: "cv.typ", DocumentKind.LETTER: "cover_letter.typ"}
LETTER_MAX_PAGES = 1


def owned_required_skills(profile: Profile, offer: JobOffer) -> list[str]:
    """Required skills of the offer that the profile actually has: they must be on the CV."""
    vocab = ProfileVocabulary.from_profile(profile)
    return [skill for skill in offer.required_skills if vocab.matches(skill)]


def _cv_snippets(data: dict[str, Any]) -> list[str]:
    snippets = [data["name"], data["headline"]]
    for section in data["sections"]:
        snippets.append(section["title"])
        for entry in section.get("entries", []):
            snippets += [entry["title"], *entry["bullets"]]
    return snippets


def build_document(
    cfg: AppConfig,
    kind: DocumentKind,
    content: TailoredCV | CoverLetter,
    profile: Profile,
    offer: JobOffer,
    output: Path,
    issues: list[Issue],
    today: date,
) -> dict[str, Any]:
    """Render the PDF at `output` and return the payload stored on the Document row."""
    template = cfg.paths.templates_dir / TEMPLATES[kind]
    if isinstance(content, TailoredCV):
        data = build_cv_data(content, profile)
        snippets = _cv_snippets(data)
        max_pages, required = cfg.settings.cv.max_pages, owned_required_skills(profile, offer)
    else:
        data = build_letter_data(content, profile, offer, today)
        snippets = [data["name"], content.subject, content.greeting, *data["paragraphs"]]
        max_pages, required = LETTER_MAX_PAGES, []
    render_pdf(template, data, output)
    report = check_pdf(output, snippets=snippets, max_pages=max_pages, required_terms=required)
    payload: dict[str, Any] = {
        "content": content.model_dump(mode="json"),
        "issues": [str(issue) for issue in issues],
        "ats": report.model_dump(),
        "ats_warnings": report.warnings,
    }
    if isinstance(content, CoverLetter):
        payload["text"] = content.as_text(profile.identity.name)
        payload["word_count"] = content.word_count
    return payload


# --- Manual edits -------------------------------------------------------------------------


@dataclass(frozen=True)
class EditableField:
    path: str  # dotted path inside the content, e.g. "experiences.0.bullets.1.text"
    label: str
    text: str
    multiline: bool


def editable_fields(kind: DocumentKind, content: dict[str, Any]) -> list[EditableField]:
    if kind is DocumentKind.LETTER:
        fields = [
            EditableField("subject", "Objet", content["subject"], False),
            EditableField("greeting", "Formule d'appel", content["greeting"], False),
        ]
        fields += [
            EditableField(f"paragraphs.{i}.text", f"Paragraphe {i + 1}", p["text"], True)
            for i, p in enumerate(content["paragraphs"])
        ]
        fields.append(EditableField("closing", "Formule de politesse", content["closing"], False))
        return fields

    fields = [EditableField("headline.text", "Accroche", content["headline"]["text"], False)]
    for section, label in (
        ("experiences", "Expérience"),
        ("education", "Formation"),
        ("projects", "Projet"),
    ):
        for i, entry in enumerate(content[section]):
            for j, bullet in enumerate(entry["bullets"]):
                fields.append(
                    EditableField(
                        f"{section}.{i}.bullets.{j}.text",
                        f"{label} {entry['source_id']} · {bullet['source_id']}",
                        bullet["text"],
                        True,
                    )
                )
    return fields


def apply_edits(content: dict[str, Any], edits: dict[str, str]) -> dict[str, Any]:
    """Return a copy of `content` with the edited texts. Unknown paths raise ValueError."""
    allowed = {f.path for f in editable_fields(_kind_of(content), content)}
    updated = copy.deepcopy(content)
    for path, text in edits.items():
        if path not in allowed:
            raise ValueError(f"Champ non modifiable : {path}")
        *parents, last = path.split(".")
        node: Any = updated
        for key in parents:
            node = node[int(key)] if key.isdigit() else node[key]
        node[last] = text.strip()
    return updated


def _kind_of(content: dict[str, Any]) -> DocumentKind:
    return DocumentKind.LETTER if "paragraphs" in content else DocumentKind.CV
