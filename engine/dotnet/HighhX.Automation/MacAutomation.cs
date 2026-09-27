// macOS implementation of the controlled action interface:
//   applications  NSWorkspace (Objective-C runtime) and /usr/bin/open with a fixed argv
//   UI elements   the Accessibility API (AXUIElement*) — by accessible role and name only
//   keyboard      CGEvent keyboard events (virtual key codes, or Unicode text)
//   scrolling     CGEvent scroll-wheel events
// Nothing here runs a shell or evaluates a script.
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text.Json.Nodes;

namespace HighhX.Automation;

public sealed class MacAutomation : IAutomation
{
    // ------------------------------------------------------------------ native
    const string AppServices = "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices";
    const string CoreFoundation = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation";
    const string ObjC = "/usr/lib/libobjc.A.dylib";
    const uint Utf8 = 0x08000100;

    [DllImport(AppServices)] static extern bool AXIsProcessTrusted();
    [DllImport(AppServices)] static extern IntPtr AXUIElementCreateApplication(int pid);
    [DllImport(AppServices)] static extern int AXUIElementCopyAttributeValue(IntPtr element, IntPtr attribute, out IntPtr value);
    [DllImport(AppServices)] static extern int AXUIElementPerformAction(IntPtr element, IntPtr action);
    [DllImport(AppServices)] static extern IntPtr CGEventCreateKeyboardEvent(IntPtr source, ushort key, bool down);
    [DllImport(AppServices)] static extern void CGEventKeyboardSetUnicodeString(IntPtr ev, nint length, char[] text);
    [DllImport(AppServices)] static extern void CGEventSetFlags(IntPtr ev, ulong flags);
    [DllImport(AppServices)] static extern void CGEventPost(uint tap, IntPtr ev);
    [DllImport(AppServices)] static extern IntPtr CGEventCreateScrollWheelEvent2(IntPtr source, uint units, uint count, int wheel1, int wheel2, int wheel3);

    [DllImport(CoreFoundation)] static extern IntPtr CFStringCreateWithCString(IntPtr alloc, string text, uint encoding);
    [DllImport(CoreFoundation)] static extern bool CFStringGetCString(IntPtr text, byte[] buffer, nint size, uint encoding);
    [DllImport(CoreFoundation)] static extern nint CFGetTypeID(IntPtr value);
    [DllImport(CoreFoundation)] static extern nint CFStringGetTypeID();
    [DllImport(CoreFoundation)] static extern nint CFBooleanGetTypeID();
    [DllImport(CoreFoundation)] static extern bool CFBooleanGetValue(IntPtr value);
    [DllImport(CoreFoundation)] static extern nint CFArrayGetCount(IntPtr array);
    [DllImport(CoreFoundation)] static extern IntPtr CFArrayGetValueAtIndex(IntPtr array, nint index);
    [DllImport(CoreFoundation)] static extern void CFRelease(IntPtr value);

    [DllImport(ObjC)] static extern IntPtr objc_getClass(string name);
    [DllImport(ObjC)] static extern IntPtr sel_registerName(string name);
    [DllImport(ObjC, EntryPoint = "objc_msgSend")] static extern IntPtr Send(IntPtr target, IntPtr selector);
    [DllImport(ObjC, EntryPoint = "objc_msgSend")] static extern IntPtr SendIndex(IntPtr target, IntPtr selector, nuint index);
    [DllImport(ObjC, EntryPoint = "objc_msgSend")] static extern int SendInt(IntPtr target, IntPtr selector);
    [DllImport(ObjC, EntryPoint = "objc_msgSend")] static extern nuint SendCount(IntPtr target, IntPtr selector);
    [DllImport(ObjC, EntryPoint = "objc_msgSend")] static extern bool SendActivate(IntPtr target, IntPtr selector, nuint options);

    const ulong FlagShift = 0x20000, FlagControl = 0x40000, FlagOption = 0x80000, FlagCommand = 0x100000;
    const nuint ActivateIgnoringOtherApps = 2;

    static readonly Dictionary<char, ushort> CharCodes = new()
    {
        ['a'] = 0, ['s'] = 1, ['d'] = 2, ['f'] = 3, ['h'] = 4, ['g'] = 5, ['z'] = 6, ['x'] = 7, ['c'] = 8, ['v'] = 9,
        ['b'] = 11, ['q'] = 12, ['w'] = 13, ['e'] = 14, ['r'] = 15, ['y'] = 16, ['t'] = 17, ['1'] = 18, ['2'] = 19,
        ['3'] = 20, ['4'] = 21, ['6'] = 22, ['5'] = 23, ['='] = 24, ['9'] = 25, ['7'] = 26, ['-'] = 27, ['8'] = 28,
        ['0'] = 29, [']'] = 30, ['o'] = 31, ['u'] = 32, ['['] = 33, ['i'] = 34, ['p'] = 35, ['l'] = 37, ['j'] = 38,
        ['\''] = 39, ['k'] = 40, [';'] = 41, ['\\'] = 42, [','] = 43, ['/'] = 44, ['n'] = 45, ['m'] = 46, ['.'] = 47,
        ['`'] = 50,
    };

    static readonly Dictionary<string, string> Roles = new()
    {
        ["AXButton"] = "button", ["AXMenuButton"] = "button", ["AXLink"] = "link", ["AXTextField"] = "textbox",
        ["AXTextArea"] = "textbox", ["AXSearchField"] = "textbox", ["AXComboBox"] = "textbox",
        ["AXCheckBox"] = "checkbox", ["AXMenuItem"] = "menuitem", ["AXTab"] = "tab", ["AXRadioButton"] = "tab",
    };

    // ------------------------------------------------------------------ helpers
    static IntPtr CF(string text) => CFStringCreateWithCString(IntPtr.Zero, text, Utf8);

    static string NS(IntPtr nsString)
    {
        if (nsString == IntPtr.Zero) return "";
        var utf8 = Send(nsString, sel_registerName("UTF8String"));
        return Marshal.PtrToStringUTF8(utf8) ?? "";
    }

    static string CFText(IntPtr value)
    {
        if (value == IntPtr.Zero || CFGetTypeID(value) != CFStringGetTypeID()) return "";
        var buffer = new byte[4096];
        return CFStringGetCString(value, buffer, buffer.Length, Utf8)
            ? System.Text.Encoding.UTF8.GetString(buffer, 0, Array.IndexOf(buffer, (byte)0) is var n and >= 0 ? n : buffer.Length)
            : "";
    }

    static IntPtr Attribute(IntPtr element, string name)
    {
        var key = CF(name);
        try
        {
            return AXUIElementCopyAttributeValue(element, key, out var value) == 0 ? value : IntPtr.Zero;
        }
        finally { CFRelease(key); }
    }

    static string TextAttribute(IntPtr element, string name)
    {
        var value = Attribute(element, name);
        if (value == IntPtr.Zero) return "";
        try { return CFText(value); } finally { CFRelease(value); }
    }

    static bool BoolAttribute(IntPtr element, string name, bool fallback)
    {
        var value = Attribute(element, name);
        if (value == IntPtr.Zero) return fallback;
        try { return CFGetTypeID(value) == CFBooleanGetTypeID() ? CFBooleanGetValue(value) : fallback; }
        finally { CFRelease(value); }
    }

    static void RequireTrusted()
    {
        if (!AXIsProcessTrusted())
            throw new ProtocolException("accessibility_denied", "Accessibility access is not granted to the HighhX engine.");
    }

    record RunningApp(IntPtr Handle, string Name, int Pid);

    static IEnumerable<RunningApp> Apps()
    {
        var workspace = Send(objc_getClass("NSWorkspace"), sel_registerName("sharedWorkspace"));
        var apps = Send(workspace, sel_registerName("runningApplications"));
        var count = SendCount(apps, sel_registerName("count"));
        for (nuint i = 0; i < count; i++)
        {
            var app = SendIndex(apps, sel_registerName("objectAtIndex:"), i);
            yield return new RunningApp(app, NS(Send(app, sel_registerName("localizedName"))), SendInt(app, sel_registerName("processIdentifier")));
        }
    }

    static RunningApp? Find(string name) =>
        Apps().FirstOrDefault(a => string.Equals(a.Name, name, StringComparison.OrdinalIgnoreCase));

    static RunningApp FrontApp()
    {
        var workspace = Send(objc_getClass("NSWorkspace"), sel_registerName("sharedWorkspace"));
        var app = Send(workspace, sel_registerName("frontmostApplication"));
        return new RunningApp(app, NS(Send(app, sel_registerName("localizedName"))), SendInt(app, sel_registerName("processIdentifier")));
    }

    static void Open(params string[] arguments)
    {
        var start = new ProcessStartInfo("/usr/bin/open") { RedirectStandardError = true, UseShellExecute = false };
        foreach (var argument in arguments) start.ArgumentList.Add(argument);
        using var process = Process.Start(start) ?? throw new ProtocolException("failed", "could not start /usr/bin/open");
        process.WaitForExit(30_000);
        if (process.ExitCode != 0)
            throw new ProtocolException("not_found", process.StandardError.ReadToEnd().Trim());
    }

    static bool Poll(Func<bool> check, int milliseconds)
    {
        var deadline = Environment.TickCount64 + milliseconds;
        while (true)
        {
            if (check()) return true;
            if (Environment.TickCount64 >= deadline) return false;
            Thread.Sleep(250);
        }
    }

    record Element(IntPtr Handle, string Role, string Name, string Value, bool Enabled, bool Focused);

    static List<Element> Elements(string app, int limit)
    {
        RequireTrusted();
        var target = string.IsNullOrEmpty(app) ? FrontApp() : Find(app)
            ?? throw new ProtocolException("not_found", $"{app} is not running");
        var root = AXUIElementCreateApplication(target.Pid);
        var found = new List<Element>();
        var queue = new Queue<IntPtr>();
        var windows = Attribute(root, "AXWindows");
        if (windows != IntPtr.Zero && CFArrayGetCount(windows) > 0) queue.Enqueue(CFArrayGetValueAtIndex(windows, 0));
        var visited = 0;
        while (queue.Count > 0 && found.Count < limit && visited < 3000)
        {
            var element = queue.Dequeue();
            visited++;
            var role = TextAttribute(element, "AXRole");
            if (Roles.TryGetValue(role, out var mapped))
            {
                var name = TextAttribute(element, "AXTitle");
                if (name.Length == 0) name = TextAttribute(element, "AXDescription");
                var secure = role == "AXSecureTextField";
                found.Add(new Element(element, mapped, name, secure ? "" : TextAttribute(element, "AXValue"),
                    BoolAttribute(element, "AXEnabled", true), BoolAttribute(element, "AXFocused", false)));
            }
            var children = Attribute(element, "AXChildren");
            if (children == IntPtr.Zero) continue;
            for (nint i = 0; i < CFArrayGetCount(children); i++) queue.Enqueue(CFArrayGetValueAtIndex(children, i));
        }
        return found;
    }

    static Element? Match(List<Element> elements, string name, string role)
    {
        var candidates = elements.Where(e => role is "" or "any" || e.Role == role).ToList();
        var exact = candidates.FirstOrDefault(e => string.Equals(e.Name, name, StringComparison.OrdinalIgnoreCase));
        if (exact is not null) return exact;
        var partial = candidates.Where(e => e.Name.Contains(name, StringComparison.OrdinalIgnoreCase)).ToList();
        if (partial.Count > 1) throw new ProtocolException("not_found", $"'{name}' matches several elements; use the exact name");
        return partial.FirstOrDefault();
    }

    static void Post(ushort code, ulong flags, char[]? text = null)
    {
        foreach (var down in new[] { true, false })
        {
            var ev = CGEventCreateKeyboardEvent(IntPtr.Zero, code, down);
            if (text is not null) CGEventKeyboardSetUnicodeString(ev, text.Length, text);
            CGEventSetFlags(ev, flags);
            CGEventPost(0, ev);
            CFRelease(ev);
        }
    }

    // ------------------------------------------------------------------ operations
    public JsonObject Status() => new()
    {
        ["engine"] = "dotnet",
        ["protocol"] = Protocol.Version,
        ["version"] = typeof(MacAutomation).Assembly.GetName().Version?.ToString() ?? "",
        ["platform"] = "darwin",
        ["accessibility"] = AXIsProcessTrusted(),
        ["ok"] = AXIsProcessTrusted(),
        ["detail"] = AXIsProcessTrusted()
            ? "Accessibility API and CGEvent (native)"
            : "Accessibility is not granted: System Settings → Privacy & Security → Accessibility",
    };

    public JsonObject Frontmost()
    {
        var front = FrontApp();
        var title = "";
        if (AXIsProcessTrusted())
        {
            var window = Attribute(AXUIElementCreateApplication(front.Pid), "AXFocusedWindow");
            if (window != IntPtr.Zero) title = TextAttribute(window, "AXTitle");
        }
        return new JsonObject { ["app"] = front.Name, ["title"] = title };
    }

    public JsonObject Running(string app) => new() { ["app"] = app, ["running"] = Find(app) is not null };

    public JsonObject Launch(string app)
    {
        Open("-a", app);
        return new JsonObject { ["app"] = app, ["running"] = Poll(() => Find(app) is not null, 10_000) };
    }

    public JsonObject Focus(string app)
    {
        var target = Find(app) ?? throw new ProtocolException("not_found", $"{app} is not running");
        SendActivate(target.Handle, sel_registerName("activateWithOptions:"), ActivateIgnoringOtherApps);
        var front = "";
        var ok = Poll(() => string.Equals(front = FrontApp().Name, app, StringComparison.OrdinalIgnoreCase), 3000);
        return new JsonObject { ["app"] = app, ["frontmost"] = ok, ["actual"] = front };
    }

    public JsonObject OpenUrl(string url, string app)
    {
        if (app.Length > 0) Open("-a", app, url); else Open(url);
        return new JsonObject { ["url"] = url, ["app"] = app.Length > 0 ? app : "default browser" };
    }

    public JsonObject Click(string name, string role, string app)
    {
        var element = Match(Elements(app, 400), name, role)
            ?? throw new ProtocolException("not_found", $"No {(role.Length > 0 ? role : "element")} named '{name}'");
        var press = CF("AXPress");
        try
        {
            if (AXUIElementPerformAction(element.Handle, press) != 0)
                throw new ProtocolException("failed", $"'{element.Name}' could not be pressed");
        }
        finally { CFRelease(press); }
        return new JsonObject { ["role"] = element.Role, ["name"] = element.Name };
    }

    public JsonObject Type(string text)
    {
        RequireTrusted();
        foreach (var chunk in text.Chunk(20)) Post(0, 0, chunk);
        return new JsonObject { ["characters"] = text.Length };
    }

    public JsonObject Key(string key, string[] modifiers)
    {
        RequireTrusted();
        ulong flags = 0;
        foreach (var modifier in modifiers)
            flags |= modifier switch
            {
                "command" => FlagCommand, "control" => FlagControl, "option" => FlagOption, "shift" => FlagShift, _ => 0,
            };
        if (Protocol.KeyCodes.TryGetValue(key, out var code)) Post(code, flags);
        else if (CharCodes.TryGetValue(char.ToLowerInvariant(key[0]), out var charCode)) Post(charCode, flags);
        else Post(0, flags, [key[0]]);
        return new JsonObject { ["keys"] = string.Join("+", modifiers.Append(key)) };
    }

    public JsonObject Scroll(string direction, int amount)
    {
        RequireTrusted();
        var (vertical, horizontal) = direction switch
        {
            "up" => (5, 0), "down" => (-5, 0), "left" => (0, 5), _ => (0, -5),
        };
        for (var i = 0; i < amount; i++)
        {
            var ev = CGEventCreateScrollWheelEvent2(IntPtr.Zero, 1, 2, vertical, horizontal, 0);
            CGEventPost(0, ev);
            CFRelease(ev);
        }
        return new JsonObject { ["direction"] = direction, ["amount"] = amount };
    }

    public JsonObject Inspect(string app, int limit)
    {
        var elements = Elements(app, limit);
        var list = new JsonArray();
        foreach (var e in elements)
            list.Add(new JsonObject
            {
                ["role"] = e.Role, ["name"] = e.Name, ["value"] = e.Value, ["enabled"] = e.Enabled, ["focused"] = e.Focused,
            });
        return new JsonObject { ["app"] = app.Length > 0 ? app : FrontApp().Name, ["elements"] = list };
    }

    public JsonObject Verify(string check, string app, string name, string role)
    {
        switch (check)
        {
            case "frontmost":
            {
                var front = FrontApp().Name;
                return new JsonObject { ["ok"] = string.Equals(front, app, StringComparison.OrdinalIgnoreCase), ["detail"] = $"{front} is frontmost" };
            }
            case "running":
                return new JsonObject { ["ok"] = Find(app) is not null, ["detail"] = app };
            case "window":
            {
                RequireTrusted();
                var target = Find(app) ?? throw new ProtocolException("not_found", $"{app} is not running");
                var windows = Attribute(AXUIElementCreateApplication(target.Pid), "AXWindows");
                var count = windows == IntPtr.Zero ? 0 : CFArrayGetCount(windows);
                return new JsonObject { ["ok"] = count > 0, ["detail"] = $"{count} window(s)" };
            }
            default:
                return new JsonObject { ["ok"] = Match(Elements(app, 400), name, role) is not null, ["detail"] = name };
        }
    }
}
