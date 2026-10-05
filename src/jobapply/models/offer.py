"""Structured job offer.

`JobOfferExtraction` holds the fields the LLM fills in; its JSON schema is injected into the
extraction prompt and enforced through structured outputs. `JobOffer` adds the fields the code
sets itself (source URL, raw text), so the model never has to copy the whole offer back.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Sector = Literal["financial_services", "other"]
Seniority = Literal["internship", "entry", "junior", "mid", "senior", "lead", "unknown"]


class JobOfferExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(description="Job title, as written in the offer.")
    company: str = Field(description="Hiring company name.")
    location: str = Field(description="Work location as written (city, area, remote/hybrid).")
    seniority: Seniority
    required_skills: list[str] = Field(description="Skills explicitly required.")
    nice_to_have: list[str] = Field(description="Skills listed as optional / a plus.")
    keywords: list[str] = Field(
        description="Important terms copied verbatim from the offer (for ATS matching)."
    )
    responsibilities: list[str]
    min_years_experience: int | None = Field(
        description="Minimum years of experience required; 0 if open to fresh graduates; "
        "null if not stated."
    )
    salary_min_sgd: int | None = Field(description="Monthly salary in SGD, or null if absent.")
    salary_max_sgd: int | None = Field(description="Monthly salary in SGD, or null if absent.")
    sector: Sector = Field(description="financial_services for banks, insurers, asset managers.")
    contact_email: str | None = Field(description="Application email if given, else null.")
    language: str = Field(description="ISO 639-1 code of the offer's language, e.g. 'en'.")

    @model_validator(mode="after")
    def _ranges(self) -> Self:
        for name in ("salary_min_sgd", "salary_max_sgd"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.min_years_experience is not None and not 0 <= self.min_years_experience <= 30:
            raise ValueError("min_years_experience must be between 0 and 30")
        if (
            self.salary_min_sgd is not None
            and self.salary_max_sgd is not None
            and self.salary_min_sgd > self.salary_max_sgd
        ):
            raise ValueError("salary_min_sgd must be <= salary_max_sgd")
        return self


class JobOffer(JobOfferExtraction):
    url: str | None = None
    raw_text: str
