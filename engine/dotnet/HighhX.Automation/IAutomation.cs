using System.Text.Json.Nodes;

namespace HighhX.Automation;

/// <summary>The controlled action interface: the only things the engine can do to the OS.</summary>
public interface IAutomation
{
    JsonObject Status();
    JsonObject Frontmost();
    JsonObject Launch(string app);
    JsonObject Focus(string app);
    JsonObject Running(string app);
    JsonObject OpenUrl(string url, string app);
    JsonObject Click(string name, string role, string app);
    JsonObject Type(string text);
    JsonObject Key(string key, string[] modifiers);
    JsonObject Scroll(string direction, int amount);
    JsonObject Inspect(string app, int limit);
    JsonObject Verify(string check, string app, string name, string role);
}

/// <summary>Platforms without a native implementation: every operation says so.</summary>
public sealed class UnsupportedAutomation : IAutomation
{
    static ProtocolException No() =>
        new("unsupported_platform", "The HighhX .NET engine implements macOS only; browser actions work everywhere.");

    public JsonObject Status() => new()
    {
        ["engine"] = "dotnet", ["protocol"] = Protocol.Version, ["platform"] = Environment.OSVersion.Platform.ToString(),
        ["ok"] = false, ["accessibility"] = false, ["detail"] = "desktop automation is implemented for macOS",
    };
    public JsonObject Frontmost() => throw No();
    public JsonObject Launch(string app) => throw No();
    public JsonObject Focus(string app) => throw No();
    public JsonObject Running(string app) => throw No();
    public JsonObject OpenUrl(string url, string app) => throw No();
    public JsonObject Click(string name, string role, string app) => throw No();
    public JsonObject Type(string text) => throw No();
    public JsonObject Key(string key, string[] modifiers) => throw No();
    public JsonObject Scroll(string direction, int amount) => throw No();
    public JsonObject Inspect(string app, int limit) => throw No();
    public JsonObject Verify(string check, string app, string name, string role) => throw No();
}
