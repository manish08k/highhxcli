"""Voice settings and setup state, kept with the user's HighhX configuration.

``<user config dir>/voice.json`` remembers what setup found or installed (the whisper.cpp
binary, the verified model, whether the microphone worked) so HighhX does not re-detect,
re-download or reinstall anything on the next start. Models live in the user *data*
directory — never in a project.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from highhx.utils.filesystem import atomic_write_text
from highhx.utils.paths import user_config_dir, user_data_dir

DEFAULT_MODEL = "base.en"
MODES = ("push-to-talk",)
CONFIRM_CHOICES = ("auto", "always")
"""``auto``: confirm only transcripts whisper.cpp is unsure of; ``always``: confirm every transcript."""
MICROPHONE_STATES = ("unknown", "ready", "denied", "silent")


def config_path() -> Path:
    return user_config_dir() / "voice.json"


def voice_data_dir() -> Path:
    return user_data_dir() / "voice"


def models_dir() -> Path:
    return voice_data_dir() / "models"


@dataclass
class VoiceConfig:
    model: str = DEFAULT_MODEL
    whisper_binary: str | None = None
    """The whisper.cpp CLI setup found or installed (re-checked before use)."""
    recorder: str | None = None
    """The recorder that last worked (``rec``, ``ffmpeg`` or ``arecord``)."""
    replies: bool = True
    mode: str = "push-to-talk"
    confirm: str = "auto"
    language: str = "en"
    microphone: str = "unknown"
    verified_models: dict[str, dict[str, Any]] = field(default_factory=dict)
    """``{model: {"size": bytes, "sha256": hex}}`` for models whose checksum was verified."""

    @classmethod
    def load(cls, path: Path | None = None) -> VoiceConfig:
        path = path or config_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()  # missing or unreadable: defaults (setup re-detects everything)
        if not isinstance(data, dict):
            return cls()
        known = {f.name for f in fields(cls)}
        config = cls(**{k: v for k, v in data.items() if k in known})
        if config.mode not in MODES:
            config.mode = MODES[0]
        if config.confirm not in CONFIRM_CHOICES:
            config.confirm = "auto"
        if config.microphone not in MICROPHONE_STATES:
            config.microphone = "unknown"
        if not isinstance(config.verified_models, dict):
            config.verified_models = {}
        return config

    def save(self, path: Path | None = None) -> None:
        path = path or config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(asdict(self), indent=2, sort_keys=True) + "\n")
