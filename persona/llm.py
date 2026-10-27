"""Local-LLM persona generation (FR-14, FR-15, SDD 5.5).

A small model served by an OpenAI-compatible endpoint (Ollama by default) writes
candidate persona JSON.  Nothing from the model is trusted: every reply is parsed,
*repaired* where the SDD allows it (transition rows are renormalised), validated
against the same limits as hand-written personas, and rejected if it breaks the
record-size / slot-rate bounds.  A failure at any step falls back to the static
profiles, so persona generation can never leave the app without a usable policy.

The module is deliberately dependency-free: ``urllib`` talks HTTP, so the Windows
build needs no extra wheels.  It is also fully testable against a stub server.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import Config
from persona.model import Persona
from persona.validator import Limits, ValidationOutcome, validate_dict

LOGGER = logging.getLogger("uqab.persona.llm")

STYLES = {
    "balanced": "a mixed browsing session: reading, a little video, occasional idle time",
    "media": "video-heavy browsing with long high-bitrate stretches and short pauses",
    "reading": "text-heavy research: many small records, long idle gaps between pages",
    "chatty": "interactive chat and social feeds: frequent small bursts, short gaps",
}

SYSTEM_PROMPT = (
    "You design network cover-traffic personas for a Tor pluggable transport. "
    "Reply with ONE JSON object and nothing else. No prose, no markdown fences.\n"
    "The object has exactly these keys: id, version, source, description, "
    "floor_slot_ms, start_state, states, transitions.\n"
    "- id: lowercase letters, digits, '-' or '_' only\n"
    "- version: 1\n"
    "- source: \"llm\"\n"
    "- description: one sentence, no URLs or personal data\n"
    "- floor_slot_ms: 500\n"
    "- start_state: the name of one of the states\n"
    "- states: 3 or 4 objects with keys name, record_size, slot_ms, duration_s, "
    "and optionally burst {every_s: [min,max], slots: [min,max], slot_ms}\n"
    "  * record_size is the fixed cover-record size in bytes, between 512 and 4096\n"
    "  * slot_ms is the interval between records, between 20 and 500\n"
    "  * duration_s is [min,max] seconds in that state, between 10 and 300\n"
    "- transitions: an object mapping every state name to an object of "
    "target-state probabilities that sums to 1.0\n"
    "Keep every state's record_size*1000/slot_ms below 262144 bytes per second."
)

FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class LLMError(RuntimeError):
    """The endpoint could not be reached or returned nothing usable."""


@dataclass(slots=True)
class LLMSettings:
    url: str = "http://127.0.0.1:11434"
    model: str = "qwen2.5:1.5b"
    timeout_s: float = 180.0
    variants: int = 3
    out_dir: Path = Path("persona/profiles/generated")
    attempts: int = 3

    @classmethod
    def from_config(cls, cfg: Config) -> LLMSettings:
        return cls(
            url=cfg.get_str("llm.url", "http://127.0.0.1:11434").rstrip("/"),
            model=cfg.get_str("llm.model", "qwen2.5:1.5b"),
            timeout_s=float(cfg.get_int("llm.timeout_s", 180)),
            variants=max(1, cfg.get_int("llm.variants", 3)),
            out_dir=cfg.path("llm.out_dir"),
            attempts=max(1, cfg.get_int("llm.attempts", 3)),
        )


@dataclass(slots=True)
class GenerationResult:
    """What one generation pass produced, including the rejected candidates."""

    personas: list[Persona] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    attempts: int = 0
    model: str = ""
    elapsed_s: float = 0.0
    fell_back: bool = False
    fallback_reason: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.personas)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "attempts": self.attempts,
            "elapsed_s": round(self.elapsed_s, 3),
            "fallback": self.fell_back,
            "fallback_reason": self.fallback_reason,
            "personas": [persona.to_dict() for persona in self.personas],
            "rejected": self.rejected,
        }


def build_messages(style: str, index: int = 0) -> list[dict[str, str]]:
    """Chat messages asking for one persona in the requested style."""
    description = STYLES.get(style.lower(), style)
    variation = (
        "Make it noticeably different from the other variants: vary how often the "
        "session changes state, and how large the idle records are."
    )
    user = (
        f"Design persona number {index + 1} for {description}. "
        f"{variation if index else ''}"
        "Answer with the JSON object only."
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user.strip()},
    ]


def extract_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model reply (tolerates code fences)."""
    candidate = text.strip()
    fenced = FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start < 0 or end <= start:
        raise LLMError("the model reply contains no JSON object")
    try:
        data = json.loads(candidate[start : end + 1])
    except ValueError as exc:
        raise LLMError(f"the model reply is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise LLMError("the model reply is not a JSON object")
    return data


def repair_weights(data: dict[str, Any]) -> dict[str, Any]:
    """Renormalise transition rows and drop unknown targets (SDD 5.5 allows it)."""
    repaired = dict(data)
    states = repaired.get("states")
    names = [
        str(state.get("name"))
        for state in states or []
        if isinstance(state, dict) and state.get("name")
    ]
    transitions = repaired.get("transitions")
    if not isinstance(transitions, dict) or not names:
        return repaired
    rows: dict[str, dict[str, float]] = {}
    for name in names:
        row = transitions.get(name)
        if not isinstance(row, dict):
            rows[name] = {name: 1.0}
            continue
        weights = {
            str(target): float(weight)
            for target, weight in row.items()
            if target in names
            and isinstance(weight, (int, float))
            and not isinstance(weight, bool)
            and weight >= 0
        }
        total = sum(weights.values())
        if total <= 0:
            weights = {name: 1.0}
            total = 1.0
        rows[name] = {target: round(value / total, 6) for target, value in weights.items()}
        # Absorb float drift so the validator's exact-sum rule holds.
        drift = 1.0 - sum(rows[name].values())
        if abs(drift) < 1e-3 and rows[name]:
            first = next(iter(rows[name]))
            rows[name][first] = round(rows[name][first] + drift, 9)
    repaired["transitions"] = rows
    return repaired


def prepare(data: dict[str, Any], *, persona_id: str) -> dict[str, Any]:
    """Force the fields the model must not choose and repair the rest."""
    prepared = repair_weights(data)
    prepared["id"] = persona_id
    prepared["source"] = "llm"
    prepared.setdefault("version", 1)
    prepared.setdefault("floor_slot_ms", 500)
    description = str(prepared.get("description", "") or "").strip()
    prepared["description"] = re.sub(r"https?://\S+", "", description)[:200] or (
        "Generated cover-traffic persona"
    )
    return prepared


class PersonaClient:
    """Minimal client for Ollama's ``/api/chat`` and OpenAI's chat completions."""

    def __init__(self, settings: LLMSettings) -> None:
        self.settings = settings

    # ------------------------------------------------------------------- HTTP
    def _endpoints(self) -> list[tuple[str, dict[str, Any]]]:
        """Candidate endpoints: OpenAI-compatible first, then Ollama's native one."""
        base = self.settings.url
        return [
            (
                f"{base}/v1/chat/completions",
                {"model": self.settings.model, "temperature": 0.8, "stream": False},
            ),
            (
                f"{base}/api/chat",
                {
                    "model": self.settings.model,
                    "stream": False,
                    "options": {"temperature": 0.8},
                },
            ),
        ]

    def chat(self, messages: list[dict[str, str]]) -> str:
        """Send one chat request and return the assistant text."""
        errors: list[str] = []
        for url, body in self._endpoints():
            body = {**body, "messages": messages}
            request = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.settings.timeout_s) as response:
                    raw = response.read().decode("utf-8", "replace")
                return _assistant_text(raw)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                errors.append(f"{url}: {exc}")
            except LLMError as exc:
                errors.append(f"{url}: {exc}")
        raise LLMError("no usable reply from the model endpoint: " + "; ".join(errors))

    # -------------------------------------------------------------- generate
    def generate_one(
        self,
        *,
        style: str,
        index: int,
        limits: Limits,
        persona_id: str,
    ) -> tuple[Persona | None, list[str]]:
        """Ask for, prepare and validate one persona."""
        errors: list[str] = []
        try:
            reply = self.chat(build_messages(style, index))
        except LLMError as exc:
            return None, [str(exc)]
        try:
            data = extract_json(reply)
        except LLMError as exc:
            return None, [str(exc)]
        prepared = prepare(data, persona_id=persona_id)
        outcome: ValidationOutcome = validate_dict(prepared, limits)
        if outcome.ok:
            return outcome.persona, errors
        errors.extend(outcome.errors)
        return None, errors

    def generate(
        self,
        *,
        style: str = "balanced",
        count: int | None = None,
        limits: Limits | None = None,
        base_id: str = "llm",
    ) -> GenerationResult:
        """Generate ``count`` personas, retrying each until it validates."""
        limits = limits or Limits()
        wanted = max(1, count if count is not None else self.settings.variants)
        result = GenerationResult(model=self.settings.model)
        started = time.monotonic()
        for index in range(wanted):
            persona_id = f"{base_id}-{index + 1}"
            for attempt in range(self.settings.attempts):
                result.attempts += 1
                persona, errors = self.generate_one(
                    style=style, index=index + attempt, limits=limits, persona_id=persona_id
                )
                if persona is not None:
                    result.personas.append(persona)
                    break
                result.rejected.extend(errors)
                LOGGER.info("persona %s attempt %d rejected: %s", persona_id, attempt + 1, errors)
                if attempt == self.settings.attempts - 1:
                    LOGGER.warning("giving up on %s after %d attempts", persona_id, attempt + 1)
        result.elapsed_s = time.monotonic() - started
        if not result.personas:
            result.fell_back = True
            result.fallback_reason = "the model produced no valid persona"
        return result


def _assistant_text(raw: str) -> str:
    """Accept both Ollama and OpenAI response envelopes, plus raw JSON."""
    try:
        payload = json.loads(raw)
    except ValueError:
        return raw
    if not isinstance(payload, dict):
        return raw
    message = payload.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"]
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            inner = first.get("message")
            if isinstance(inner, dict) and isinstance(inner.get("content"), str):
                return inner["content"]
            if isinstance(first.get("text"), str):
                return first["text"]
    if isinstance(payload.get("response"), str):
        return payload["response"]
    return raw


def write_personas(personas: Sequence[Persona], out_dir: Path | str) -> list[Path]:
    """Write generated personas into ``out_dir`` (default ``profiles/generated``)."""
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for persona in personas:
        path = target / f"{persona.id}.json"
        path.write_text(persona.to_json(), encoding="utf-8")
        paths.append(path)
    return paths


def static_fallback(limits: Limits, *, reason: str = "") -> GenerationResult:
    """Copy the static profiles when the model is unavailable (FR-15)."""
    from persona.validator import fallback_persona

    persona = fallback_persona(limits)
    return GenerationResult(
        personas=[persona],
        fell_back=True,
        fallback_reason=reason or "the local model was unavailable",
    )


def generate_personas(
    cfg: Config,
    *,
    style: str = "balanced",
    count: int | None = None,
    write: bool = True,
    limits: Limits | None = None,
) -> GenerationResult:
    """Configured entry point used by the CLI, the app and the tests."""
    settings = LLMSettings.from_config(cfg)
    limits = limits or Limits.from_config(cfg)
    client = PersonaClient(settings)
    result = client.generate(style=style, count=count, limits=limits)
    if result.ok and write:
        write_personas(result.personas, settings.out_dir)
    elif not result.ok:
        fallback = static_fallback(limits, reason="; ".join(result.rejected[:3]))
        result.fell_back = True
        result.fallback_reason = fallback.fallback_reason
        result.personas = fallback.personas
    return result


def main(argv: list[str] | None = None) -> int:
    import argparse

    from config import load_config

    parser = argparse.ArgumentParser(description="Generate persona variants with a local LLM")
    parser.add_argument("--config", default=None)
    parser.add_argument("--style", default="balanced", choices=sorted(STYLES))
    parser.add_argument("--count", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true", help="print instead of writing files")
    args = parser.parse_args(argv)

    cfg = load_config(Path(args.config)) if args.config else load_config()
    result = generate_personas(cfg, style=args.style, count=args.count, write=not args.dry_run)
    if result.fell_back:
        print(f"fell back to a static persona: {result.fallback_reason}")
    for persona in result.personas:
        if args.dry_run:
            print(persona.to_json())
        else:
            print(f"wrote {persona.id} ({persona.source})")
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
