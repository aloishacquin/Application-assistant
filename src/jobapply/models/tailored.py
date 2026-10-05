"""LLM outputs for generated documents (SPEC section 7.3).

Every piece of generated text carries the id(s) of the profile facts it comes from, so the
validator can check it. Factual fields (job titles, companies, dates, schools) are never
generated: the renderer takes them from the profile through `source_id`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Section = Literal["experience", "education", "projects", "skills"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TailoredBullet(_Strict):
    source_id: str = Field(description="Id of the profile bullet this text rephrases.")
    text: str


class TailoredEntry(_Strict):
    source_id: str = Field(description="Id of the profile experience, education or project.")
    bullets: list[TailoredBullet]


class TailoredCV(_Strict):
    headline: TailoredBullet = Field(description="source_id is a headline_variants id.")
    section_order: list[Section] = Field(min_length=1)
    experiences: list[TailoredEntry]
    education: list[TailoredEntry]
    projects: list[TailoredEntry]
    skills: list[str] = Field(description="Ids of the profile skills to show, most relevant first.")
    keywords_covered: list[str] = Field(description="Offer keywords the CV now contains.")


class LetterParagraph(_Strict):
    text: str
    source_ids: list[str] = Field(description="Ids of the profile facts used in this paragraph.")


class CoverLetter(_Strict):
    subject: str
    greeting: str
    paragraphs: list[LetterParagraph] = Field(min_length=3, max_length=4)
    closing: str

    @property
    def word_count(self) -> int:
        return sum(len(p.text.split()) for p in self.paragraphs)

    def as_text(self, signature: str) -> str:
        body = "\n\n".join(p.text for p in self.paragraphs)
        return f"{self.greeting}\n\n{body}\n\n{self.closing}\n{signature}\n"
