"""Anti-hallucination validator (SPEC section 8.3).

Checks that generated text only uses facts from the profile:
- every `source_id` exists, in the right place (a bullet belongs to the cited entry...);
- every number in a rephrased text appears in its source;
- every proper noun / technology name appears in the profile or in the offer.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from pydantic import BaseModel

from jobapply.config import CoverLetterSettings, CVSettings
from jobapply.models.offer import JobOffer
from jobapply.models.profile import (
    Bullet,
    Education,
    Experience,
    Headline,
    Motivation,
    Profile,
    Project,
    Skill,
)
from jobapply.models.tailored import CoverLetter, TailoredBullet, TailoredCV, TailoredEntry

NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
TOKEN_RE = re.compile(r"[A-Za-z][\w+#.\-/]*[\w+#]|[A-Za-z]")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?:;])\s+|\n+")
# Capitalised words that are not facts about the candidate.
COMMON_WORDS = frozenset(
    w.casefold()
    for w in [
        "I",
        "Dear",
        "Hiring",
        "Manager",
        "Team",
        "Recruitment",
        "Sincerely",
        "Regards",
        "Best",
        "Kind",
        "Yours",
        "Mr",
        "Ms",
        "Mrs",
        "Singapore",
        "Asia",
        "CV",
        "Re",
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ]
)
LETTER_WORD_TOLERANCE = 0.1  # LLMs do not count words exactly; ±10 % around the configured range


@dataclass(frozen=True)
class Issue:
    location: str
    message: str

    def __str__(self) -> str:
        return f"{self.location}: {self.message}"


def normalize_number(raw: str) -> str:
    if re.fullmatch(r"\d{1,3}(,\d{3})+", raw):  # 5,000 -> 5000
        return raw.replace(",", "")
    return raw.replace(",", ".")


def numbers_in(text: str) -> set[str]:
    return {normalize_number(m) for m in NUMBER_RE.findall(text)}


def _strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list | tuple):
        for v in value:
            yield from _strings(v)


def _words(texts: Iterable[str]) -> set[str]:
    return {t.casefold() for text in texts for t in TOKEN_RE.findall(text)}


def source_text(element: BaseModel) -> str:
    """The text a generated passage citing this element may draw facts from."""
    match element:
        case Bullet() | Headline() | Motivation():
            return element.text
        case Skill():
            return element.name
        case Experience():
            head = f"{element.title} {element.company} {element.location} {element.dates}"
            return " ".join([head, *(b.text for b in element.bullets)])
        case Education():
            head = f"{element.school} {element.degree} {element.dates}"
            return " ".join([head, *(b.text for b in element.bullets)])
        case Project():
            return " ".join([element.name, element.description, *(b.text for b in element.bullets)])
    return ""


class _Checker:
    def __init__(self, profile: Profile, offer: JobOffer) -> None:
        self.profile = profile
        self.index = profile.index
        self.known_words = _words(_strings(profile.model_dump())) | _words(
            _strings(offer.model_dump())
        )
        self.offer_numbers = numbers_in(offer.raw_text)
        self.issues: list[Issue] = []

    def add(self, location: str, message: str) -> None:
        self.issues.append(Issue(location, message))

    def text(self, location: str, text: str, allowed_numbers: set[str]) -> None:
        if not text.strip():
            self.add(location, "empty text")
            return
        invented = sorted(numbers_in(text) - allowed_numbers)
        if invented:
            self.add(location, f"numbers not found in the source: {', '.join(invented)}")
        unknown = sorted(self._unknown_names(text))
        if unknown:
            self.add(location, f"names not found in the profile or the offer: {', '.join(unknown)}")

    def _unknown_names(self, text: str) -> set[str]:
        unknown = set()
        for sentence in SENTENCE_SPLIT_RE.split(text):
            for token in TOKEN_RE.findall(sentence)[1:]:  # the first word is capitalised anyway
                is_name = any(c.isupper() for c in token) or any(c in token for c in "+#")
                key = token.casefold()
                if is_name and key not in self.known_words and key not in COMMON_WORDS:
                    unknown.add(token)
        return unknown

    def bullet(self, location: str, bullet: TailoredBullet, kind: type[BaseModel]) -> None:
        source = self.index.get(bullet.source_id)
        if not isinstance(source, kind):
            self.add(location, f"unknown {kind.__name__.lower()} id '{bullet.source_id}'")
            return
        self.text(location, bullet.text, numbers_in(source_text(source)))


def validate_cv(
    cv: TailoredCV, profile: Profile, offer: JobOffer, settings: CVSettings
) -> list[Issue]:
    check = _Checker(profile, offer)
    check.bullet("headline", cv.headline, Headline)

    if len(set(cv.section_order)) != len(cv.section_order):
        check.add("section_order", "duplicate sections")

    sections: list[tuple[str, list[TailoredEntry], type[BaseModel]]] = [
        ("experiences", cv.experiences, Experience),
        ("education", cv.education, Education),
        ("projects", cv.projects, Project),
    ]
    total_bullets = 0
    seen_bullets: set[str] = set()
    for name, entries, kind in sections:
        for i, entry in enumerate(entries):
            location = f"{name}[{i}]"
            parent = check.index.get(entry.source_id)
            if not isinstance(parent, kind):
                check.add(location, f"unknown {kind.__name__.lower()} id '{entry.source_id}'")
                continue
            assert isinstance(parent, Experience | Education | Project)
            own_bullets = {b.id: b for b in parent.bullets}
            if len(entry.bullets) > settings.max_bullets_per_entry:
                check.add(location, f"more than {settings.max_bullets_per_entry} bullets")
            for j, bullet in enumerate(entry.bullets):
                b_location = f"{location}.bullets[{j}]"
                total_bullets += 1
                if bullet.source_id in seen_bullets:
                    check.add(b_location, f"bullet '{bullet.source_id}' used twice")
                seen_bullets.add(bullet.source_id)
                if bullet.source_id not in own_bullets:
                    check.add(
                        b_location,
                        f"'{bullet.source_id}' is not a bullet of '{entry.source_id}'",
                    )
                    continue
                check.text(b_location, bullet.text, numbers_in(own_bullets[bullet.source_id].text))

    if total_bullets > settings.max_bullets:
        check.add("cv", f"{total_bullets} bullets, maximum {settings.max_bullets}")
    for i, skill_id in enumerate(cv.skills):
        if not isinstance(check.index.get(skill_id), Skill):
            check.add(f"skills[{i}]", f"unknown skill id '{skill_id}'")
    return check.issues


def validate_letter(
    letter: CoverLetter, profile: Profile, offer: JobOffer, settings: CoverLetterSettings
) -> list[Issue]:
    check = _Checker(profile, offer)
    motivation_used = False
    for i, paragraph in enumerate(letter.paragraphs):
        location = f"paragraphs[{i}]"
        allowed = set(check.offer_numbers)
        for source_id in paragraph.source_ids:
            source = check.index.get(source_id)
            if source is None:
                check.add(location, f"unknown source id '{source_id}'")
                continue
            motivation_used |= isinstance(source, Motivation)
            allowed |= numbers_in(source_text(source))
        check.text(location, paragraph.text, allowed)

    if profile.motivations and not motivation_used:
        check.add("paragraphs", "no paragraph uses a 'motivations' entry (why Singapore)")

    low = int(settings.min_words * (1 - LETTER_WORD_TOLERANCE))
    high = int(settings.max_words * (1 + LETTER_WORD_TOLERANCE))
    if not low <= letter.word_count <= high:
        check.add(
            "letter",
            f"{letter.word_count} words, expected {settings.min_words}–{settings.max_words}",
        )
    return check.issues
