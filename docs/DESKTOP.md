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

System file dialogs (macOS): `computer.file_dialog` chooses a project file in the open dialog in
front, or saves into the project through the save dialog, by typing the path into the dialog's own
"Go to folder" field (no clicks at guessed places). The dialog must already be open; afterwards it
must have closed, and a saved file must exist. Project files only (never secret files), medium risk
(a file handed to an application may leave the machine); replacing a file needs `overwrite`. On
Windows and Linux it is refused with how to proceed: type the path into the dialog's file-name field.

Keyboard actions are refused when their target is a terminal (commands go through `shell.run`).
Pasting and cutting are medium risk; a click is rated by its target's label (`Delete account` is high).

## Capability detection

`highhx capabilities` and `highhx computer status` report what this machine allows (permissions,
tools) and why not; unsupported operations raise a capability error instead of pretending.
