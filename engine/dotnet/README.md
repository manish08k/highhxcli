# HighhX automation engine (C#/.NET)

`highhx-automation` is the native automation engine behind HighhX Free's desktop actions
(launch, focus, click, type, keys, scroll, inspect, verify). HighhX talks to it through the
**automation bridge**: one JSON object per line over stdin/stdout, protocol version 1
(`src/highhx/automation/engine/protocol.py` is the reference; `Protocol.cs` mirrors it).

```
HighhX Free: deterministic resolver → plan → action executor ─┐   (risk, approval, verification)
HighhX Pro:  Pro agent (JEv gate)   → actions / computer tools ─┴→ automation bridge
           → highhx-automation (this engine)  or  the built-in Python engine → macOS
```

The engine can only do what the protocol lists. There is no script evaluation, no shell and
no clicking at raw coordinates: elements are found by accessible role and name. Keyboard
operations are refused while a terminal is frontmost (checked by HighhX *and* by the engine).

## Build

Requires the .NET 8 SDK.

```sh
cd engine/dotnet/HighhX.Automation
dotnet publish -c Release -r osx-arm64 -o out     # osx-x64 on Intel Macs
mkdir -p ~/Library/Application\ Support/highhx/engine   # or your HIGHHX_DATA_DIR/engine
cp out/highhx-automation ~/Library/Application\ Support/highhx/engine/
```

Grant the engine Accessibility access (System Settings → Privacy & Security → Accessibility).
`highhx computer status` shows which engine HighhX is using.

## Selecting the engine

| `HIGHHX_AUTOMATION_ENGINE` | engine |
|---|---|
| unset / `auto` | the .NET engine when installed (PATH or `<data dir>/engine`) and the handshake succeeds, else Python |
| `python` | always the built-in Python engine (System Events via osascript) |
| `dotnet` | the .NET engine; an error when it is missing |
| a path | the .NET engine at that path |

## Try the protocol by hand

```sh
echo '{"v":1,"id":1,"op":"frontmost","args":{}}' | ./out/highhx-automation --stdio
./out/highhx-automation --protocol
```

## Status

The Python engine is the default and is what HighhX's test suite exercises. The .NET engine
implements the same operations natively for macOS (other platforms answer
`unsupported_platform`); its protocol tables are checked against the Python ones in
`tests/unit/automation/test_engine.py`.
