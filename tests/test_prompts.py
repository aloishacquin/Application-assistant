from pathlib import Path

import pytest

from jobapply.llm.prompts import PromptError, load_prompt


def test_real_prompts_load(project_root: Path) -> None:
    for name in ("extract_offer", "retry_feedback"):
        prompt = load_prompt(name, project_root / "prompts")
        assert prompt.version >= 1
        assert not prompt.body.startswith("---")


def test_render_substitutes_and_keeps_braces(tmp_path: Path) -> None:
    (tmp_path / "p.md").write_text('---\nversion: 3\n---\nSchema: {"a": 1}\n$x\n', "utf-8")
    prompt = load_prompt("p", tmp_path)
    assert prompt.version == 3
    assert prompt.render(x="hello") == 'Schema: {"a": 1}\nhello\n'


def test_missing_variable(tmp_path: Path) -> None:
    (tmp_path / "p.md").write_text("---\nversion: 1\n---\n$x $y\n", "utf-8")
    with pytest.raises(PromptError, match="Variable manquante"):
        load_prompt("p", tmp_path).render(x="1")


@pytest.mark.parametrize(
    "content",
    ["no front matter\n", "---\ndescription: x\n---\nbody\n", "---\nversion: zero\n---\nbody\n"],
)
def test_invalid_front_matter(tmp_path: Path, content: str) -> None:
    (tmp_path / "p.md").write_text(content, "utf-8")
    with pytest.raises(PromptError):
        load_prompt("p", tmp_path)


def test_missing_prompt(tmp_path: Path) -> None:
    with pytest.raises(PromptError, match="introuvable"):
        load_prompt("nope", tmp_path)
