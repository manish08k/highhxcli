// HighhX desktop fixture: a small AppKit application with one of each control the computer
// runtime acts on. After every change it writes its state as JSON to the path given as its
// first argument, so tests verify real effects (a count went up, text arrived, a slider moved),
// not just that an event was sent. Built by tests/e2e/test_desktop_live.py with swiftc.
import AppKit

final class Fixture: NSObject, NSApplicationDelegate, NSTextFieldDelegate {
    let statePath: String
    var window: NSWindow!
    var count = 0
    let label = NSTextField(labelWithString: "Count: 0")
    let name = NSTextField(string: "")
    let agree = NSButton(checkboxWithTitle: "Agree", target: nil, action: nil)
    let volume = NSSlider(value: 0, minValue: 0, maxValue: 100, target: nil, action: nil)
    let scroll = NSScrollView()

    init(statePath: String) { self.statePath = statePath }

    func write() {
        let state: [String: Any] = [
            "count": count, "name": name.stringValue, "agree": agree.state == .on,
            "volume": Int(volume.doubleValue), "scrolled": Int(scroll.contentView.bounds.origin.y),
            "frame": [Int(window.frame.origin.x), Int(window.frame.origin.y),
                      Int(window.frame.size.width), Int(window.frame.size.height)],
        ]
        if let data = try? JSONSerialization.data(withJSONObject: state) {
            try? data.write(to: URL(fileURLWithPath: statePath))
        }
    }

    @objc func increment() { count += 1; label.stringValue = "Count: \(count)"; write() }
    @objc func changed() { write() }
    @objc func reset() { count = 0; label.stringValue = "Count: 0"; write() }
    func controlTextDidChange(_ notification: Notification) { write() }
    @objc func scrolled() { write() }

    func applicationDidFinishLaunching(_ notification: Notification) {
        window = NSWindow(contentRect: NSRect(x: 200, y: 200, width: 480, height: 360),
                          styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        window.title = "HighhX Fixture"
        let button = NSButton(title: "Increment", target: self, action: #selector(increment))
        name.placeholderString = "Name"
        name.setAccessibilityLabel("Name")
        name.delegate = self
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
        let stack = NSStackView(views: [label, button, name, agree, volume, scroll])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.edgeInsets = NSEdgeInsets(top: 16, left: 16, bottom: 16, right: 16)
        scroll.heightAnchor.constraint(equalToConstant: 120).isActive = true
        scroll.widthAnchor.constraint(equalToConstant: 400).isActive = true
        name.widthAnchor.constraint(equalToConstant: 300).isActive = true
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
        NSApp.mainMenu = bar

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
