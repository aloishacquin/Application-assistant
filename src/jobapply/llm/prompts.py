"""Loading and rendering of versioned prompt files from prompts/.

A prompt file is Markdown with a YAML front matter holding at least an integer `version`:

    ---
    version: 1
    description: ...
    ---
    Body with $placeholders (string.Template syntax, so JSON braces need no escaping).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from string import Template

import yaml


class PromptError(Exception):
    """Raised when a prompt file is missing, malformed or rendered with missing variables."""


@dataclass(frozen=True)
class Prompt:
    name: str
    version: int
    body: str

    def render(self, **variables: str) -> str:
        try:
            return Template(self.body).substitute(variables)
        except KeyError as exc:
            raise PromptError(f"Variable manquante pour le prompt '{self.name}' : {exc}") from exc
        except ValueError as exc:
            raise PromptError(f"Placeholder invalide dans le prompt '{self.name}' : {exc}") from exc


def load_prompt(name: str, prompts_dir: Path) -> Prompt:
    path = prompts_dir / f"{name}.md"
    if not path.is_file():
        raise PromptError(f"Prompt introuvable : {path}")
    text = path.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    if not text.startswith("---") or len(parts) < 3:
        raise PromptError(f"{path} : front matter YAML manquant (bloc --- ... --- en tête)")
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as exc:
        raise PromptError(f"{path} : front matter YAML invalide : {exc}") from exc
    version = meta.get("version") if isinstance(meta, dict) else None
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise PromptError(f"{path} : 'version' doit être un entier >= 1")
    return Prompt(name=name, version=version, body=parts[2].strip() + "\n")
