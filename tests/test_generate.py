import copy
from datetime import date
from pathlib import Path

import pytest

from jobapply.ats.check import check_pdf, extract_text
from jobapply.config import AppConfig, CoverLetterSettings, CVSettings
from jobapply.generate.cover_letter import generate_cover_letter
from jobapply.generate.cv import generate_cv
from jobapply.generate.documents import apply_edits, build_document, editable_fields
from jobapply.generate.render import RenderError, build_cv_data, render_pdf
from jobapply.generate.runner import GenerationError
from jobapply.models.db import DocumentKind
from jobapply.models.offer import JobOffer
from jobapply.models.profile import Profile, load_profile
from jobapply.models.tailored import CoverLetter, TailoredCV
from tests.conftest import (
    COVER_LETTER,
    OFFER_NAMES,
    TAILORED_CV,
    FakeLLM,
    expected_offer,
    full_responder,
    offer_text,
)


@pytest.fixture(scope="module")
def profile() -> Profile:
    return load_profile(Path(__file__).parent / "fixtures" / "profile.yaml")


@pytest.fixture(scope="module")
def offer() -> JobOffer:
    return JobOffer(**expected_offer(OFFER_NAMES[0]), raw_text=offer_text(OFFER_NAMES[0]))


def invented_cv() -> dict:
    data = copy.deepcopy(TAILORED_CV)
    data["experiences"][0]["bullets"][0]["text"] = "Built pipelines on Kubernetes for 9 TB of data"
    return data


# --- Generation loop ----------------------------------------------------------------------


def test_generate_cv(profile, offer, tmp_config: AppConfig) -> None:
    llm = FakeLLM(full_responder)
    result = generate_cv(
        profile, offer, llm=llm, prompts_dir=tmp_config.paths.prompts_dir, settings=CVSettings()
    )
    assert result.value.headline.source_id == "hl-data"
    assert result.attempts == 1
    assert result.prompt_version == 1
    prompt = llm.prompts[0]
    assert "exp-airbus-1" in prompt and "Lion City Bank" in prompt
    assert "14 bullets in total" in prompt
    for private in ("camille.martin@example.com", "birth_year", "min_salary_sgd"):
        assert private not in prompt


def test_regeneration_with_feedback(profile, offer, tmp_config: AppConfig) -> None:
    answers = iter([invented_cv(), TAILORED_CV])
    llm = FakeLLM(lambda prompt: next(answers))
    result = generate_cv(
        profile, offer, llm=llm, prompts_dir=tmp_config.paths.prompts_dir, settings=CVSettings()
    )
    assert result.attempts == 2
    assert len(result.calls) == 2
    feedback = llm.prompts[1]
    assert "Kubernetes" in feedback and "numbers not found in the source: 9" in feedback


def test_explicit_failure_after_regeneration(profile, offer, tmp_config: AppConfig) -> None:
    llm = FakeLLM(lambda prompt: invented_cv())
    with pytest.raises(GenerationError) as exc_info:
        generate_cv(
            profile, offer, llm=llm, prompts_dir=tmp_config.paths.prompts_dir, settings=CVSettings()
        )
    assert len(llm.prompts) == 2
    assert any("Kubernetes" in str(i) for i in exc_info.value.issues)
    assert "CV rejeté" in str(exc_info.value)


def test_generate_cover_letter(profile, offer, tmp_config: AppConfig) -> None:
    llm = FakeLLM(full_responder)
    result = generate_cover_letter(
        profile,
        offer,
        llm=llm,
        prompts_dir=tmp_config.paths.prompts_dir,
        settings=CoverLetterSettings(),
    )
    assert result.value.paragraphs[2].source_ids == ["exp-airbus-3", "mot-sg"]
    assert "Candidate name: Camille Martin" in llm.prompts[0]


# --- Rendering and ATS --------------------------------------------------------------------


def test_cv_data_uses_profile_facts(profile) -> None:
    data = build_cv_data(TailoredCV.model_validate(TAILORED_CV), profile)
    assert data["name"] == "Camille Martin"
    assert [s["title"] for s in data["sections"]] == [
        "Experience",
        "Education",
        "Projects",
        "Skills",
    ]
    airbus = data["sections"][0]["entries"][0]
    assert airbus["title"] == "Data Engineering Intern — Airbus"  # from the profile, not the LLM
    assert airbus["dates"] == "Feb 2025 – Aug 2025"
    assert data["sections"][3]["rows"][0]["value"] == "Python, SQL, PySpark, Airflow, Docker"
    assert "2002" not in str(data)  # birth year never rendered


def test_cv_without_projects_section(profile) -> None:
    cv = TailoredCV.model_validate({**TAILORED_CV, "projects": []})
    titles = [s["title"] for s in build_cv_data(cv, profile)["sections"]]
    assert "Projects" not in titles


@pytest.fixture
def cv_pdf(profile, offer, tmp_config: AppConfig, tmp_path: Path) -> tuple[Path, dict]:
    output = tmp_path / "cv_v1.pdf"
    payload = build_document(
        tmp_config,
        DocumentKind.CV,
        TailoredCV.model_validate(TAILORED_CV),
        profile,
        offer,
        output,
        [],
        date(2026, 10, 4),
    )
    return output, payload


def test_cv_pdf_passes_ats(cv_pdf) -> None:
    output, payload = cv_pdf
    assert output.is_file() and output.with_suffix(".json").is_file()
    ats = payload["ats"]
    assert ats["pages"] == 1
    assert ats["extractable"] and ats["order_ok"]
    assert ats["missing_snippets"] == []
    assert ats["required_present"] == ["Python", "SQL", "Spark", "Airflow"]
    assert payload["ats_warnings"] == []
    text, _ = extract_text(output)
    assert "Wrote data-quality checks that caught 30% more sensor anomalies" in " ".join(
        text.split()
    )


def test_ats_detects_problems(cv_pdf) -> None:
    output, _ = cv_pdf
    report = check_pdf(
        output,
        snippets=["Education", "Experience", "A sentence that is not in the CV"],
        max_pages=0,
        required_terms=["Python", "Kafka"],
    )
    assert not report.order_ok  # "Experience" comes before "Education" in the PDF
    assert report.missing_snippets == ["A sentence that is not in the CV"]
    assert report.required_missing == ["Kafka"]
    assert not report.ok
    assert len(report.warnings) == 4


def test_unreadable_pdf(tmp_path: Path) -> None:
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a pdf")
    report = check_pdf(broken, snippets=[], max_pages=1)
    assert report.pages == 0 and not report.extractable


def test_letter_document(profile, offer, tmp_config: AppConfig, tmp_path: Path) -> None:
    payload = build_document(
        tmp_config,
        DocumentKind.LETTER,
        CoverLetter.model_validate(COVER_LETTER),
        profile,
        offer,
        tmp_path / "letter_v1.pdf",
        [],
        date(2026, 10, 4),
    )
    assert payload["ats"]["pages"] == 1
    assert payload["ats_warnings"] == []
    assert payload["word_count"] == 235
    assert payload["text"].startswith("Dear Hiring Manager,")
    assert payload["text"].rstrip().endswith("Kind regards,\nCamille Martin")
    text, _ = extract_text(tmp_path / "letter_v1.pdf")
    assert "4 October 2026" in text and "Lion City Bank" in text


def test_render_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.typ"
    bad.write_text("#let x = (", encoding="utf-8")
    with pytest.raises(RenderError, match=r"bad\.typ"):
        render_pdf(bad, {}, tmp_path / "out.pdf")


# --- Manual edits -------------------------------------------------------------------------


def test_editable_fields() -> None:
    cv_fields = editable_fields(DocumentKind.CV, TAILORED_CV)
    assert cv_fields[0].path == "headline.text"
    assert len(cv_fields) == 1 + 6  # headline + 6 bullets
    assert cv_fields[1].path == "experiences.0.bullets.0.text"
    letter_fields = [f.path for f in editable_fields(DocumentKind.LETTER, COVER_LETTER)]
    assert letter_fields[:2] == ["subject", "greeting"]
    assert letter_fields[-1] == "closing"
    assert "paragraphs.3.text" in letter_fields


def test_apply_edits() -> None:
    updated = apply_edits(TAILORED_CV, {"experiences.0.bullets.1.text": "  New text  "})
    assert updated["experiences"][0]["bullets"][1]["text"] == "New text"
    assert TAILORED_CV["experiences"][0]["bullets"][1]["text"] != "New text"  # copy, not in place
    assert apply_edits(COVER_LETTER, {"closing": "Best regards,"})["closing"] == "Best regards,"


@pytest.mark.parametrize("path", ["experiences.0.source_id", "skills.0", "nope", "headline"])
def test_apply_edits_rejects_other_fields(path: str) -> None:
    with pytest.raises(ValueError, match="non modifiable"):
        apply_edits(TAILORED_CV, {path: "x"})
