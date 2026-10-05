"""TailoredCV / CoverLetter -> JSON -> Typst -> PDF (SPEC section 8.3).

Factual fields (titles, companies, schools, dates, contact details) come from the profile;
only the selected and rephrased text comes from the generated content.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import date
from pathlib import Path

import typst

from jobapply.models.offer import JobOffer
from jobapply.models.profile import Education, Experience, Profile, Project, Skill
from jobapply.models.tailored import CoverLetter, TailoredCV, TailoredEntry

SECTION_TITLES = {
    "experience": "Experience",
    "education": "Education",
    "projects": "Projects",
    "skills": "Skills",
}


class RenderError(Exception):
    """Typst could not compile the document."""


def _contact(profile: Profile) -> list[str]:
    identity = profile.identity
    return [identity.email, identity.phone, identity.location, *identity.links.values()]


def _entries(profile: Profile, entries: list[TailoredEntry]) -> list[dict[str, object]]:
    rendered = []
    for entry in entries:
        source = profile.index[entry.source_id]
        bullets = [b.text for b in entry.bullets]
        match source:
            case Experience():
                title, dates = f"{source.title} — {source.company}", source.dates
                subtitle = source.location
            case Education():
                title, dates, subtitle = source.school, source.dates, source.degree
            case Project():
                title, dates, subtitle = source.name, "", source.description
            case _:
                raise ValueError(f"'{entry.source_id}' is not an experience/education/project")
        rendered.append({"title": title, "subtitle": subtitle, "dates": dates, "bullets": bullets})
    return rendered


def build_cv_data(cv: TailoredCV, profile: Profile) -> dict[str, object]:
    entries = {
        "experience": _entries(profile, cv.experiences),
        "education": _entries(profile, cv.education),
        "projects": _entries(profile, cv.projects),
    }
    skills = [s.name for i in cv.skills if isinstance(s := profile.index.get(i), Skill)]
    languages = [f"{lang.name} ({lang.level})" for lang in profile.languages]
    rows = [{"label": "Technical", "value": ", ".join(skills)}] if skills else []
    if languages:
        rows.append({"label": "Languages", "value": ", ".join(languages)})

    sections: list[dict[str, object]] = []
    for name in cv.section_order:
        if name == "skills":
            if rows:
                sections.append({"title": SECTION_TITLES[name], "rows": rows})
        elif entries[name]:
            sections.append({"title": SECTION_TITLES[name], "entries": entries[name]})
    return {
        "name": profile.identity.name,
        "contact": _contact(profile),
        "headline": cv.headline.text,
        "sections": sections,
    }


def build_letter_data(
    letter: CoverLetter, profile: Profile, offer: JobOffer, today: date
) -> dict[str, object]:
    return {
        "name": profile.identity.name,
        "contact": _contact(profile),
        "date": f"{today.day} {today:%B %Y}",
        "recipient": [offer.company, offer.location],
        "subject": letter.subject,
        "greeting": letter.greeting,
        "paragraphs": [p.text for p in letter.paragraphs],
        "closing": letter.closing,
    }


def render_pdf(template: Path, data: dict[str, object], output: Path) -> Path:
    """Compile `template` with `data` as data.json; write the PDF and its JSON next to it."""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.with_suffix(".json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        shutil.copy(template, workdir / "main.typ")
        (workdir / "data.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        try:
            pdf = typst.compile(str(workdir / "main.typ"), root=str(workdir))
        except RuntimeError as exc:
            raise RenderError(f"Échec du rendu PDF ({template.name}) : {exc}") from exc
    if not isinstance(pdf, bytes):
        raise RenderError(f"Échec du rendu PDF ({template.name}) : sortie inattendue")
    output.write_bytes(pdf)
    return output
