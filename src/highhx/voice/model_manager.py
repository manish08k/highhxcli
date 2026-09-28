"""The whisper.cpp model HighhX manages: download once, verify, cache, reuse.

Models are downloaded from the whisper.cpp model repository into the user data directory
(``<data>/voice/models``), streamed to a ``.part`` file while their SHA-256 is computed, and
only renamed into place when size and checksum match the published values. After that a
cheap check (size + GGML header) is enough on each start; ``verify(full=True)`` re-hashes.
"""

from __future__ import annotations

import hashlib
import os
import ssl
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from highhx.voice.config import DEFAULT_MODEL, VoiceConfig, models_dir
from highhx.voice.engines import VoiceError

BASE_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"
GGML_MAGIC = b"lmgg"  # 0x67676d6c little-endian: the header of every ggml whisper model
CHUNK = 1 << 20

Progress = Callable[[int, int], None]
"""``(downloaded bytes, total bytes)``."""
Opener = Callable[[str], "object"]


@dataclass(frozen=True)
class ModelInfo:
    name: str
    size: int
    sha256: str
    description: str

    @property
    def filename(self) -> str:
        return f"ggml-{self.name}.bin"

    @property
    def url(self) -> str:
        return f"{BASE_URL}/{self.filename}"

    @property
    def size_label(self) -> str:
        return f"{self.size / 1_000_000:.0f} MB"


MODELS: dict[str, ModelInfo] = {
    m.name: m
    for m in (
        ModelInfo(
            "tiny.en",
            77_704_715,
            "921e4cf8686fdd993dcd081a5da5b6c365bfde1162e72b08d75ac75289920b1f",
            "fastest, least accurate",
        ),
        ModelInfo(
            "base.en",
            147_964_211,
            "a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002",
            "fast and accurate for spoken commands (default)",
        ),
        ModelInfo(
            "small.en",
            487_614_201,
            "c6138d6d58ecc8322097e0f987c32f1be8bb0a18532a3f88f734d1bbf9c41e5d",
            "more accurate, slower",
        ),
    )
}


def model_info(name: str) -> ModelInfo:
    try:
        return MODELS[name]
    except KeyError:
        known = ", ".join(MODELS)
        raise VoiceError("model-unknown", f"Unknown voice model {name!r}.", hint=f"Choose one of: {known}.") from None


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


SYSTEM_CA_BUNDLES = (
    "/etc/ssl/cert.pem",  # macOS, Alpine, Arch
    "/etc/ssl/certs/ca-certificates.crt",  # Debian, Ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",  # Fedora, RHEL
    "/etc/ssl/ca-bundle.pem",  # openSUSE
)


def ssl_context() -> ssl.SSLContext:
    """A verifying TLS context. Python builds without their own CA store (python.org's macOS
    installer, before "Install Certificates") get the operating system's CA bundle instead —
    verification is never turned off."""
    context = ssl.create_default_context()
    if context.cert_store_stats().get("x509_ca", 0):
        return context
    try:
        import certifi

        context.load_verify_locations(certifi.where())
        return context
    except ImportError:
        pass
    for bundle in SYSTEM_CA_BUNDLES:
        if Path(bundle).is_file():
            context.load_verify_locations(bundle)
            break
    return context


def _urlopen(url: str) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": "HighhX-voice"})
    return urllib.request.urlopen(request, timeout=60, context=ssl_context())  # nosec B310 - fixed https URL


class ModelManager:
    def __init__(self, config: VoiceConfig, *, directory: Path | None = None, opener: Opener = _urlopen) -> None:
        self.config = config
        self.directory = directory or models_dir()
        self.opener = opener

    @property
    def name(self) -> str:
        return self.config.model if self.config.model in MODELS else DEFAULT_MODEL

    @property
    def info(self) -> ModelInfo:
        return MODELS[self.name]

    def override(self) -> Path | None:
        """``HIGHHX_WHISPER_MODEL`` — an advanced override, never required."""
        value = os.environ.get("HIGHHX_WHISPER_MODEL")
        return Path(value).expanduser() if value else None

    def path(self, name: str | None = None) -> Path:
        return self.directory / model_info(name or self.name).filename

    # --------------------------------------------------------------- checks
    def status(self) -> str:
        """``ready``, ``missing`` or ``corrupted``."""
        override = self.override()
        if override is not None:
            return "ready" if override.is_file() and _has_magic(override) else "missing"
        try:
            self.verify()
        except VoiceError as exc:
            return "missing" if exc.problem == "model-missing" else "corrupted"
        return "ready"

    def verify(self, *, full: bool = False) -> Path:
        """The usable model file, or a :class:`VoiceError` saying what is wrong with it."""
        override = self.override()
        if override is not None:
            if not override.is_file():
                raise VoiceError(
                    "model-missing",
                    f"HIGHHX_WHISPER_MODEL points to a missing file: {override}",
                    hint="Unset HIGHHX_WHISPER_MODEL to use the model HighhX manages.",
                )
            return override
        info = self.info
        path = self.path()
        if not path.is_file():
            raise VoiceError("model-missing", f"Whisper model {info.name} is not downloaded.", hint="Run /voice setup.")
        if path.stat().st_size != info.size or not _has_magic(path):
            raise VoiceError(
                "model-corrupted",
                f"Whisper model {info.name} is damaged or incomplete.",
                hint="Run /voice setup to download it again.",
            )
        recorded = self.config.verified_models.get(info.name, {})
        if full or recorded.get("sha256") != info.sha256:
            if sha256_of(path) != info.sha256:
                raise VoiceError(
                    "model-corrupted",
                    f"Whisper model {info.name} failed its checksum.",
                    hint="Run /voice setup to download it again.",
                )
            self.config.verified_models[info.name] = {"size": info.size, "sha256": info.sha256}
        return path

    # -------------------------------------------------------------- download
    def download(self, progress: Progress | None = None) -> Path:
        info = self.info
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.path()
        partial = target.with_suffix(".part")
        digest = hashlib.sha256()
        done = 0
        try:
            response = self.opener(info.url)
            total = int(getattr(response, "headers", {}).get("Content-Length") or info.size)
            with partial.open("wb") as handle:
                while chunk := response.read(CHUNK):  # type: ignore[attr-defined]
                    handle.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if progress is not None:
                        progress(done, total)
            close = getattr(response, "close", None)
            if close is not None:
                close()
        except OSError as exc:
            partial.unlink(missing_ok=True)
            raise VoiceError(
                "model-download-failed",
                f"Downloading the {info.name} voice model failed.",
                hint=f"{exc}. Check your internet connection and run /voice setup again.",
            ) from None
        if done != info.size or digest.hexdigest() != info.sha256:
            partial.unlink(missing_ok=True)
            raise VoiceError(
                "model-corrupted",
                f"The downloaded {info.name} model did not match its checksum.",
                hint="Nothing was installed. Run /voice setup to try again.",
            )
        partial.replace(target)
        self.config.verified_models[info.name] = {"size": info.size, "sha256": info.sha256}
        return target


def _has_magic(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(4) == GGML_MAGIC
    except OSError:
        return False
