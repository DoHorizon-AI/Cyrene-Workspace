using System.Text.Json;
using AstrBot.DotNetHost.Contracts;
using AstrBot.DotNetHost.Services;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.DependencyInjection.Extensions;
using Microsoft.Extensions.Hosting;
using Npgsql;

namespace Cyrene.Phase7.HostDriver;

public static class DriverMain
{
    internal const string MainBinding = "qq-main";
    internal const string SecondaryBinding = "qq-secondary";
    private const string SharedSessionId = "424242";
    private const string SharedPlatformId = "aiocqhttp";

    public static async Task<int> Main()
    {
        try
        {
            await RunAsync();
            return 0;
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine($"PHASE7_HOST_FAIL: {exception}");
            return 1;
        }
    }

    private static async Task RunAsync()
    {
        var astrBotRoot = RequiredPath("CYRENE_PHASE7_ASTRBOT_ROOT");
        var dataRoot = RequiredPath("CYRENE_PHASE7_DATA_ROOT");
        var database = Required("CYRENE_PHASE7_DATABASE");
        var cesEndpoint = Required("CYRENE_PHASE7_CES_ENDPOINT");
        var mainState = RequiredPath("CYRENE_PHASE7_MAIN_STATE");
        var secondaryState = RequiredPath("CYRENE_PHASE7_SECONDARY_STATE");
        Directory.CreateDirectory(dataRoot);
        ConfigureTestEnvironment(dataRoot, database, cesEndpoint);

        using var factory = new Phase7HostFactory(
            astrBotRoot,
            dataRoot,
            database,
            cesEndpoint);
        using var client = factory.CreateClient();

        await WaitForActionAsync(mainState, MainBinding);
        await WaitForActionAsync(secondaryState, SecondaryBinding);
        AssertPeerState(mainState, MainBinding);
        AssertPeerState(secondaryState, SecondaryBinding);
        await AssertDatabaseAsync(database);

        Console.WriteLine("PHASE7_HOST_PASS");
        Console.WriteLine("  real AstrBot WebApplicationFactory host: PASS");
        Console.WriteLine("  PostgreSQL session/history isolation: PASS");
        Console.WriteLine("  qq-main -> qq-main and qq-secondary -> qq-secondary: PASS");
    }

    private static void ConfigureTestEnvironment(
        string dataRoot,
        string database,
        string cesEndpoint)
    {
        var values = new Dictionary<string, string?>
        {
            ["ASTRBOT_ROOT"] = dataRoot,
            ["DOTNET_ENVIRONMENT"] = "Development",
            ["ASPNETCORE_ENVIRONMENT"] = "Development",
            ["Runtime__Mode"] = "Development",
            ["DeveloperCompatibility__PythonPluginsEnabled"] = "false",
            ["CompatibilityBridge__Enabled"] = "false",
            ["ProviderPersistence__KeyRingPath"] = Path.Combine(dataRoot, "keys"),
            ["ProviderPersistence__RequireKeyEncryption"] = "false",
            ["ConnectionStrings__AstrBot"] = database,
            ["MediaProcessor__ServiceEndpoint"] = cesEndpoint,
            ["MessageConnector__Enabled"] = "true",
            ["MessageConnector__InstanceIds__0"] = MainBinding,
            ["MessageConnector__InstanceIds__1"] = SecondaryBinding,
            ["MessageConnector__ReconnectDelayMilliseconds"] = "100",
            ["HostPipeline__EnableHostPlatformSend"] = "true",
            ["HostPipeline__WakePrefixes__0"] = "/",
            ["HostPipeline__EnableRateLimit"] = "false",
            ["HostPipeline__EnableWhitelist"] = "false",
            ["HostPipeline__EnableSmartFilter"] = "false",
            ["HostRuntimeOptions__EnableWorkerSupervision"] = "false",
            ["HostRuntimeOptions__EnableMessageMerge"] = "false",
            ["HostRuntimeOptions__MergeWindowSeconds"] = "0",
            ["HostRuntimeOptions__WorkerTimeoutMs"] = "5000",
            ["HostRuntimeOptions__SendTimeoutMs"] = "5000",
            ["Iris__Enabled"] = "false",
            ["KnowledgeBaseRuntime__Enabled"] = "false",
            ["HostOneBot__Enabled"] = "false",
            ["PythonWorker__EnableCompatibilityProxy"] = "false",
            ["PythonWorker__EnableHealthProbe"] = "false",
            ["PythonCapabilities__Enabled"] = "false",
        };

        foreach (var (key, value) in values)
        {
            Environment.SetEnvironmentVariable(key, value);
        }
    }

    private static async Task WaitForActionAsync(PathInfo statePath, string bindingId)
    {
        var deadline = DateTimeOffset.UtcNow.AddSeconds(90);
        while (DateTimeOffset.UtcNow < deadline)
        {
            if (ReadRecords(statePath).Any(record =>
                    IsKind(record, "action") &&
                    GetString(record, "binding_id") == bindingId))
            {
                return;
            }

            await Task.Delay(100);
        }

        throw new TimeoutException($"Timed out waiting for {bindingId} outbound action.");
    }

    private static void AssertPeerState(PathInfo statePath, string bindingId)
    {
        var records = ReadRecords(statePath);
        Assert(
            records.Any(record => IsKind(record, "connected")),
            $"{bindingId} fake peer never observed a WebSocket connection.");
        Assert(
            records.Any(record => IsKind(record, "heartbeat")),
            $"{bindingId} fake peer did not emit the heartbeat fixture.");

        var events = records.Where(record => IsKind(record, "event")).ToArray();
        Assert(events.Length == 1, $"{bindingId} fake peer emitted {events.Length} events, expected one.");
        Assert(
            events.All(record => GetString(record, "binding_id") == bindingId),
            $"{bindingId} event record crossed a configured binding boundary.");

        var actions = records.Where(record => IsKind(record, "action")).ToArray();
        Assert(actions.Length == 1, $"{bindingId} fake peer observed {actions.Length} actions, expected one.");
        foreach (var action in actions)
        {
            Assert(
                GetString(action, "binding_id") == bindingId,
                $"{bindingId} action record has the wrong binding id.");
            Assert(
                GetString(action, "action") == "send_private_msg",
                $"{bindingId} used an unexpected OneBot action.");
            var parameters = action.GetProperty("params");
            Assert(
                parameters.GetProperty("user_id").GetInt64() == 424242,
                $"{bindingId} action used the wrong conversation target.");
            var message = parameters.GetProperty("message");
            Assert(
                message.EnumerateArray().Any(segment =>
                    segment.GetProperty("type").GetString() == "text" &&
                    segment.GetProperty("data").GetProperty("text").GetString() ==
                    $"reply:{bindingId}"),
                $"{bindingId} response did not preserve the binding-scoped reply.");
        }

        Assert(
            records.All(record => GetString(record, "binding_id") == bindingId),
            $"{bindingId} fake peer state contains another configured binding.");
    }

    private static async Task AssertDatabaseAsync(string connectionString)
    {
        await using var connection = new NpgsqlConnection(connectionString);
        await connection.OpenAsync();

        var sessionCount = await ScalarAsync(
            connection,
            """
            SELECT COUNT(*) FROM platform_sessions
            WHERE platform_id = @platform AND session_id = @session
            """,
            null);
        Assert(sessionCount == 2, $"expected two isolated platform sessions, found {sessionCount}.");

        foreach (var bindingId in new[] { MainBinding, SecondaryBinding })
        {
            var scopedSessions = await ScalarAsync(
                connection,
                """
                SELECT COUNT(*) FROM platform_sessions
                WHERE platform_id = @platform AND session_id = @session
                  AND connector_instance_id = @binding
                  AND bot_instance_id = '' AND is_group = 0
                """,
                bindingId);
            Assert(scopedSessions == 1, $"{bindingId} has {scopedSessions} scoped sessions, expected one.");

            var scopedHistory = await ScalarAsync(
                connection,
                """
                SELECT COUNT(*) FROM platform_message_history
                WHERE platform_id = @platform AND user_id = @session
                  AND connector_instance_id = @binding
                  AND bot_instance_id = ''
                """,
                bindingId);
            Assert(scopedHistory >= 2, $"{bindingId} has {scopedHistory} history rows, expected user and assistant rows.");
        }

        var legacyRows = await ScalarAsync(
            connection,
            """
            SELECT COUNT(*) FROM platform_message_history
            WHERE platform_id = @platform AND user_id = @session
              AND connector_instance_id LIKE 'legacy:%'
            """,
            null);
        Assert(legacyRows == 0, "explicit connector events fell back to a legacy storage identity.");

        var distinctBindings = await ScalarAsync(
            connection,
            """
            SELECT COUNT(DISTINCT connector_instance_id)
            FROM platform_message_history
            WHERE platform_id = @platform AND user_id = @session
            """,
            null);
        Assert(distinctBindings == 2, $"history collapsed configured identities into {distinctBindings} binding(s).");
    }

    private static async Task<long> ScalarAsync(
        NpgsqlConnection connection,
        string sql,
        string? bindingId)
    {
        await using var command = new NpgsqlCommand(sql, connection);
        command.Parameters.AddWithValue("platform", SharedPlatformId);
        command.Parameters.AddWithValue("session", SharedSessionId);
        if (sql.Contains("@binding", StringComparison.Ordinal))
        {
            command.Parameters.AddWithValue("binding", bindingId ?? string.Empty);
        }

        return Convert.ToInt64(await command.ExecuteScalarAsync());
    }

    private static List<JsonElement> ReadRecords(PathInfo path)
    {
        if (!File.Exists(path.Value))
        {
            return [];
        }

        var records = new List<JsonElement>();
        foreach (var line in File.ReadLines(path.Value))
        {
            if (string.IsNullOrWhiteSpace(line))
            {
                continue;
            }

            using var document = JsonDocument.Parse(line);
            records.Add(document.RootElement.Clone());
        }

        return records;
    }

    private static bool IsKind(JsonElement record, string kind)
        => record.TryGetProperty("kind", out var value) && value.GetString() == kind;

    private static string? GetString(JsonElement record, string property)
        => record.TryGetProperty(property, out var value) ? value.GetString() : null;

    private static string Required(string name)
        => Environment.GetEnvironmentVariable(name) is { Length: > 0 } value
            ? value
            : throw new InvalidOperationException($"{name} is required.");

    private static PathInfo RequiredPath(string name) => new(Required(name));

    private static void Assert(bool condition, string message)
    {
        if (!condition)
        {
            throw new InvalidOperationException(message);
        }
    }
}

internal readonly record struct PathInfo(string Value)
{
    public static implicit operator string(PathInfo path) => path.Value;
}

internal sealed class Phase7HostFactory(
    string astrBotRoot,
    string dataRoot,
    string database,
    string cesEndpoint) : WebApplicationFactory<global::Program>
{
    private IHost? _host;

    protected override IHost CreateHost(IHostBuilder builder)
    {
        _host = base.CreateHost(builder);
        return _host;
    }

    protected override void ConfigureWebHost(IWebHostBuilder builder)
    {
        builder.UseEnvironment("Development");
        builder.UseContentRoot(astrBotRoot);
        builder.ConfigureAppConfiguration((_, configuration) =>
        {
            configuration.AddInMemoryCollection(new Dictionary<string, string?>
            {
                ["ASTRBOT_ROOT"] = dataRoot,
                ["Runtime:Mode"] = "Development",
                ["DeveloperCompatibility:PythonPluginsEnabled"] = "false",
                ["CompatibilityBridge:Enabled"] = "false",
                ["ProviderPersistence:KeyRingPath"] = Path.Combine(dataRoot, "keys"),
                ["ProviderPersistence:RequireKeyEncryption"] = "false",
                ["ConnectionStrings:AstrBot"] = database,
                ["MediaProcessor:ServiceEndpoint"] = cesEndpoint,
                ["MessageConnector:Enabled"] = "true",
                ["MessageConnector:InstanceIds:0"] = DriverMain.MainBinding,
                ["MessageConnector:InstanceIds:1"] = DriverMain.SecondaryBinding,
                ["MessageConnector:ReconnectDelayMilliseconds"] = "100",
                ["HostPipeline:EnableHostPlatformSend"] = "true",
                ["HostPipeline:WakePrefixes:0"] = "/",
                ["HostPipeline:EnableRateLimit"] = "false",
                ["HostPipeline:EnableWhitelist"] = "false",
                ["HostPipeline:EnableSmartFilter"] = "false",
                ["HostRuntimeOptions:EnableMessageMerge"] = "false",
                ["HostRuntimeOptions:MergeWindowSeconds"] = "0",
                ["HostRuntimeOptions:WorkerTimeoutMs"] = "5000",
                ["HostRuntimeOptions:SendTimeoutMs"] = "5000",
                ["Iris:Enabled"] = "false",
                ["KnowledgeBaseRuntime:Enabled"] = "false",
                ["HostOneBot:Enabled"] = "false",
                ["PythonWorker:EnableHealthProbe"] = "false",
                ["PythonCapabilities:Enabled"] = "false",
            });
        });
        builder.ConfigureServices(services =>
        {
            services.RemoveAll<IWorkerTransport>();
            services.AddSingleton<IWorkerTransport, Phase7DeterministicWorkerTransport>();
        });
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing && _host is not null)
        {
            using var shutdown = new CancellationTokenSource();
            shutdown.Cancel();
            try
            {
                _host.StopAsync(shutdown.Token).GetAwaiter().GetResult();
            }
            catch (OperationCanceledException)
            {
                // The connector subscription observes the already-cancelled
                // host token and exits without a reconnect delay.
            }
        }

        base.Dispose(disposing);
    }
}

internal sealed class Phase7DeterministicWorkerTransport(
    HostConversationPersistenceService persistence) : IWorkerTransport
{
    public async Task<ProcessingResult> DispatchAsync(
        EventEnvelope envelope,
        CancellationToken cancellationToken)
    {
        var bindingId = envelope.Session.ConnectorInstanceId;
        if (string.IsNullOrWhiteSpace(bindingId))
        {
            throw new InvalidOperationException("Phase7 worker received an unbound event.");
        }

        var reply = $"reply:{bindingId}";
        await persistence.PersistTurnAsync(envelope, reply, cancellationToken);
        return new ProcessingResult
        {
            TraceId = envelope.TraceId,
            Status = HostDispatchStatus.Succeeded,
            ResultKind = "message_chain",
            HasVisibleOutput = true,
            MessageChain =
            [
                new MessageComponentContract
                {
                    Type = "Plain",
                    Payload = new Dictionary<string, string?> { ["text"] = reply },
                },
            ],
        };
    }
}
