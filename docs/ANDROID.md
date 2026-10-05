# Android

HighhX operates Android phones, tablets and emulators through **adb**. Every operation is an
`android.*` catalog action, run by the executor (risk, policy, approval, verification, audit).

Code: [`src/highhx/drivers/android/`](../src/highhx/drivers/android),
[`actions/handlers/android.py`](../src/highhx/actions/handlers/android.py).

## Requirements and capability detection

adb is found as `$HIGHHX_ADB`, `adb` on `PATH`, or `$ANDROID_HOME`/`$ANDROID_SDK_ROOT`
`/platform-tools/adb`. Without it, `highhx android devices` says so and how to install it, and
every other command fails with that reason. Nothing is pretended. Unauthorized or offline
devices are reported with the fix ("accept the USB debugging prompt"). With several devices,
pass `--device SERIAL`.

**What has run where:** the client, hierarchy parser, driver and actions are tested against a
simulated device behind a fake adb runner (`highhx.benchmarks.environments.android`). adb is
not installed in the environment this build was developed in, so **no real device or emulator
has been driven by this build.**

## Actions

| Action | Risk | Notes |
|---|---|---|
| `android.devices`, `packages`, `find`, `inspect` | safe | reads |
| `android.observe` | low | UI hierarchy (uiautomator), focused app, device. `screen:capture` with `--screenshot` |
| `android.screenshot` | low | PNG via `exec-out screencap -p` |
| `android.tap` | low by text, medium at a point | text is grounded in the hierarchy. Several matches are refused, never guessed. The label is classified (`Delete account` is high) |
| `android.long_press` | medium | |
| `android.swipe`, `android.scroll` | low | direction or two points |
| `android.type` | medium | verified by the focused field's value. Never read back from password fields |
| `android.key` (alias `android.press`) | low; enter medium; power high | named keys or `KEYCODE_…` |
| `android.back`, `home`, `recents` | low | |
| `android.launch` | low | verified by the package that comes to the front |
| `android.stop` | medium | force-stop |
| `android.install` / `uninstall` | high | policy names `android:install` / `android:delete` |
| `android.connect` | low | `adb connect host:port` |
| `android.emulators` | safe | the SDK emulator's AVDs |
| `android.emulator_start` | medium; high with `wipe` | starts an AVD detached (`-no-window`, `-grpc 8554` by default, `-no-snapshot-save`) and waits for `sys.boot_completed` |
| `android.emulator_stop` | medium | `adb emu kill`; refuses serials that are not emulators |
| `android.emulator_reset` | high | stop, then start the AVD again with its data wiped |
| `android.health` | safe | booted, battery, screen on, free storage, model, Android version (read, never guessed) |

Safety details: every adb call is an argv list (no host shell). Text for `input text` is quoted
for the device shell (`; rm -rf /` stays text). Package names, activities, serials and property
names are validated.

## Emulators and AndroidWorld-style tasks

The emulator is found as `$HIGHHX_EMULATOR`, `emulator` on `PATH`, or
`$ANDROID_HOME`/`$ANDROID_SDK_ROOT``/emulator/emulator`; its log goes to the user data
directory. AndroidWorld expects an emulator with gRPC on 8554, the default here.

[`benchmarks/environments/android_world.py`](../src/highhx/benchmarks/environments/android_world.py)
mirrors AndroidWorld's task contract with adb as the environment: `initialize_task`,
`is_successful` (0.0–1.0, read from the device), `tear_down`, `complexity`, `params`. Tasks are
Python classes registered by name (`open_app`, `screen_shows_text` are built in), and a
benchmark suite refers to them by name, so a YAML file never imports code:

```yaml
- id: open-settings
  environment: {kind: android_device, task: open_app, params: {package: com.android.settings}}
  planner: {kind: scripted, steps: [{action: launch, parameters: {name: com.android.settings}}]}
```

The agent loop acts through the executor; the task's own `is_successful` decides. Without adb or
a ready device the task is listed under `skipped` with the reason: neither passed nor failed.

**Status:** implemented and tested with a fake emulator binary and simulated adb only. It is
experimental until it has run against a real emulator.

## Driver

`AndroidDriver` implements the common `ComputerDriver` interface. Coordinates are device
pixels. `move`, `hotkey` and `right_click` report that Android has no such thing
(`CapabilityError`); `long_press`, `swipe`, `back`, `home` and `recents` are Android extras.

## CLI

```text
highhx android devices · connect HOST:PORT · observe [--screenshot] · screenshot
highhx android tap "Save" | tap 540,1200 [--long] [--role button]
highhx android type TEXT · swipe up | swipe 540,1500 540,400 · press enter · back · home · recents
highhx android launch com.android.settings · find "Wi-Fi"
highhx android agent "turn on airplane mode" --plan steps.yaml | --model
```
