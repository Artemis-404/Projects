"""Configuration loading for UQAB.

The SRS fixes the configuration format as ``config/config.yaml`` (SDD 5.8).  The
client engine must run on a bare Python install with no third-party packages, so
this module ships a small, strict reader for the YAML subset that the UQAB
configuration actually uses: nested block mappings, block sequences of scalars,
quoted strings, integers, floats, booleans, null and comments.

Using ``json``-compatible semantics keeps the file readable for humans while the
engine stays dependency free (NFR-14).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "config" / "config.yaml"


class ConfigError(Exception):
    """Raised when the configuration file is missing or malformed."""


_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")


def _strip_comment(line: str) -> str:
    """Remove a trailing ``#`` comment that is not inside quotes."""
    out: list[str] = []
    quote: str | None = None
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in {"'", '"'}:
            quote = ch
            out.append(ch)
            continue
        if ch == "#":
            break
        out.append(ch)
    return "".join(out).rstrip()


def _split_key(text: str) -> tuple[str, str] | None:
    """Split ``key: value`` (the value may be empty). Returns None if not a mapping entry."""
    quote: str | None = None
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in {"'", '"'}:
            quote = ch
            continue
        if ch == ":" and (i + 1 == len(text) or text[i + 1] == " "):
            return text[:i].strip(), text[i + 1 :].strip()
    return None


def _scalar(text: str) -> Any:
    text = text.strip()
    if text == "":
        return None
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    low = text.lower()
    if low in {"true", "yes", "on"}:
        return True
    if low in {"false", "no", "off"}:
        return False
    if low in {"null", "none", "~"}:
        return None
    if low == "[]":
        return []
    if low == "{}":
        return {}
    if _INT_RE.match(text):
        return int(text)
    if _FLOAT_RE.match(text) and ("." in text or "e" in low):
        return float(text)
    return text


def _parse_block(lines: list[tuple[int, str]], pos: int, indent: int) -> tuple[Any, int]:
    """Parse a block that starts at ``lines[pos]`` and is indented at ``indent``."""
    if pos >= len(lines):
        return {}, pos
    first = lines[pos][1]
    if not first.startswith("-") and _split_key(first) is None:
        # A bare scalar used as a value on its own line, e.g. ``[]``.
        return _scalar(first), pos + 1
    is_list = first == "-" or first.startswith("- ")

    if is_list:
        items: list[Any] = []
        while pos < len(lines) and lines[pos][0] == indent and lines[pos][1].startswith("-"):
            body = lines[pos][1][1:].strip()
            pos += 1
            if body == "":
                if pos < len(lines) and lines[pos][0] > indent:
                    value, pos = _parse_block(lines, pos, lines[pos][0])
                else:
                    value = None
            else:
                value = _scalar(body)
            items.append(value)
        return items, pos

    mapping: dict[str, Any] = {}
    while pos < len(lines) and lines[pos][0] == indent:
        text = lines[pos][1]
        if text.startswith("-"):
            raise ConfigError(f"unexpected sequence item inside a mapping: {text!r}")
        pair = _split_key(text)
        if pair is None:
            raise ConfigError(f"expected 'key: value', got {text!r}")
        key, value_text = pair
        pos += 1
        if value_text == "":
            if pos < len(lines) and lines[pos][0] > indent:
                value, pos = _parse_block(lines, pos, lines[pos][0])
            else:
                value = {}
        else:
            value = _scalar(value_text)
        mapping[key] = value
    if pos < len(lines) and lines[pos][0] > indent:
        raise ConfigError(f"bad indentation near {lines[pos][1]!r}")
    return mapping, pos


def dump_yaml(data: dict[str, Any], indent: int = 0) -> str:
    """Emit the same YAML subset this module reads (for effective config files)."""
    pad = " " * indent
    lines: list[str] = []
    for key, value in data.items():
        if isinstance(value, dict):
            if not value:
                lines.append(f"{pad}{key}: {{}}")
                continue
            lines.append(f"{pad}{key}:")
            lines.append(dump_yaml(value, indent + 2).rstrip("\n"))
            continue
        if isinstance(value, list):
            if not value:
                lines.append(f"{pad}{key}: []")
                continue
            lines.append(f"{pad}{key}:")
            for item in value:
                lines.append(f"{pad}  - {_render_scalar(item)}")
            continue
        lines.append(f"{pad}{key}: {_render_scalar(value)}")
    return "\n".join(lines) + "\n"


def _render_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    text = str(value)
    if text == "" or text.strip() != text or any(ch in text for ch in ":#\n\"{}\"'"):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def parse_yaml(text: str) -> dict[str, Any]:
    """Parse the UQAB YAML subset into plain Python objects."""
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        stripped = _strip_comment(raw.replace("\t", "  "))
        if not stripped.strip():
            continue
        if stripped.strip() == "---":
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        lines.append((indent, stripped.strip()))
    if not lines:
        return {}
    result, pos = _parse_block(lines, 0, lines[0][0])
    if pos != len(lines):
        raise ConfigError("trailing content after the document root")
    if not isinstance(result, dict):
        raise ConfigError("the configuration root must be a mapping")
    return result


class Config:
    """Read-only dotted access to the configuration tree."""

    def __init__(self, data: dict[str, Any], root: Path | None = None) -> None:
        self._data = data
        self.root = root or REPO_ROOT

    @classmethod
    def load(cls, path: str | Path | None = None) -> Config:
        cfg_path = Path(path) if path else DEFAULT_CONFIG
        if not cfg_path.is_absolute():
            cfg_path = REPO_ROOT / cfg_path
        try:
            text = cfg_path.read_text(encoding="utf-8")
        except OSError as exc:  # pragma: no cover - environment error
            raise ConfigError(f"cannot read configuration {cfg_path}: {exc}") from exc
        return cls(parse_yaml(text), root=REPO_ROOT)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, dotted: str) -> Any:
        sentinel = object()
        value = self.get(dotted, sentinel)
        if value is sentinel:
            raise ConfigError(f"missing required configuration key: {dotted}")
        return value

    def get_int(self, dotted: str, default: int | None = None) -> int:
        value = self.get(dotted, default)
        if value is None:
            raise ConfigError(f"missing integer configuration key: {dotted}")
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{dotted} must be an integer, got {value!r}") from exc

    def get_float(self, dotted: str, default: float | None = None) -> float:
        value = self.get(dotted, default)
        if value is None:
            raise ConfigError(f"missing numeric configuration key: {dotted}")
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{dotted} must be a number, got {value!r}") from exc

    def get_bool(self, dotted: str, default: bool = False) -> bool:
        value = self.get(dotted, default)
        return bool(value)

    def get_str(self, dotted: str, default: str = "") -> str:
        value = self.get(dotted, default)
        return "" if value is None else str(value)

    def path(self, dotted: str, default: str = "") -> Path:
        """Resolve a configured path relative to the repository root."""
        raw = self.get_str(dotted, default)
        p = Path(raw)
        return p if p.is_absolute() else (self.root / p)

    def as_dict(self) -> dict[str, Any]:
        return self._data

    def with_overrides(self, **overrides: Any) -> Config:
        """Return a copy with dotted keys overridden (used by CLI flags and tests)."""
        import copy

        data = copy.deepcopy(self._data)
        for dotted, value in overrides.items():
            node = data
            parts = dotted.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value
        return Config(data, root=self.root)

    def ensure_dirs(self) -> None:
        for dotted in ("paths.run", "paths.data", "paths.data.sqlite"):
            try:
                target = self.path(dotted)
            except ConfigError:
                continue
            if target.suffix:
                target = target.parent
            target.mkdir(parents=True, exist_ok=True)
        (self.path("paths.run") / "logs").mkdir(parents=True, exist_ok=True)
        self.path("profile.path").parent.mkdir(parents=True, exist_ok=True)


PATH_KEYS = (
    "tls.ca",
    "tls.cert",
    "tls.key",
    "tls.server_cert",
    "tls.server_key",
    "persona.profiles_dir",
    "profile.path",
    "profile.opt_in_flag",
    "llm.out_dir",
    "paths.run",
    "paths.data",
    "paths.sqlite",
)


def effective_config(cfg: Config) -> dict[str, Any]:
    """Absolute-path copy of the configuration, safe to hand to child processes."""
    import copy

    data = copy.deepcopy(cfg.as_dict())
    for dotted in PATH_KEYS:
        node: Any = data
        parts = dotted.split(".")
        for part in parts[:-1]:
            if not isinstance(node, dict):
                break
            node = node.setdefault(part, {})
        if isinstance(node, dict):
            node[parts[-1]] = str(cfg.path(dotted))
    return data


def write_effective_config(cfg: Config, path: str | Path) -> Path:
    """Write the effective configuration for child processes (lab, Tor's PT)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dump_yaml(effective_config(cfg)), encoding="utf-8")
    return target


def load_config(path: str | Path | None = None) -> Config:
    """Load the UQAB configuration from ``path`` or the repository default."""
    return Config.load(path)


__all__ = [
    "Config",
    "ConfigError",
    "DEFAULT_CONFIG",
    "REPO_ROOT",
    "dump_yaml",
    "effective_config",
    "load_config",
    "parse_yaml",
    "write_effective_config",
]

if __name__ == "__main__":  # pragma: no cover - manual inspection helper
    import json
    import sys

    print(json.dumps(load_config(os.environ.get("UQAB_CONFIG", sys.argv[1] if len(sys.argv) > 1 else None)).as_dict(), indent=2))
