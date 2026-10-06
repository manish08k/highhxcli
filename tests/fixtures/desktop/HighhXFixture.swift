// HighhX desktop fixture: a small AppKit application with one of each control the computer
// runtime acts on. After every change it writes its state as JSON to the path given as its
// first argument, so tests verify real effects (a count went up, text arrived, a slider moved),
// not just that an event was sent. Built by tests/e2e/test_desktop_live.py with swiftc.
import AppKit

// Records every kind of pointer input it receives, with where (in its own coordinates).
final class Pad: NSView {
    var onEvent: ((String, NSPoint) -> Void)?
    override var acceptsFirstResponder: Bool { true }
    override func mouseDown(with event: NSEvent) {
        onEvent?(event.clickCount == 2 ? "double" : "left", convert(event.locationInWindow, from: nil))
    }
    override func rightMouseDown(with event: NSEvent) { onEvent?("right", convert(event.locationInWindow, from: nil)) }
    override func otherMouseDown(with event: NSEvent) { onEvent?("middle", convert(event.locationInWindow, from: nil)) }
    override func scrollWheel(with event: NSEvent) {
        if event.scrollingDeltaX != 0 { onEvent?(event.scrollingDeltaX > 0 ? "scroll-left" : "scroll-right", .zero) }
        if event.scrollingDeltaY != 0 { onEvent?(event.scrollingDeltaY > 0 ? "scroll-up" : "scroll-down", .zero) }
    }
    override func draw(_ dirtyRect: NSRect) { NSColor.systemTeal.setFill(); dirtyRect.fill() }
}

final class Fixture: NSObject, NSApplicationDelegate, NSTextFieldDelegate {
    let statePath: String
    var window: NSWindow!
    var second: NSWindow!
    var count = 0
    var adds = [0, 0]
    let label = NSTextField(labelWithString: "Count: 0")
    let name = NSTextField(string: "")
    let email = NSTextField(string: "")
    let secret = NSSecureTextField(string: "hunter2")
    let agree = NSButton(checkboxWithTitle: "Agree", target: nil, action: nil)
    let volume = NSSlider(value: 0, minValue: 0, maxValue: 100, target: nil, action: nil)
    let scroll = NSScrollView()
    let pad = Pad(frame: NSRect(x: 0, y: 0, width: 200, height: 60))
    var padEvents: [String: Int] = [:]
    var tinyClicks = 0
    var chosen = ""
    var saved = ""

    init(statePath: String) { self.statePath = statePath }

    func write() {
        let state: [String: Any] = [
            "count": count, "adds": adds, "name": name.stringValue, "email": email.stringValue, "agree": agree.state == .on,
            "volume": Int(volume.doubleValue), "scrolled": Int(scroll.contentView.bounds.origin.y),
            "frame": [Int(window.frame.origin.x), Int(window.frame.origin.y),
                      Int(window.frame.size.width), Int(window.frame.size.height)],
            "pad": padEvents, "tiny": tinyClicks, "minimized": window.isMiniaturized,
            "chosen": chosen, "saved": saved,
        ]
        if let data = try? JSONSerialization.data(withJSONObject: state) {
            try? data.write(to: URL(fileURLWithPath: statePath))
        }
    }

    @objc func increment() { count += 1; label.stringValue = "Count: \(count)"; write() }
    // Two buttons with the same accessible name: only exact targeting tells them apart.
    @objc func addFirst() { adds[0] += 1; write() }
    @objc func addSecond() { adds[1] += 1; write() }
    @objc func changed() { write() }
    @objc func reset() { count = 0; label.stringValue = "Count: 0"; write() }
    func controlTextDidChange(_ notification: Notification) { write() }
    @objc func scrolled() { write() }
    @objc func tiny() { tinyClicks += 1; write() }
    // Real system file panels: what HighhX chooses or saves through them lands in the state.
    @objc func choose() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.begin { response in
            if response == .OK, let url = panel.url { self.chosen = url.path; self.write() }
        }
    }
    @objc func saveAs() {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = "untitled.txt"
        panel.begin { response in
            if response == .OK, let url = panel.url {
                try? "saved by the fixture\n".write(to: url, atomically: true, encoding: .utf8)
                self.saved = url.path
                self.write()
            }
        }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        // A second window of the same application (behind the main one): exact window targeting.
        second = NSWindow(contentRect: NSRect(x: 760, y: 200, width: 320, height: 200),
                          styleMask: [.titled], backing: .buffered, defer: false)
        second.title = "HighhX Fixture 2"
        second.orderFront(nil)
        window = NSWindow(contentRect: NSRect(x: 200, y: 200, width: 480, height: 400),
                          styleMask: [.titled, .closable, .resizable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "HighhX Fixture"
        window.setContentSize(NSSize(width: 480, height: 500))
        let button = NSButton(title: "Increment", target: self, action: #selector(increment))
        name.placeholderString = "Name"
        name.setAccessibilityLabel("Name")
        name.delegate = self
        email.placeholderString = "Email"
        email.setAccessibilityLabel("Email")
        email.delegate = self
        secret.setAccessibilityLabel("Password")
        secret.widthAnchor.constraint(equalToConstant: 140).isActive = true
        agree.target = self; agree.action = #selector(changed)
        volume.target = self; volume.action = #selector(changed)
        volume.setAccessibilityLabel("Volume")
        let text = NSTextView(frame: NSRect(x: 0, y: 0, width: 400, height: 2000))
        text.string = (1...200).map { "Line \($0)" }.joined(separator: "\n")
        scroll.documentView = text
        scroll.hasVerticalScroller = true
        scroll.setAccessibilityLabel("Lines")
        scroll.contentView.postsBoundsChangedNotifications = true
        NotificationCenter.default.addObserver(self, selector: #selector(scrolled),
                                               name: NSView.boundsDidChangeNotification, object: scroll.contentView)
        let pair = NSStackView(views: [NSButton(title: "Add", target: self, action: #selector(addFirst)),
                                       NSButton(title: "Add", target: self, action: #selector(addSecond)),
                                       NSButton(title: "Choose…", target: self, action: #selector(choose)),
                                       NSButton(title: "Save As…", target: self, action: #selector(saveAs))])
        pair.orientation = .horizontal
        pad.setAccessibilityElement(true)
        pad.setAccessibilityRole(.group)
        pad.setAccessibilityLabel("Pad")
        pad.onEvent = { kind, _ in self.padEvents[kind, default: 0] += 1; self.write() }
        pad.widthAnchor.constraint(equalToConstant: 200).isActive = true
        pad.heightAnchor.constraint(equalToConstant: 60).isActive = true
        let tinyButton = NSButton(frame: NSRect(x: 0, y: 0, width: 10, height: 10))
        tinyButton.title = ""
        tinyButton.isBordered = false
        tinyButton.wantsLayer = true
        tinyButton.layer?.backgroundColor = NSColor.systemRed.cgColor
        tinyButton.target = self; tinyButton.action = #selector(tiny)
        tinyButton.setAccessibilityLabel("Tiny")
        tinyButton.widthAnchor.constraint(equalToConstant: 10).isActive = true
        tinyButton.heightAnchor.constraint(equalToConstant: 10).isActive = true
        let credentials = NSStackView(views: [email, secret])
        credentials.orientation = .horizontal
        let row = NSStackView(views: [pad, tinyButton])
        row.orientation = .horizontal
        row.alignment = .centerY
        let stack = NSStackView(views: [label, button, name, credentials, agree, volume, scroll, pair, row])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.edgeInsets = NSEdgeInsets(top: 16, left: 16, bottom: 16, right: 16)
        scroll.heightAnchor.constraint(equalToConstant: 80).isActive = true
        scroll.widthAnchor.constraint(equalToConstant: 400).isActive = true
        name.widthAnchor.constraint(equalToConstant: 300).isActive = true
        email.widthAnchor.constraint(equalToConstant: 220).isActive = true
        volume.widthAnchor.constraint(equalToConstant: 300).isActive = true
        window.contentView = stack

        let bar = NSMenu()
        let appItem = NSMenuItem(); bar.addItem(appItem)
        let appMenu = NSMenu(); appMenu.addItem(withTitle: "Quit", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        let fixtureItem = NSMenuItem(title: "Fixture", action: nil, keyEquivalent: ""); bar.addItem(fixtureItem)
        let fixtureMenu = NSMenu(title: "Fixture")
        fixtureMenu.addItem(withTitle: "Reset", action: #selector(reset), keyEquivalent: "").target = self
        fixtureItem.submenu = fixtureMenu
        // Like every Mac application: text fields receive copy/paste/undo shortcuts through these.
        let editItem = NSMenuItem(title: "Edit", action: nil, keyEquivalent: ""); bar.addItem(editItem)
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        editMenu.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = editMenu
        let windowItem = NSMenuItem(title: "Window", action: nil, keyEquivalent: ""); bar.addItem(windowItem)
        let windowMenu = NSMenu(title: "Window")
        windowMenu.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        windowItem.submenu = windowMenu
        NSApp.windowsMenu = windowMenu
        NSApp.mainMenu = bar
        NotificationCenter.default.addObserver(forName: NSWindow.didMiniaturizeNotification, object: window, queue: nil) { _ in self.write() }
        NotificationCenter.default.addObserver(forName: NSWindow.didDeminiaturizeNotification, object: window, queue: nil) { _ in self.write() }

        NotificationCenter.default.addObserver(forName: NSWindow.didResizeNotification, object: window, queue: nil) { _ in self.write() }
        NotificationCenter.default.addObserver(forName: NSWindow.didMoveNotification, object: window, queue: nil) { _ in self.write() }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        write()
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = Fixture(statePath: CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "/dev/null")
app.delegate = delegate
app.run()
