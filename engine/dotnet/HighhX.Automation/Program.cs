// highhx-automation: the HighhX C#/.NET automation engine.
//
//   highhx-automation --stdio      serve the bridge protocol (one JSON object per line)
//   highhx-automation --protocol   print the protocol tables (for conformance tests)
//
// Every request is validated (Protocol.cs) before anything touches the OS; keyboard
// operations are refused while a terminal is frontmost.
using System.Text.Json;
using System.Text.Json.Nodes;
using HighhX.Automation;

if (args.Contains("--protocol"))
{
    Console.WriteLine(new JsonObject
    {
        ["version"] = Protocol.Version,
        ["ops"] = new JsonArray(Protocol.Ops.Select(o => (JsonNode)o).ToArray()),
        ["keys"] = new JsonArray(Protocol.KeyCodes.Keys.Order().Select(k => (JsonNode)k).ToArray()),
    }.ToJsonString());
    return 0;
}
if (!args.Contains("--stdio"))
{
    Console.Error.WriteLine("usage: highhx-automation --stdio | --protocol");
    return 2;
}

IAutomation automation = OperatingSystem.IsMacOS() ? new MacAutomation() : new UnsupportedAutomation();
var dispatcher = new Dispatcher(automation);
string? line;
while ((line = Console.In.ReadLine()) is not null)
{
    if (string.IsNullOrWhiteSpace(line)) continue;
    Console.Out.WriteLine(dispatcher.Handle(line).ToJsonString());
    Console.Out.Flush();
}
return 0;

namespace HighhX.Automation
{
    public sealed class Dispatcher(IAutomation automation)
    {
        public JsonObject Handle(string line)
        {
            JsonNode? id = null;
            try
            {
                var request = JsonNode.Parse(line) as JsonObject ?? throw new ProtocolException("invalid_request", "expected an object");
                id = request["id"]?.DeepClone();
                if (request["v"]?.GetValue<int>() != Protocol.Version)
                    throw new ProtocolException("invalid_request", $"protocol version {Protocol.Version} expected");
                var op = request["op"]?.GetValue<string>() ?? throw new ProtocolException("invalid_request", "needs op");
                var args = request["args"] as JsonObject ?? [];
                var result = Run(op, args);
                return new JsonObject { ["v"] = Protocol.Version, ["id"] = id, ["ok"] = true, ["result"] = result };
            }
            catch (ProtocolException exc)
            {
                return Error(id, exc.Code, exc.Message);
            }
            catch (Exception exc) when (exc is JsonException or InvalidOperationException or FormatException)
            {
                return Error(id, "invalid_request", exc.Message);
            }
            catch (Exception exc)
            {
                return Error(id, "failed", exc.Message);
            }
        }

        static JsonObject Error(JsonNode? id, string code, string message) => new()
        {
            ["v"] = Protocol.Version,
            ["id"] = id,
            ["ok"] = false,
            ["error"] = new JsonObject { ["code"] = code, ["message"] = message },
        };

        JsonObject Run(string op, JsonObject args)
        {
            if (!Protocol.Ops.Contains(op)) throw new ProtocolException("unknown_op", $"unknown operation {op}");
            if (Protocol.KeyboardOps.Contains(op))
            {
                var front = automation.Frontmost()["app"]?.GetValue<string>() ?? "";
                if (Protocol.IsTerminal(front))
                    throw new ProtocolException("refused", $"{front} is a terminal: HighhX never types or presses keys into a terminal.");
            }
            return op switch
            {
                "status" => automation.Status(),
                "frontmost" => automation.Frontmost(),
                "launch" => automation.Launch(Protocol.App(args)),
                "focus" => automation.Focus(Protocol.App(args)),
                "running" => automation.Running(Protocol.App(args)),
                "open_url" => automation.OpenUrl(Protocol.Url(args), Protocol.App(args, required: false)),
                "click" => automation.Click(
                    Protocol.Text(args, "name"),
                    Protocol.Choice(args, "role", Protocol.Roles, required: false),
                    Protocol.App(args, required: false)),
                "type" => automation.Type(Protocol.Text(args, "text", 2000)),
                "key" => automation.Key(Protocol.Key(args), []),
                "hotkey" => automation.Key(Protocol.Key(args), Protocol.ModifierList(args)),
                "scroll" => automation.Scroll(
                    Protocol.Choice(args, "direction", Protocol.ScrollDirections),
                    Protocol.Number(args, "amount", 1, 20, 1)),
                "wait" => Wait(Protocol.Number(args, "ms", 0, 10_000, 0)),
                "inspect" => automation.Inspect(Protocol.App(args, required: false), Protocol.Number(args, "limit", 1, 300, 100)),
                "verify" => automation.Verify(
                    Protocol.Choice(args, "check", Protocol.Checks),
                    Protocol.App(args, required: false),
                    Protocol.Text(args, "name", required: false),
                    Protocol.Choice(args, "role", Protocol.Roles, required: false)),
                _ => throw new ProtocolException("unknown_op", op),
            };
        }

        static JsonObject Wait(int ms)
        {
            Thread.Sleep(ms);
            return new JsonObject { ["ms"] = ms };
        }
    }
}
