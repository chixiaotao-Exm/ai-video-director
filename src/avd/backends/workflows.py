"""Strict JSON template loading for ComfyUI API workflows."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any


_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_EXACT_PLACEHOLDER_RE = re.compile(r"^\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}$")


class WorkflowTemplateError(ValueError):
    """A workflow template could not be rendered strictly."""


@dataclass(frozen=True)
class FileRef:
    """A path or ComfyUI file name inserted as a string into a template."""

    value: str | Path

    def as_string(self) -> str:
        if isinstance(self.value, Path):
            return self.value.as_posix()
        return str(self.value)


DEFAULT_WORKFLOWS_DIR = Path(__file__).resolve().parents[3] / "workflows"


def template_placeholders(template: Any) -> set[str]:
    """Return placeholder names found anywhere in a JSON-like template."""

    found: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, str):
            found.update(match.group(1) for match in _PLACEHOLDER_RE.finditer(value))
        elif isinstance(value, Mapping):
            for key, nested in value.items():
                visit(key)
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(template)
    return found


def render_workflow(
    template: Mapping[str, Any],
    values: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Substitute placeholders while preserving typed JSON values.

    A placeholder occupying an entire JSON string receives its value's type,
    allowing integer seeds and float parameters.  Placeholders embedded in a
    larger string are converted to text.  ``Path`` and :class:`FileRef` values
    become POSIX strings suitable for ComfyUI API inputs.
    """

    if not isinstance(template, Mapping):
        raise WorkflowTemplateError("workflow template must be a JSON object")
    substitutions = dict(values or {})
    expected = template_placeholders(template)
    supplied = set(substitutions)
    missing = sorted(expected - supplied)
    unused = sorted(supplied - expected)
    if missing or unused:
        problems: list[str] = []
        if missing:
            problems.append(f"missing placeholders: {', '.join(missing)}")
        if unused:
            problems.append(f"unused values: {', '.join(unused)}")
        raise WorkflowTemplateError("; ".join(problems))

    def value_as_text(value: Any) -> str:
        if isinstance(value, FileRef):
            return value.as_string()
        if isinstance(value, Path):
            return value.as_posix()
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    def substitute(value: Any) -> Any:
        if isinstance(value, str):
            exact = _EXACT_PLACEHOLDER_RE.fullmatch(value)
            if exact:
                replacement = deepcopy(substitutions[exact.group(1)])
                if isinstance(replacement, FileRef):
                    return replacement.as_string()
                if isinstance(replacement, Path):
                    return replacement.as_posix()
                return replacement
            return _PLACEHOLDER_RE.sub(
                lambda match: value_as_text(substitutions[match.group(1)]), value
            )
        if isinstance(value, Mapping):
            return {substitute(key): substitute(nested) for key, nested in value.items()}
        if isinstance(value, list):
            return [substitute(nested) for nested in value]
        return deepcopy(value)

    rendered = substitute(template)
    if not isinstance(rendered, dict):  # pragma: no cover - guarded above
        raise WorkflowTemplateError("rendered workflow must be a JSON object")
    return rendered


def load_workflow(
    name: str | Path,
    values: Mapping[str, Any] | None = None,
    *,
    template_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Load a JSON template and render it with strict placeholder checking."""

    template_name = Path(name)
    if template_name.suffix.lower() != ".json":
        template_name = template_name.with_suffix(".json")
    if template_name.name != template_name.as_posix() or ".." in template_name.parts:
        raise WorkflowTemplateError(f"invalid workflow template name: {name!r}")
    directory = Path(template_dir) if template_dir is not None else DEFAULT_WORKFLOWS_DIR
    path = (directory / template_name).resolve()
    try:
        path.relative_to(directory.resolve())
    except ValueError as exc:
        raise WorkflowTemplateError(f"workflow template is outside template directory: {name!r}") from exc
    if not path.is_file():
        raise FileNotFoundError(f"workflow template not found: {path}")
    try:
        template = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorkflowTemplateError(f"invalid workflow JSON in {path}: {exc}") from exc
    return render_workflow(template, values)


# Friendly aliases for callers that prefer template terminology.
load_template = load_workflow
render_template = render_workflow


__all__ = [
    "DEFAULT_WORKFLOWS_DIR",
    "FileRef",
    "WorkflowTemplateError",
    "load_template",
    "load_workflow",
    "render_template",
    "render_workflow",
    "template_placeholders",
]
