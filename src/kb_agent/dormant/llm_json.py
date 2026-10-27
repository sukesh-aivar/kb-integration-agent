"""Parse JSON out of LLM text, tolerating Markdown code fences and prose.

Claude on Bedrock often wraps JSON answers in ```json ... ``` fences, and newer
models may prepend a short explanation before the JSON object. A bare
``json.loads`` fails on both, which silently pushed good model output into the
"unparseable" fallback path (coverage -> GAP, naming -> unparsed_cluster). This
helper strips fences and, as a last resort, extracts the first balanced JSON
object/array from the text.
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def extract_json(raw: str) -> Any:
    """Return the JSON value embedded in ``raw``.

    Tries, in order: the string as-is, the contents of the first ```json``` (or
    plain ```) fence, and finally the first balanced ``{...}`` or ``[...]`` span.
    Raises ``json.JSONDecodeError`` if nothing parses, so callers keep their
    existing try/except fallbacks.
    """
    text = (raw or "").strip()

    # 1. Straight parse.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. Fenced block.
    m = _FENCE_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass

    # 3. First balanced JSON object or array in the text.
    span = _first_json_span(text)
    if span is not None:
        return json.loads(span)

    # Nothing worked: re-raise a JSONDecodeError for the caller's except block.
    return json.loads(text)


def _first_json_span(text: str) -> str | None:
    """Find the first balanced {...} or [...] span, respecting strings."""
    starts = {"{": "}", "[": "]"}
    for i, ch in enumerate(text):
        if ch in starts:
            close = starts[ch]
            depth = 0
            in_str = False
            esc = False
            for j in range(i, len(text)):
                c = text[j]
                if in_str:
                    if esc:
                        esc = False
                    elif c == "\\":
                        esc = True
                    elif c == '"':
                        in_str = False
                    continue
                if c == '"':
                    in_str = True
                elif c == ch:
                    depth += 1
                elif c == close:
                    depth -= 1
                    if depth == 0:
                        return text[i : j + 1]
            return None
    return None
