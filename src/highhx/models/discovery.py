"""Local model discovery: which models do the servers on this computer offer, and which can see?

    Ollama              GET http://127.0.0.1:11434/api/tags
    OpenAI-compatible   GET <base_url>/models   (llama.cpp server, vLLM, LM Studio …)

Only loopback servers are contacted (the default Ollama address, and HIGHHX_PLANNER_BASE_URL /
HIGHHX_VISION_BASE_URL when they are on this computer), only when asked (``highhx agent models
--discover``) — never by ``highhx capabilities``. No key is sent: local servers need none, and
provider keys are never read by the CLI. Vision support is the same name-based inference the
runtime uses (or what the server declares).
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any

from highhx.agent.model.capabilities import VISION_NAME, is_loopback

OLLAMA = "http://127.0.0.1:11434"


@dataclass(frozen=True)
class LocalModel:
    endpoint: str
    model: str
    vision: bool
    server: str
    """``ollama`` or ``openai-compatible``."""
    size: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _get(url: str, timeout: float) -> Any:
    with urllib.request.urlopen(
        urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=timeout
    ) as r:  # nosec B310 - loopback only, checked by the caller
        return json.loads(r.read(2_000_000))


def endpoints() -> list[str]:
    found = [OLLAMA]
    for var in ("HIGHHX_PLANNER_BASE_URL", "HIGHHX_VISION_BASE_URL"):
        value = os.environ.get(var, "").rstrip("/")
        if value and is_loopback(value) and value not in found:
            found.append(value)
    return found


def discover(*, timeout: float = 2.0) -> tuple[list[LocalModel], dict[str, str]]:
    """Models per loopback endpoint, and the endpoints that did not answer (with why)."""
    models: list[LocalModel] = []
    silent: dict[str, str] = {}
    for base in endpoints():
        if not is_loopback(base):
            continue
        try:
            if base == OLLAMA:
                data = _get(f"{base}/api/tags", timeout)
                for item in data.get("models") or []:
                    name = str(item.get("name") or item.get("model") or "")
                    families = " ".join(str(f) for f in (item.get("details") or {}).get("families") or [])
                    models.append(
                        LocalModel(
                            f"{base}/v1",
                            name,
                            bool(VISION_NAME.search(name.lower()) or "clip" in families),
                            "ollama",
                            int(item.get("size") or 0),
                        )
                    )
            else:
                data = _get(f"{base}/models", timeout)
                for item in data.get("data") or []:
                    name = str(item.get("id") or "")
                    models.append(LocalModel(base, name, bool(VISION_NAME.search(name.lower())), "openai-compatible"))
        except (OSError, ValueError) as exc:
            silent[base] = str(getattr(exc, "reason", exc))[:120]
    return models, silent
