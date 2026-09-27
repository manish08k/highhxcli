// The automation-bridge protocol, version 1 — the C# side of
// src/highhx/automation/engine/protocol.py. Both sides validate every request; the tables
// below must match the Python ones (tests/unit/automation/test_engine.py checks that).
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace HighhX.Automation;

public sealed class ProtocolException(string code, string message) : Exception(message)
{
    public string Code { get; } = code;
}

public static partial class Protocol
{
    public const int Version = 1;

    public static readonly string[] Ops =
    [
        "status", "frontmost", "launch", "focus", "running", "open_url", "click",
        "type", "key", "hotkey", "scroll", "wait", "inspect", "verify",
    ];

    public static readonly string[] KeyboardOps = ["type", "key", "hotkey"];

    public static readonly Dictionary<string, ushort> KeyCodes = new()
    {
        ["enter"] = 36, ["return"] = 36, ["tab"] = 48, ["space"] = 49, ["delete"] = 51,
        ["backspace"] = 51, ["escape"] = 53, ["esc"] = 53, ["left"] = 123, ["right"] = 124,
        ["down"] = 125, ["up"] = 126, ["arrowleft"] = 123, ["arrowright"] = 124,
        ["arrowdown"] = 125, ["arrowup"] = 126, ["pageup"] = 116, ["pagedown"] = 121,
        ["home"] = 115, ["end"] = 119,
    };

    public static readonly string[] Modifiers = ["command", "control", "option", "shift"];

    public static readonly string[] Roles = ["button", "link", "textbox", "checkbox", "menuitem", "tab", "field", "any"];

    public static readonly string[] ScrollDirections = ["up", "down", "left", "right"];

    public static readonly string[] Checks = ["frontmost", "running", "window", "element"];

    public static readonly string[] TerminalApps =
        ["terminal", "iterm", "iterm2", "warp", "alacritty", "kitty", "hyper", "wezterm", "ghostty", "tabby"];

    [GeneratedRegex(@"^[\w .&+'()-]{1,100}$")]
    private static partial Regex AppName();

    public static bool IsTerminal(string app)
    {
        var low = app.ToLowerInvariant();
        return TerminalApps.Any(low.Contains);
    }

    public static string Text(JsonObject args, string name, int maxLength = 200, bool required = true)
    {
        var value = args[name]?.GetValue<string>()?.Trim();
        if (string.IsNullOrEmpty(value))
        {
            if (required) throw new ProtocolException("invalid_request", $"needs {name}");
            return "";
        }
        if (value.Length > maxLength) throw new ProtocolException("invalid_request", $"{name} is too long");
        if (value.Any(c => c < ' ' && c != '\t' && c != '\n'))
            throw new ProtocolException("invalid_request", $"{name} contains control characters");
        return value;
    }

    public static string App(JsonObject args, bool required = true)
    {
        var app = Text(args, "app", 100, required);
        if (app.Length > 0 && !AppName().IsMatch(app))
            throw new ProtocolException("invalid_request", $"not an application name: {app}");
        return app;
    }

    public static string Choice(JsonObject args, string name, string[] choices, bool required = true)
    {
        var value = Text(args, name, 40, required).ToLowerInvariant();
        if (value.Length == 0) return value;
        if (!choices.Contains(value)) throw new ProtocolException("invalid_request", $"{name} must be one of {string.Join(", ", choices)}");
        return value;
    }

    public static string Key(JsonObject args)
    {
        var key = Text(args, "key", 20).ToLowerInvariant();
        if (!KeyCodes.ContainsKey(key) && key.Length != 1) throw new ProtocolException("invalid_request", $"unknown key {key}");
        return key;
    }

    public static int Number(JsonObject args, string name, int low, int high, int fallback)
    {
        if (args[name] is null) return fallback;
        var value = args[name]!.GetValue<int>();
        if (value < low || value > high) throw new ProtocolException("invalid_request", $"{name} must be {low}–{high}");
        return value;
    }

    public static string[] ModifierList(JsonObject args)
    {
        if (args["modifiers"] is not JsonArray list || list.Count is < 1 or > 3)
            throw new ProtocolException("invalid_request", "modifiers must list 1–3 modifiers");
        var names = list.Select(m => m!.GetValue<string>().ToLowerInvariant()).Distinct().ToArray();
        if (names.Any(m => !Modifiers.Contains(m))) throw new ProtocolException("invalid_request", "unknown modifier");
        return names;
    }

    public static string Url(JsonObject args)
    {
        var url = Text(args, "url", 2000);
        if (!Uri.TryCreate(url, UriKind.Absolute, out var uri) || (uri.Scheme != "http" && uri.Scheme != "https"))
            throw new ProtocolException("invalid_request", "url must be an http(s) URL");
        return uri.AbsoluteUri;
    }
}
