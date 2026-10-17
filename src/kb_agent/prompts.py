"""Versioned prompt loading (spec 6: the docstring/template IS the prompt).

Prompt templates live in ``config/prompts/<name>.<version>.md`` so they are
versioned and reviewable as data, and the pinned version is recorded in the run
manifest (spec 2.3). Each template begins with a ``<<KIND:...>>`` marker that the
fake LLM reads to choose a response shape; it is harmless to the real model.

Templates use ``str.format`` placeholders. Rendering NEVER executes template
content and treats inserted ticket/document text as untrusted data (spec 7.4).
"""

from __future__ import annotations

from functools import lru_cache

from kb_agent.config import CONFIG_DIR

PROMPTS_DIR = CONFIG_DIR / "prompts"


@lru_cache(maxsize=32)
def load_prompt(name: str, version: str = "v1") -> str:
    """Load a prompt template's raw text.

    Args:
        name: Base name (e.g. ``"naming"``).
        version: Version tag (e.g. ``"v1"``).

    Returns:
        The template text, including its ``<<KIND:...>>`` marker.
    """
    path = PROMPTS_DIR / f"{name}.{version}.md"
    return path.read_text(encoding="utf-8")


def render_prompt(name: str, version: str = "v1", **fields: str) -> str:
    """Load and fill a prompt template's ``{placeholder}`` slots.

    We deliberately do NOT use ``str.format`` on the whole template: the templates
    contain literal JSON braces (e.g. ``{"intent": str}``) in their instruction
    comments, which ``str.format`` would misread as fields. Instead we replace
    only the exact ``{field}`` tokens we were given. This also means untrusted
    inserted values can never be interpreted as format directives (spec 7.4).
    """
    template = load_prompt(name, version)
    rendered = template
    for key, value in fields.items():
        rendered = rendered.replace("{" + key + "}", value)
    return rendered


def prompt_version(name: str, version: str = "v1") -> str:
    """Return the recorded version identifier for the manifest (e.g. 'naming/v1')."""
    return f"{name}/{version}"
