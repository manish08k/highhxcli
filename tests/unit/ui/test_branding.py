"""The HighhX knight: terminal-native art, layouts, fallbacks and startup integration."""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest
from rich.console import Console

from highhx import __version__
from highhx.agent.context import ProjectContext
from highhx.agent.ui import TerminalUI
from highhx.ui import branding
from highhx.ui.branding import ARTS, FULL_MIN_HEIGHT, GAP, banner, banner_lines, choose, fit, knight
from highhx.ui.terminal import UNICODE_SYMBOLS

QUADRANTS = set(" ▘▝▖▗▀▄▌▐▚▞▛▜▙▟█")
MIRROR = str.maketrans("▘▝▖▗▌▐▚▞▛▜▙▟/\\", "▝▘▗▖▐▌▞▚▜▛▟▙\\/")
TEXT = ["HighhX v0.3.0", "AI Developer Agent", "~/cli_project"]


def _console(width: int, height: int = 40) -> tuple[Console, io.StringIO]:
    out = io.StringIO()
    return Console(file=out, width=width, height=height, color_system=None, force_terminal=True), out


# ------------------------------------------------------------------ the art
@pytest.mark.parametrize("key", list(ARTS))
def test_art_is_symmetric_and_compact(key: tuple[str, str]) -> None:
    art = ARTS[key]
    assert (art.width, art.height) == ((26, 31) if key[1] == "full" else (13, 15))
    for row in art.padded():
        assert row == row[::-1].translate(MIRROR), f"asymmetric row: {row!r}"
    assert all(line == line.rstrip() for line in art.lines)  # stored without trailing padding


def test_unicode_art_uses_only_block_characters() -> None:
    for compact in (False, True):
        art = knight(unicode=True, compact=compact)
        assert set("".join(art.lines)) <= QUADRANTS
        assert sum(ch != " " for ch in "".join(art.lines)) > (60 if compact else 250)  # a real drawing


def test_ascii_fallback_is_pure_ascii_with_the_same_silhouette() -> None:
    for compact in (False, True):
        uni, asc = knight(unicode=True, compact=compact), knight(unicode=False, compact=compact)
        assert all(ch.isascii() for ch in "".join(asc.lines))
        assert (uni.width, uni.height) == (asc.width, asc.height)
        for u, a in zip(uni.padded(), asc.padded(), strict=True):
            assert [c == " " for c in u] == [c == " " for c in a]


def test_the_art_ships_as_text_not_as_an_image() -> None:
    package = Path(branding.__file__).parent.parent
    images = [p for p in package.rglob("*") if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".svg", ".bmp"}]
    assert images == []
    source = Path(branding.__file__).read_text(encoding="utf-8")
    for marker in ("sixel", "\x1b]1337", "\x1b_G", "base64", "PIL"):
        assert marker not in source


# ------------------------------------------------------------------ layout
@pytest.mark.parametrize(
    ("width", "height", "expected", "stacked"),
    [
        (120, 50, "unicode-full", False),  # large terminal
        (80, 24, "unicode-compact", False),  # classic 80x24
        (100, FULL_MIN_HEIGHT - 1, "unicode-compact", False),  # wide but short
        (36, 24, "unicode-compact", True),  # narrow: knight above the text
        (13, 24, "unicode-compact", True),
        (12, 24, None, False),  # too narrow for any knight
    ],
)
def test_layout_for_terminal_size(width: int, height: int, expected: str | None, stacked: bool) -> None:
    layout = choose(width, height, unicode=True)
    assert (layout.art.name if layout.art else None) == expected
    assert layout.stacked is stacked


def test_layout_preferences() -> None:
    assert choose(120, 50, unicode=True, preference="off").art is None
    assert choose(120, 50, unicode=True, preference="compact").art == knight(compact=True)
    assert choose(120, 20, unicode=True, preference="full").art == knight()
    assert choose(40, 20, unicode=True, preference="full").art == knight(compact=True)  # full does not fit
    assert choose(120, 50, unicode=False).art == knight(unicode=False)


@pytest.mark.parametrize("width", [8, 13, 20, 36, 40, 50, 60, 80, 100, 160])
@pytest.mark.parametrize("unicode", [True, False])
def test_banner_never_exceeds_the_terminal_width(width: int, unicode: bool) -> None:
    for height in (15, 24, 60):
        lines = banner_lines(choose(width, height, unicode=unicode), TEXT, width=width)
        assert all(len(line) <= width for line in lines), (width, height)
        joined = "\n".join(lines)
        assert "HighhX" in joined or width < 6


def test_text_sits_beside_the_helmet() -> None:
    lines = banner_lines(choose(100, 50, unicode=True), TEXT, width=100)
    row = next(i for i, line in enumerate(lines) if "HighhX v0.3.0" in line)
    assert 5 < row < 15  # beside the dome, not above or below the knight
    assert lines[row].index("HighhX") == knight().width + GAP
    assert "AI Developer Agent" in lines[row + 1] and "~/cli_project" in lines[row + 2]


def test_long_paths_keep_their_end() -> None:
    assert fit("/very/long/path/to/project", 12, keep_end=True) == "…/to/project"
    assert fit("HighhX v0.3.0", 8) == "HighhX …"
    lines = banner_lines(choose(50, 24, unicode=True), ["HighhX", "Agent", "/a/b/c/d/e/f/g/h/i/j/k/project"], width=50)
    assert any(line.endswith("…/j/k/project") or line.endswith("k/project") for line in lines)


# ------------------------------------------------------------------ rendering
def test_rich_banner_unicode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HIGHHX_ASCII", raising=False)
    monkeypatch.delenv("HIGHHX_BANNER", raising=False)
    console, out = _console(100, 50)
    console.print(banner(console, "HighhX v0.3.0", "AI Developer Agent", "~/cli_project"))
    text = out.getvalue()
    assert knight().lines[0] in text and "HighhX v0.3.0" in text
    assert max(len(line) for line in text.splitlines()) <= 100


def test_rich_banner_falls_back_to_ascii(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIGHHX_ASCII", "1")
    console, out = _console(100, 50)
    console.print(banner(console, "HighhX v0.3.0", "Developer command center", "~/p"))
    assert out.getvalue().isascii()
    assert knight(unicode=False).lines[0] in out.getvalue()


def test_non_utf8_terminal_gets_ascii(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HIGHHX_ASCII", raising=False)
    ascii_stream = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
    assert branding.supports_unicode(Console(file=ascii_stream, width=100)) is False
    assert branding.supports_unicode(Console(file=io.TextIOWrapper(io.BytesIO(), encoding="utf-8"))) is True


def test_banner_can_be_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIGHHX_BANNER", "off")
    console, out = _console(100, 50)
    console.print(banner(console, "HighhX v0.3.0", "Developer command center"))
    assert out.getvalue().strip().splitlines() == ["HighhX v0.3.0", "Developer command center"]


# ------------------------------------------------------------------ integration
def test_agent_banner_shows_the_knight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HIGHHX_BANNER", raising=False)
    monkeypatch.delenv("HIGHHX_ASCII", raising=False)
    console, out = _console(80, 24)
    ui = TerminalUI(console, UNICODE_SYMBOLS, interactive=True)
    ui.banner(ProjectContext(name="demo", root=tmp_path, initialized=True), [("Project", "demo")])
    text = out.getvalue()
    assert knight(compact=True).lines[0] in text
    assert f"HighhX v{__version__}" in text and "Developer command center" in text and "Project" in text


def test_bare_highhx_prints_no_banner_when_piped() -> None:
    from click.testing import CliRunner

    from highhx.cli import cli

    result = CliRunner().invoke(cli, [])
    assert result.exit_code == 0 and result.output.startswith("Usage:")
    assert "▟" not in result.output


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pseudo-terminal")
@pytest.mark.parametrize(("cols", "rows", "art"), [(100, 50, "full"), (80, 24, "compact"), (30, 24, "compact")])
def test_bare_highhx_in_a_real_terminal_shows_the_knight(cols: int, rows: int, art: str, tmp_path: Path) -> None:
    import fcntl
    import pty
    import select
    import struct
    import termios
    import time

    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - child
        os.chdir(tmp_path)
        env = {**os.environ, "COLUMNS": str(cols), "LINES": str(rows), "NO_COLOR": "1", "TERM": "xterm"}
        env.pop("HIGHHX_BANNER", None)
        env.pop("HIGHHX_ASCII", None)
        os.execve(sys.executable, [sys.executable, "-m", "highhx"], env)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    output = b""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
    os.waitpid(pid, 0)
    text = output.decode("utf-8", "replace").replace("\r\n", "\n")
    expected = knight(compact=art == "compact")
    assert expected.lines[0].strip() in text and expected.lines[-1].strip() in text
    assert f"HighhX v{__version__}" in text and "Usage:" in text
    knight_rows = text.split("Usage:", 1)[0].splitlines()  # the help below is Click's own text
    assert all(len(line) <= cols for line in knight_rows if "\x1b" not in line)
