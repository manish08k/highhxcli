# Desktop control

HighhX operates desktop applications through one `ComputerDriver` interface and the `computer.*`
actions — every one classified, approved, audited and verified by the executor.

| Platform | Backend | Status here |
|---|---|---|
| macOS | Accessibility (AX) + Quartz events, `screencapture` | implemented; the opt-in input tests (`HIGHHX_TEST_DESKTOP_INPUT=1`) were not run in this phase (they need the owner at the keyboard) |
| Windows | UI Automation, `SendInput`, `SetWindowPos` | implemented, not validated on Windows (experimental) |
| Linux | X11 (`xdotool`), AT-SPI (PyGObject), screenshots via the desktop's tools | implemented, not validated (experimental); Wayland is not supported |

## Actions

Pointer: `computer.click_at` (left/right/middle, double/triple, by text or coordinates, background
delivery), `computer.move`, `computer.mouse_button` (down/up), `computer.drag`, `computer.scroll`
(up/down/left/right). Keyboard: `computer.type`, `computer.press`, `computer.hotkey`,
`computer.edit` (copy, cut, paste, select all, undo with ⌘ on macOS and Ctrl elsewhere). Windows and
apps: `computer.launch`, `computer.quit`, `computer.focus`, `computer.windows`, `computer.window`
(move/resize), `computer.window_state` (maximize everywhere; minimize on macOS), `computer.menu`.
Other: `computer.screenshot`, `computer.state`, `computer.element_at`, `computer.clipboard_read/write`,
`computer.verify`, `computer.wait`.

Keyboard actions are refused when their target is a terminal (commands go through `shell.run`).
Pasting and cutting are medium risk; a click is rated by its target's label (`Delete account` is high).

## Capability detection

`highhx capabilities` and `highhx computer status` report what this machine allows (permissions,
tools) and why not; unsupported operations raise a capability error instead of pretending.
