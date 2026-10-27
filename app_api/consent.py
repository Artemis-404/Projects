"""Clearnet-learning consent state (FR-30, SDD 5.7).

Clearnet learning is off by default.  The opt-in flag file that
``profile/capture_features.py`` checks is only written when the user accepts the
consent text in the app, and withdrawing consent removes both the flag and
``profile.json``.  The consent text version is stored so a changed text can
require a fresh acceptance later.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CONSENT_TEXT_VERSION = "1.1"

CONSENT_TEXT = """\
UQAB clearnet learning - consent

If you turn this on, UQAB may read a capture file that you supply, containing
metadata from your own ordinary (non-Tor) browsing, and turn it into aggregate
statistics: how many states your browsing has, the average bytes per second,
packet sizes and gaps in each state, and how often one state follows another.

What is stored: only those aggregates, in profile.json on this machine.
What is never stored: URLs, host names, page titles, IP addresses, search terms
or any payload bytes.
What happens to your capture: it is deleted as soon as the features are
extracted.
Who can see it: nobody but you. Nothing leaves this machine.

You can withdraw at any time in the app; withdrawing deletes profile.json and
the opt-in flag. Learning stays off until you accept this text again.
"""


@dataclass(frozen=True, slots=True)
class ConsentState:
    opt_in: bool = False
    consented_at: str | None = None
    text_version: str = CONSENT_TEXT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "opt_in": self.opt_in,
            "consented_at": self.consented_at,
            "text_version": self.text_version,
        }


class ConsentStore:
    """Reads and writes the opt-in flag file (SDD 6.1)."""

    def __init__(self, flag_path: Path, profile_path: Path) -> None:
        self.flag_path = Path(flag_path)
        self.profile_path = Path(profile_path)

    @classmethod
    def from_config(cls, cfg: Any) -> ConsentStore:
        return cls(cfg.path("profile.opt_in_flag"), cfg.path("profile.path"))

    def read(self) -> ConsentState:
        try:
            data = json.loads(self.flag_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ConsentState(opt_in=False)
        if not isinstance(data, dict):
            return ConsentState(opt_in=False)
        return ConsentState(
            opt_in=bool(data.get("opt_in", False)),
            consented_at=data.get("consented_at") if isinstance(data.get("consented_at"), str) else None,
            text_version=str(data.get("text_version", "")),
        )

    def is_opted_in(self) -> bool:
        """The exact check ``profile/capture_features.py`` performs (FR-16)."""
        state = self.read()
        return state.opt_in and state.text_version == CONSENT_TEXT_VERSION

    def accept(self, text_version: str = CONSENT_TEXT_VERSION) -> ConsentState:
        if text_version != CONSENT_TEXT_VERSION:
            raise ValueError(
                f"consent text version {text_version!r} does not match {CONSENT_TEXT_VERSION!r}"
            )
        state = ConsentState(
            opt_in=True,
            consented_at=datetime.now(UTC).isoformat(timespec="seconds"),
            text_version=text_version,
        )
        self.flag_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.flag_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, self.flag_path)
        return state

    def withdraw(self) -> ConsentState:
        """Clear the flag and delete profile.json (FR-30)."""
        for path in (self.flag_path, self.profile_path):
            try:
                path.unlink()
            except FileNotFoundError:
                continue
        return ConsentState(opt_in=False)

    def profile_exists(self) -> bool:
        return self.profile_path.is_file()


def require_opt_in(flag_path: Path) -> None:
    """Raise unless the user has accepted consent (FR-16)."""
    store = ConsentStore(flag_path, flag_path.parent / "profile.json")
    if not store.is_opted_in():
        raise PermissionError(
            "clearnet learning is not enabled: accept the consent screen in the UQAB app first"
        )
