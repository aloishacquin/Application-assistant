"""Knowledge base models: the single source of facts the LLM is allowed to use.

Every reusable element carries a stable `id`; ids must be unique across the whole profile so
that generated content can always be traced back to exactly one source (SPEC section 6.3).
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from jobapply.config import ConfigError, format_validation_error, read_yaml

Id = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")]
NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
SkillLevel = Literal["beginner", "intermediate", "advanced", "expert"]


class ProfileError(ConfigError):
    """Raised when profile.yaml is missing or invalid."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Identified(_Strict):
    id: Id


class Bullet(_Identified):
    text: NonEmpty
    tags: list[str] = Field(default_factory=list)
    metrics: bool = False


class Identity(_Strict):
    name: NonEmpty
    email: NonEmpty
    phone: NonEmpty
    location: NonEmpty
    links: dict[str, str] = Field(default_factory=dict)
    birth_year: int = Field(ge=1940, le=2015, description="Only used for the EP threshold.")


class Headline(_Identified):
    text: NonEmpty
    tags: list[str] = Field(default_factory=list)


class Education(_Identified):
    school: NonEmpty
    degree: NonEmpty
    dates: NonEmpty
    bullets: list[Bullet] = Field(default_factory=list)


class Experience(_Identified):
    title: NonEmpty
    company: NonEmpty
    location: NonEmpty
    dates: NonEmpty
    bullets: list[Bullet] = Field(default_factory=list)


class Project(_Identified):
    name: NonEmpty
    description: NonEmpty
    bullets: list[Bullet] = Field(default_factory=list)


class Skill(_Identified):
    name: NonEmpty
    level: SkillLevel | None = None
    tags: list[str] = Field(default_factory=list)


class Language(_Strict):
    name: NonEmpty
    level: NonEmpty


class Motivation(_Identified):
    text: NonEmpty


class Constraints(_Strict):
    min_salary_sgd: int | None = Field(default=None, gt=0)
    target_roles: list[NonEmpty] = Field(default_factory=list)
    years_experience: float = Field(default=0, ge=0, description="Full-time, internships excluded.")
    available_from: Annotated[str, StringConstraints(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]

    @property
    def available_from_date(self) -> date:
        """First day of the availability month."""
        year, month = self.available_from.split("-")
        return date(int(year), int(month), 1)


class Profile(_Strict):
    identity: Identity
    headline_variants: list[Headline] = Field(min_length=1)
    education: list[Education] = Field(default_factory=list)
    experiences: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    skills: list[Skill] = Field(default_factory=list)
    languages: list[Language] = Field(default_factory=list)
    motivations: list[Motivation] = Field(default_factory=list)
    constraints: Constraints

    def _elements(self) -> list[tuple[str, _Identified]]:
        """(location, element) for every element that has an id."""
        items: list[tuple[str, _Identified]] = []
        for name in ("headline_variants", "skills", "motivations"):
            items += [(f"{name}[{i}]", el) for i, el in enumerate(getattr(self, name))]
        for name in ("education", "experiences", "projects"):
            for i, parent in enumerate(getattr(self, name)):
                items.append((f"{name}[{i}]", parent))
                items += [
                    (f"{name}[{i}].bullets[{j}]", bullet) for j, bullet in enumerate(parent.bullets)
                ]
        return items

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        elements = self._elements()
        counts = Counter(el.id for _, el in elements)
        duplicates = [f"{loc} : id dupliqué '{el.id}'" for loc, el in elements if counts[el.id] > 1]
        if duplicates:
            raise ValueError("ids non uniques : " + " ; ".join(duplicates))
        return self

    @property
    def index(self) -> dict[str, _Identified]:
        """Every identified element, by id."""
        return {el.id: el for _, el in self._elements()}

    def for_llm(self) -> str:
        """YAML of the profile without identity and salary data, which prompts do not need."""
        data = self.model_dump(exclude={"identity": True, "constraints": {"min_salary_sgd"}})
        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)

    @property
    def bullets(self) -> dict[str, Bullet]:
        """Every bullet (education, experiences, projects), by id."""
        return {k: v for k, v in self.index.items() if isinstance(v, Bullet)}


def load_profile(path: Path) -> Profile:
    """Load and strictly validate a profile YAML file."""
    try:
        data = read_yaml(path)
    except ConfigError as exc:
        raise ProfileError(str(exc)) from exc
    try:
        return Profile.model_validate(data)
    except ValidationError as exc:
        raise ProfileError(format_validation_error(path, exc)) from exc
