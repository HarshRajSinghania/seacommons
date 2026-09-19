using System.Collections.Concurrent;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using TAOSW.DSC_Decoder.Core;
using TAOSW.DSC_Decoder.Core.TAOSW.DSC_Decoder.Core;

var protocol = new StreamWriter(Console.OpenStandardOutput()) { AutoFlush = true };
Console.SetOut(TextWriter.Null);
var states = new Dictionary<string, DecoderState>();

string? line;
while ((line = Console.In.ReadLine()) is not null)
{
    var messages = new List<object>();
    try
    {
        using var doc = JsonDocument.Parse(line);
        var root = doc.RootElement;
        var encoding = root.GetProperty("encoding").GetString() ?? "";
        var frequency = root.GetProperty("frequency_hz").GetInt32();
        if (encoding == "pcm_s16le" && IsDscFrequency(frequency))
        {
            var rate = root.GetProperty("sample_rate_hz").GetInt32();
            var lineage = root.TryGetProperty("physical_lineage", out var lineageElement)
                ? lineageElement.GetString()
                : null;
            var receiver = root.TryGetProperty("receiver_id", out var receiverElement)
                ? receiverElement.GetString()
                : null;
            var streamKey = $"{lineage ?? receiver ?? "unknown"}|{frequency}|{rate}";
            var pcm = Convert.FromBase64String(root.GetProperty("payload_b64").GetString() ?? "");
            if (pcm.Length > 0 && pcm.Length % 2 == 0)
            {
                if (!states.TryGetValue(streamKey, out var state))
                    states[streamKey] = state = new DecoderState(rate);
                state.Process(pcm);
                foreach (var msg in state.Drain()) if (msg.Status == "OK") messages.Add(ToWire(msg));
            }
        }
    }
    catch { }
    protocol.WriteLine(JsonSerializer.Serialize(new { messages }));
}

static bool IsDscFrequency(int hz)
{
    int[] allowed = { 2_187_500, 4_207_500, 6_312_000, 8_414_500, 12_577_000, 16_804_500, 156_525_000 };
    return allowed.Any(value => Math.Abs(value - hz) <= 100);
}

static object ToWire(DSCMessage msg)
{
    var payload = new Dictionary<string, object?>
    {
        ["category"] = msg.Category.ToString().ToLowerInvariant(),
        ["mmsi"] = Regex.Match(msg.From ?? "", @"\d{6,9}").Value is var m && m.Length > 0 ? m : null,
        ["nature_code"] = msg.Nature?.ToString().ToLowerInvariant(),
        ["format"] = msg.Format.ToString(),
        ["status"] = msg.Status,
    };
    var pos = ParsePosition(msg.Position);
    if (pos is not null)
    {
        payload["latitude"] = pos.Value.lat;
        payload["longitude"] = pos.Value.lon;
    }
    var material = string.Join(",", msg.Symbols ?? new List<int>());
    var digest = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(material))).Substring(0, 24).ToLowerInvariant();
    return new { kind = "dsc", payload, message_id = $"taosw_{digest}" };
}

static (double lat, double lon)? ParsePosition(string? value)
{
    if (string.IsNullOrWhiteSpace(value)) return null;
    var match = Regex.Match(value, @"^(\d{2}) (\d{2})([NS]) (\d{3}) (\d{2})([EW])$");
    if (!match.Success) return null;
    double lat = int.Parse(match.Groups[1].Value) + int.Parse(match.Groups[2].Value) / 60.0;
    double lon = int.Parse(match.Groups[4].Value) + int.Parse(match.Groups[5].Value) / 60.0;
    if (match.Groups[3].Value == "S") lat = -lat;
    if (match.Groups[6].Value == "W") lon = -lon;
    return (lat, lon);
}

sealed class DecoderState
{
    readonly FskAutoTuner tuner;
    readonly DSCDecoder decoder;
    readonly GMDSSDecoder[] gmdss;
    readonly ConcurrentQueue<DSCMessage> pending = new();

    public DecoderState(int sampleRate)
    {
        tuner = new FskAutoTuner(1200, 100, sampleRate, 170);
        tuner.SetManualLeftFreq(300);
        decoder = new DSCDecoder(100, sampleRate);
        gmdss = Enumerable.Range(0, DSCDecoder.SlideWindowsNumber).Select(_ => new GMDSSDecoder()).ToArray();
        foreach (var item in gmdss) item.OnMessageDecoded += msg => pending.Enqueue(msg);
    }

    public void Process(byte[] pcm)
    {
        var signal = new float[pcm.Length / 2];
        for (var i = 0; i < signal.Length; i++)
        {
            short sample = (short)(pcm[i * 2] | (pcm[i * 2 + 1] << 8));
            signal[i] = sample / 32768f;
        }
        var processed = tuner.ProcessSignal(signal);
        if (processed.Length == 0) return;
        var bits = decoder.DecodeFSK(processed, tuner.LeftFreq, tuner.RightFreq);
        for (var i = 0; i < Math.Min(bits.Length, gmdss.Length); i++) gmdss[i].AddBits(bits[i]);
    }

    public IEnumerable<DSCMessage> Drain()
    {
        while (pending.TryDequeue(out var msg)) yield return msg;
    }
}
