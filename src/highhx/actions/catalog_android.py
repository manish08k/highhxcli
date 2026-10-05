"""``android.*`` catalog entries. Risks: reads are SAFE, input is LOW to MEDIUM (a tap at an
unknown point is MEDIUM, a tap on a named control is rated by its label, e.g. "Delete" is HIGH),
installing and uninstalling apps are HIGH (policy names ``android:install`` and
``android:delete``), and a screenshot is ``screen:capture``."""

from __future__ import annotations

from highhx.actions.handlers import android
from highhx.actions.policy import Risk
from highhx.actions.spec import ANDROID, ActionSpec, Inputs
from highhx.cloud.plans import AGENT_COMPUTER_USE
from highhx.safety.actions import ActionKind
from highhx.utils.validation import Bool, Int, Obj, Prop, Str

DEVICE = Prop(Str(min_length=1), description="Device serial (default: the only connected device).")
COORD = Int(minimum=0, maximum=100_000)
PACKAGE = Prop(Str(min_length=1), required=True, description="e.g. com.android.settings")


def _tap_risk(inputs: Inputs) -> Risk:
    return Risk.LOW if inputs.get("text") else Risk.MEDIUM


def _key_risk(inputs: Inputs) -> Risk:
    key = str(inputs.get("key") or "").lower()
    if key in ("power", "keycode_power"):
        return Risk.HIGH
    return Risk.MEDIUM if key in ("enter", "keycode_enter") else Risk.LOW


def _spec(name: str, description: str, handler, inputs: Obj, risk: Risk, kind: ActionKind, **kw) -> ActionSpec:  # type: ignore[no-untyped-def]
    return ActionSpec(
        name,
        description,
        handler,
        inputs,
        kw.pop("outputs", {}),
        risk,
        kind,
        (ANDROID,),
        timeout=kw.pop("timeout", 60),
        feature=AGENT_COMPUTER_USE,
        agent=False,
        **kw,
    )


def android_specs() -> list[ActionSpec]:
    return [
        _spec("android.devices", "List connected Android devices and emulators (adb).", android.devices, Obj({}), Risk.SAFE, ActionKind.READ, idempotent=True),
        _spec("android.emulators", "List the Android emulator's virtual devices (AVDs).", android.emulators, Obj({}), Risk.SAFE, ActionKind.READ, idempotent=True),
        _spec(
            "android.emulator_start",
            "Start an Android emulator (AVD) headless with gRPC (AndroidWorld uses 8554) and wait until it has booted.",
            android.emulator_start,
            Obj(
                {
                    "avd": Prop(Str(min_length=1), required=True),
                    "grpc_port": Prop(Int(minimum=1024, maximum=65535)),
                    "window": Prop(Bool(), description="Show the emulator window."),
                    "wipe": Prop(Bool(), description="Wipe the AVD's data first (factory reset)."),
                    "timeout": Prop(Int(minimum=10, maximum=1800)),
                }
            ),
            Risk.MEDIUM,
            ActionKind.APP_LAUNCH,
            target=lambda i: str(i.get("avd", "")),
            risk_for=lambda i: Risk.HIGH if i.get("wipe") else Risk.MEDIUM,
            timeout=1900,
            outputs={"serial": "the emulator's adb serial", "pid": "emulator process", "log": "its log file"},
        ),
        _spec("android.health", "Is the device usable: booted, battery, screen, free storage, model, Android version (read-only).", android.health, Obj({"device": DEVICE}), Risk.SAFE, ActionKind.READ, idempotent=True),
        _spec(
            "android.emulator_reset",
            "Factory-reset an emulator: stop it and start its AVD again with all data wiped (always asked).",
            android.emulator_reset,
            Obj({"device": Prop(Str(min_length=1), required=True), "avd": Prop(Str(min_length=1), required=True), "timeout": Prop(Int(minimum=10, maximum=1800))}),
            Risk.HIGH,
            ActionKind.DELETE_FILE,
            target=lambda i: f"{i.get('device', '')} ({i.get('avd', '')})",
            timeout=1900,
        ),
        _spec(
            "android.emulator_stop",
            "Stop a running emulator (adb emu kill).",
            android.emulator_stop,
            Obj({"device": Prop(Str(min_length=1), required=True, description="e.g. emulator-5554")}),
            Risk.MEDIUM,
            ActionKind.EXEC,
            target=lambda i: str(i.get("device", "")),
        ),
        _spec(
            "android.connect",
            "Connect to a device over the network (adb connect host:port).",
            android.connect,
            Obj({"address": Prop(Str(min_length=3), required=True)}),
            Risk.LOW,
            ActionKind.READ,
            target=lambda i: str(i.get("address", "")),
        ),
        _spec(
            "android.observe",
            "Observe the device: the UI hierarchy (roles, names, bounds, resource ids), the focused app and, "
            "when asked, a screenshot and OCR.",
            android.observe,
            Obj({"device": DEVICE, "screenshot": Prop(Bool()), "ocr": Prop(Str(choices=("never", "auto", "always"))), "query": Prop(Str(min_length=1))}),
            Risk.LOW,
            ActionKind.READ,
            idempotent=True,
            policy_action=lambda i: "screen:capture" if i.get("screenshot") or i.get("ocr") == "always" else "android:observe",
        ),
        _spec(
            "android.screenshot",
            "Save a screenshot of the device (PNG).",
            android.screenshot,
            Obj({"device": DEVICE}),
            Risk.LOW,
            ActionKind.READ,
            policy_action=lambda i: "screen:capture",
        ),
        _spec(
            "android.tap",
            "Tap a point (device pixels) — or the control named by `text`, found in the UI hierarchy. `long` for a long press.",
            android.tap,
            Obj(
                {
                    "x": Prop(COORD),
                    "y": Prop(COORD),
                    "text": Prop(Str(min_length=1)),
                    "role": Prop(Str(min_length=1)),
                    "long": Prop(Bool()),
                    "ms": Prop(Int(minimum=100, maximum=10_000)),
                    "device": DEVICE,
                }
            ),
            Risk.LOW,
            ActionKind.UI_CLICK,
            risk_for=_tap_risk,
            target=lambda i: str(i.get("text") or f"({i.get('x')}, {i.get('y')})"),
        ),
        _spec(
            "android.swipe",
            "Swipe between two points, or scroll in a direction (up/down/left/right).",
            android.swipe,
            Obj(
                {
                    "x1": Prop(COORD),
                    "y1": Prop(COORD),
                    "x2": Prop(COORD),
                    "y2": Prop(COORD),
                    "ms": Prop(Int(minimum=50, maximum=10_000)),
                    "direction": Prop(Str(choices=("up", "down", "left", "right"))),
                    "amount": Prop(Int(minimum=1, maximum=10)),
                    "device": DEVICE,
                }
            ),
            Risk.LOW,
            ActionKind.UI_SCROLL,
        ),
        _spec(
            "android.type",
            "Type text into the focused field (verified by the field's value; never read back from password fields).",
            android.type_text,
            Obj({"text": Prop(Str(min_length=1), required=True), "device": DEVICE}),
            Risk.MEDIUM,
            ActionKind.UI_TYPE,
        ),
        _spec(
            "android.key",
            "Send a key event (enter, back, home, tab, delete, app_switch, KEYCODE_…).",
            android.key,
            Obj({"key": Prop(Str(min_length=1), required=True), "device": DEVICE}),
            Risk.LOW,
            ActionKind.UI_KEY,
            risk_for=_key_risk,
            target=lambda i: str(i.get("key", "")),
            aliases=("android.press",),
        ),
        _spec("android.back", "Press Back.", android.back, Obj({"device": DEVICE}), Risk.LOW, ActionKind.UI_KEY),
        _spec("android.recents", "Show the recent apps.", android.recents, Obj({"device": DEVICE}), Risk.LOW, ActionKind.UI_KEY),
        _spec(
            "android.long_press",
            "Long-press a point — or the control named by `text`, found in the UI hierarchy.",
            android.long_press,
            Obj({"x": Prop(COORD), "y": Prop(COORD), "text": Prop(Str(min_length=1)), "role": Prop(Str(min_length=1)), "ms": Prop(Int(minimum=100, maximum=10_000)), "device": DEVICE}),
            Risk.MEDIUM,
            ActionKind.UI_CLICK,
            target=lambda i: str(i.get("text") or f"({i.get('x')}, {i.get('y')})"),
        ),
        _spec(
            "android.scroll",
            "Scroll the screen (up, down, left, right).",
            android.scroll,
            Obj({"direction": Prop(Str(choices=("up", "down", "left", "right"))), "amount": Prop(Int(minimum=1, maximum=10)), "device": DEVICE}),
            Risk.LOW,
            ActionKind.UI_SCROLL,
        ),
        _spec(
            "android.find",
            "Find a control by its text or description (grounded in the UI hierarchy; several matches are listed, never guessed).",
            android.find,
            Obj({"text": Prop(Str(min_length=1), required=True), "role": Prop(Str(min_length=1)), "device": DEVICE}),
            Risk.SAFE,
            ActionKind.READ,
            idempotent=True,
        ),
        _spec(
            "android.inspect",
            "The element at a point, or the whole UI hierarchy (no screenshot).",
            android.inspect,
            Obj({"x": Prop(COORD), "y": Prop(COORD), "device": DEVICE}),
            Risk.SAFE,
            ActionKind.READ,
            idempotent=True,
        ),
        _spec("android.home", "Go to the home screen.", android.home, Obj({"device": DEVICE}), Risk.LOW, ActionKind.UI_KEY),
        _spec(
            "android.launch",
            "Launch an app by package (optionally an activity), verified by what comes to the front.",
            android.launch,
            Obj({"package": PACKAGE, "activity": Prop(Str(min_length=1)), "device": DEVICE}),
            Risk.LOW,
            ActionKind.APP_LAUNCH,
            target=lambda i: str(i.get("package", "")),
        ),
        _spec(
            "android.stop",
            "Force-stop an app (unsaved state in it is lost).",
            android.stop,
            Obj({"package": PACKAGE, "device": DEVICE}),
            Risk.MEDIUM,
            ActionKind.EXEC,
            target=lambda i: str(i.get("package", "")),
            command=lambda i: f"adb shell am force-stop {i.get('package', '')}",
        ),
        _spec(
            "android.packages",
            "List installed packages (third_party: only those the user installed).",
            android.packages,
            Obj({"third_party": Prop(Bool()), "device": DEVICE}),
            Risk.SAFE,
            ActionKind.READ,
            idempotent=True,
        ),
        _spec(
            "android.install",
            "Install (or update) an app from an .apk file.",
            android.install,
            Obj({"apk": Prop(Str(min_length=1), required=True), "device": DEVICE}),
            Risk.HIGH,
            ActionKind.EXEC,
            timeout=600,
            target=lambda i: str(i.get("apk", "")),
            command=lambda i: f"adb install -r {i.get('apk', '')}",
            policy_action=lambda i: "android:install",
        ),
        _spec(
            "android.uninstall",
            "Uninstall an app and its data.",
            android.uninstall,
            Obj({"package": PACKAGE, "device": DEVICE}),
            Risk.HIGH,
            ActionKind.DELETE_FILE,
            timeout=120,
            target=lambda i: str(i.get("package", "")),
            policy_action=lambda i: "android:delete",
        ),
    ]
