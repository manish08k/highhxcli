"""A simulated Android device behind a fake adb runner: devices, a UI hierarchy that reacts to
taps and typing, the focused app, packages and screenshots. Only adb is simulated; the client,
driver, handlers, executor and approvals are real."""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Any

from highhx.perception.png import encode, fill, solid


def png(width: int = 80, height: int = 60, boxes: tuple[tuple[int, int, int, int], ...] = ()) -> bytes:
    rgb = solid(width, height)
    for box in boxes:
        fill(rgb, width, box, (0, 0, 0))
    return encode(width, height, bytes(rgb))

SETTINGS = "com.android.settings"
NOTES = "com.example.notes"


@dataclass
class FakeDevice:
    serial: str = "emulator-5554"
    state: str = "device"
    focused_app: str = "com.android.launcher3"
    field_value: str = ""
    field_focused: bool = False
    packages: list[str] = field(default_factory=lambda: [SETTINGS, NOTES, "com.android.launcher3"])
    duplicate_delete: bool = False
    calls: list[list[str]] = field(default_factory=list)
    taps: list[tuple[int, int]] = field(default_factory=list)
    typed: list[str] = field(default_factory=list)
    extra_devices: list[str] = field(default_factory=list)

    @property
    def tapped(self) -> bool:
        return bool(self.taps)

    def hierarchy(self) -> str:
        nodes = [
            '<node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="{app}" clickable="false" bounds="[0,0][1080,1920]">',
            '<node index="1" text="Notes" resource-id="{app}:id/title" class="android.widget.TextView" package="{app}" clickable="false" bounds="[40,60][400,140]" />',
            '<node index="2" text="{value}" hint="Title" resource-id="{app}:id/note_title" class="android.widget.EditText" package="{app}" clickable="true" focused="{focused}" bounds="[40,200][1040,300]" />',
            '<node index="3" text="" resource-id="{app}:id/pin" class="android.widget.Switch" checkable="true" checked="false" package="{app}" clickable="true" bounds="[900,320][1040,400]" content-desc="Pin" />',
            '<node index="4" text="Save" resource-id="{app}:id/save" class="android.widget.Button" package="{app}" clickable="true" bounds="[40,420][500,520]" />',
            '<node index="5" text="Delete" resource-id="{app}:id/delete" class="android.widget.Button" package="{app}" clickable="true" bounds="[540,420][1040,520]" />',
            '<node index="6" text="" resource-id="{app}:id/pw" class="android.widget.EditText" package="{app}" password="true" clickable="true" bounds="[40,560][1040,660]" hint="Password" />',
        ]
        if self.duplicate_delete:
            nodes.append(
                '<node index="7" text="Delete" resource-id="{app}:id/delete_all" class="android.widget.Button" package="{app}" clickable="true" bounds="[540,700][1040,800]" />'
            )
        nodes.append("</node>")
        body = "".join(nodes).format(app=self.focused_app, value=self.field_value, focused=str(self.field_focused).lower())
        return f"<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation=\"0\">{body}</hierarchy>UI hierchary dumped to: /dev/tty"

    # ------------------------------------------------------------------ runner
    def __call__(self, argv: list[str], timeout: float, cancel: Any) -> tuple[int, bytes, bytes]:
        self.calls.append(argv)
        args = argv[1:]
        if args[:1] == ["-s"]:
            if args[1] not in (self.serial, *self.extra_devices):
                return 1, b"", f"error: device '{args[1]}' not found".encode()
            args = args[2:]
        if args == ["devices", "-l"]:
            lines = [f"{self.serial}\t{self.state} product:sdk_gphone64 model:Pixel_8 transport_id:1"]
            lines += [f"{s}\tdevice model:Other transport_id:2" for s in self.extra_devices]
            return 0, ("List of devices attached\n" + "\n".join(lines) + "\n\n").encode(), b""
        if args[:1] == ["connect"]:
            return 0, f"connected to {args[1]}".encode(), b""
        if args == ["exec-out", "screencap", "-p"]:
            return 0, png(108, 192), b""
        if args == ["exec-out", "uiautomator", "dump", "/dev/tty"]:
            return 0, self.hierarchy().encode(), b""
        if args[:1] == ["install"]:
            self.packages.append("com.example.installed")
            return 0, b"Performing Streamed Install\nSuccess", b""
        if args[:1] == ["uninstall"]:
            if args[1] in self.packages:
                self.packages.remove(args[1])
                return 0, b"Success", b""
            return 1, b"Failure [DELETE_FAILED_INTERNAL_ERROR]", b""
        if args[:1] == ["shell"]:
            return self._shell(shlex.split(" ".join(args[1:])))
        return 1, b"", f"unexpected adb call {args}".encode()

    def _shell(self, cmd: list[str]) -> tuple[int, bytes, bytes]:
        if cmd == ["wm", "size"]:
            return 0, b"Physical size: 1080x1920\n", b""
        if cmd[:2] == ["dumpsys", "window"]:
            return 0, f"  mCurrentFocus=Window{{4f2 u0 {self.focused_app}/.MainActivity}}\n".encode(), b""
        if cmd[:3] == ["pm", "list", "packages"]:
            return 0, "".join(f"package:{p}\n" for p in self.packages).encode(), b""
        if cmd[:2] == ["input", "tap"]:
            x, y = int(cmd[2]), int(cmd[3])
            self.taps.append((x, y))
            self.field_focused = 40 <= x <= 1040 and 200 <= y <= 300
            return 0, b"", b""
        if cmd[:2] == ["input", "text"]:
            text = cmd[2].replace("%s", " ").replace("\\%", "%")
            self.typed.append(text)
            if self.field_focused:
                self.field_value += text
            return 0, b"", b""
        if cmd[:2] in (["input", "keyevent"], ["input", "swipe"]):
            if cmd[:2] == ["input", "keyevent"] and cmd[2] == "3":
                self.focused_app = "com.android.launcher3"
            return 0, b"", b""
        if cmd[:1] == ["monkey"]:
            package = cmd[cmd.index("-p") + 1]
            if package not in self.packages:
                return 0, b"** No activities found to run, monkey aborted.", b""
            self.focused_app = package
            return 0, b"Events injected: 1", b""
        if cmd[:2] == ["am", "start"]:
            self.focused_app = cmd[3].split("/")[0]
            return 0, b"Starting: Intent", b""
        if cmd[:2] == ["am", "force-stop"]:
            if self.focused_app == cmd[2]:
                self.focused_app = "com.android.launcher3"
            return 0, b"", b""
        if cmd[:1] == ["getprop"]:
            return 0, b"Pixel 8\n", b""
        return 1, b"", f"unexpected shell {cmd}".encode()
