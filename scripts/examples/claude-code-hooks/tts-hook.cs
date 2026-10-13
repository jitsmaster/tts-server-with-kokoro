// tts-hook.exe -- one compiled replacement for the PowerShell voice hooks
// (detach-launcher.ps1 + speak.ps1 / stop-speak.ps1 / elicitation-speak.ps1 + skill-lookup.ps1
// + tts-client.ps1). Same behaviour, same state files and mutex, but:
//   * starts in ~tens of ms instead of ~1 s per powershell.exe (the old chain started two),
//   * finds the last-used skill by scanning the transcript tail as text (the PowerShell version
//     ConvertFrom-Json'ed up to 400 huge lines: 2-29 s on 1-14 MB transcripts),
//   * caches synthesized WAVs of short, repeated phrases (tts-cache\), so those play instantly,
//   * can pass a speech speed to the server (tts-config.json).
//
// Usage (hook command):  tts-hook.exe speak|stop|elicit      (hook JSON on stdin)
//   speak  = Notification / PermissionRequest   (male)
//   stop   = Stop                                (male, spoken summary of last message)
//   elicit = Elicitation / PreToolUse AskUserQuestion (female, high priority)
// Other:  tts-hook.exe --daemon   run the persistent player in the foreground (normally auto-started)
//         tts-hook.exe --quit     ask the running player to exit (do this before replacing the exe)
//         tts-hook.exe <mode> --dry < payload.json   print what would be spoken, no sound
//
// Architecture: the hook command is a tiny client. It sends the payload over the named pipe
// "tts-hook-daemon-v1" to ONE long-lived player process (this same exe with --daemon), waits for a
// one-byte ack and exits (~tens of ms). The player stays resident: its JIT, JSON and regex code is
// warm, it builds the text, synthesizes (or hits the WAV cache) and plays, one message at a time
// (requests stream from the server phrase by phrase and start playing after the first one; one playback
// thread plays them high-priority-first then FIFO, holding
// the same cross-process mutex and polling tts-stop.flag as before). If no player is running the
// client starts it, and if that fails it falls back to a detached one-shot worker.
// The player does NOT write its PID to audio.pids (Ctrl+Alt+M hard-kills that PID; it must not kill
// the player); playback stops via tts-stop.flag, polled every 50 ms.
//
// Build (C# 5 compiler that ships with .NET Framework):
//   C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe /target:winexe /optimize /out:tts-hook.exe ^
//       /reference:System.Web.Extensions.dll tts-hook.cs
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Net;
using System.Net.Sockets;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Media;
using System.Web.Script.Serialization;

static class TtsHook
{
    static readonly string Dir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".claude");
    static string P(string name) { return Path.Combine(Dir, name); }
    // Per-thread so the resident player can handle several messages at once, and lazy so the tiny
    // client path (Main) never pays for loading System.Web.Extensions.
    [ThreadStatic] static JavaScriptSerializer _json;
    static JavaScriptSerializer Json
    {
        get { if (_json == null) _json = new JavaScriptSerializer { MaxJsonLength = int.MaxValue, RecursionLimit = 200 }; return _json; }
    }
    [ThreadStatic] static Stopwatch _clock;
    [ThreadStatic] static StringBuilder _stages;
    static void StartTiming() { _clock = Stopwatch.StartNew(); _stages = new StringBuilder(); }
    static string Stages { get { return _stages == null ? "" : _stages.ToString(); } }
    static void Stage(string name)
    {
        if (_stages != null) _stages.Append(name).Append('=').Append(_clock.ElapsedMilliseconds).Append("ms ");
    }

    const string PipeName = "tts-hook-daemon-v1";

    // ---- entry -------------------------------------------------------------------------------
    static int Main(string[] args)
    {
        try
        {
            if (args.Length < 1) return 2;
            if (args[0] == "--daemon") return Daemon();
            if (args[0] == "--quit") return SendToDaemon("quit", "", 500) ? 0 : 1;

            string mode = args[0];
            if (mode != "speak" && mode != "stop" && mode != "elicit") return 2;
            if (File.Exists(P("tts-muted.flag"))) return 0;

            bool dry = Array.IndexOf(args, "--dry") >= 0;
            if (args.Length >= 3 && args[1] == "--worker")        // fallback one-shot worker
            {
                string raw = File.ReadAllText(args[2], new UTF8Encoding(false));
                try { File.Delete(args[2]); } catch { }
                Run(mode, raw, false, false);
                return 0;
            }

            string payload;
            using (Stream stdin = Console.OpenStandardInput())
            using (StreamReader sr = new StreamReader(stdin, new UTF8Encoding(false)))
                payload = sr.ReadToEnd();

            if (dry) { Run(mode, payload, true, false); return 0; }

            // 1. a player is already running: hand over and leave.
            if (SendToDaemon(mode, payload, 250)) return 0;

            // 2. start the player and retry while it comes up.
            string self = Process.GetCurrentProcess().MainModule.FileName;
            try
            {
                ProcessStartInfo dpsi = new ProcessStartInfo(self, "--daemon");
                dpsi.UseShellExecute = false; dpsi.CreateNoWindow = true;
                Process.Start(dpsi);
                for (int i = 0; i < 25; i++)
                {
                    if (SendToDaemon(mode, payload, 200)) return 0;
                    Thread.Sleep(100);
                }
            }
            catch (Exception ex) { Log("tts-hook-errors.log", "daemon start: " + ex.Message); }

            // 3. last resort: detached one-shot worker (the pre-daemon behaviour).
            string tmp = Path.GetTempFileName();
            File.WriteAllText(tmp, payload, new UTF8Encoding(false));
            ProcessStartInfo psi = new ProcessStartInfo(self, mode + " --worker \"" + tmp + "\"");
            psi.UseShellExecute = false; psi.CreateNoWindow = true;
            Process.Start(psi);
            Log("tts-hook-errors.log", "fell back to one-shot worker for " + mode);
            return 0;
        }
        catch (Exception ex)
        {
            Log("tts-hook-errors.log", ex.GetType().FullName + ": " + ex.Message);
            return 1;
        }
    }

    // ---- persistent player -------------------------------------------------------------------
    // Client side: frame = int32 length + UTF-8 "mode\npayload"; the player answers one byte 'K' once it
    // has the whole message queued.
    static bool SendToDaemon(string mode, string payload, int connectMs)
    {
        try
        {
            using (NamedPipeClientStream c = new NamedPipeClientStream(".", PipeName, PipeDirection.InOut, PipeOptions.None))
            {
                c.Connect(connectMs);
                byte[] data = new UTF8Encoding(false).GetBytes(mode + "\n" + payload);
                byte[] len = BitConverter.GetBytes(data.Length);
                c.Write(len, 0, 4);
                c.Write(data, 0, data.Length);
                c.Flush();
                byte[] ack = new byte[1];
                System.Threading.Tasks.Task<int> t = c.ReadAsync(ack, 0, 1);
                return t.Wait(3000) && t.Result == 1 && ack[0] == (byte)'K';
            }
        }
        catch { return false; }
    }

    static int Daemon()
    {
        // The player outlives whichever project the first hook ran in; never keep that directory locked.
        try { Directory.SetCurrentDirectory(Dir); } catch { }
        bool created;
        Mutex single = new Mutex(true, "Global\\TtsHookDaemon", out created);
        if (!created) return 0;                                   // another player is already running

        Log("tts-hook.log", "player started pid=" + Process.GetCurrentProcess().Id);
        try { File.WriteAllText(P("audio.pids"), ""); } catch { }

        Thread playback = new Thread(PlayerLoop);
        playback.IsBackground = true; playback.Start();

        // Warm the JIT / serializer / regexes so the first real message is not slow.
        Thread warm = new Thread(delegate ()
        {
            try
            {
                StartTiming(); string g; bool h;
                BuildText("stop", "{\"last_assistant_message\":\"## Insights\\nwarm **up**\\n\\ntext\",\"transcript_path\":\"\"}", out g, out h);
                Json.Serialize(new Dictionary<string, object>());
            }
            catch { }
        });
        warm.IsBackground = true; warm.Start();

        PipeSecurity ps = new PipeSecurity();
        ps.AddAccessRule(new PipeAccessRule(WindowsIdentity.GetCurrent().User, PipeAccessRights.FullControl, AccessControlType.Allow));

        // Several listeners, so a pipe instance is always waiting: with one, there is a gap between a client
        // connecting and the next instance being created, and a client arriving in it gets ENOENT ("no player").
        for (int i = 1; i < 3; i++)
        {
            Thread lt = new Thread(delegate () { AcceptLoop(ps); });
            lt.IsBackground = true; lt.Start();
        }
        Thread tcp = new Thread(TcpAcceptLoop);
        tcp.IsBackground = true; tcp.Start();
        AcceptLoop(ps);
        return 0;
    }

    // tts-send.exe talks to the player over loopback TCP: opening a named pipe from a freshly started
    // process stalls for seconds at random on this machine, a loopback connect never does. Same framing.
    const int TcpPort = 8882;
    static void TcpAcceptLoop()
    {
        TcpListener l = null;
        try
        {
            l = new TcpListener(IPAddress.Loopback, TcpPort);
            l.Start(64);
        }
        catch (Exception ex)
        {
            Log("tts-hook-errors.log", "player tcp listen: " + ex.GetType().Name + ": " + ex.Message);
            return;
        }
        while (true)
        {
            try
            {
                TcpClient cl = l.AcceptTcpClient();
                cl.NoDelay = true;
                Thread t = new Thread(delegate () { HandleConnection(cl.GetStream()); });
                t.IsBackground = true; t.Start();
            }
            catch (Exception ex)
            {
                Log("tts-hook-errors.log", "player tcp accept: " + ex.GetType().Name + ": " + ex.Message);
                Thread.Sleep(200);
            }
        }
    }

    static void AcceptLoop(PipeSecurity ps)
    {
        while (true)
        {
            NamedPipeServerStream srv = null;
            try
            {
                srv = new NamedPipeServerStream(PipeName, PipeDirection.InOut, NamedPipeServerStream.MaxAllowedServerInstances,
                    PipeTransmissionMode.Byte, PipeOptions.None, 0, 0, ps);
                srv.WaitForConnection();
                NamedPipeServerStream conn = srv; srv = null;
                Thread t = new Thread(delegate () { HandleConnection(conn); });
                t.IsBackground = true; t.Start();                 // listener goes straight back to listening
            }
            catch (Exception ex)
            {
                Log("tts-hook-errors.log", "player accept: " + ex.GetType().Name + ": " + ex.Message);
                if (srv != null) { try { srv.Dispose(); } catch { } }
                Thread.Sleep(200);
            }
        }
    }

    static void HandleConnection(Stream conn)
    {
        string mode = null, raw = null;
        try
        {
            using (conn)
            {
                byte[] lenBuf = ReadExactly(conn, 4);
                int len = BitConverter.ToInt32(lenBuf, 0);
                if (len <= 0 || len > 64 * 1024 * 1024) return;
                byte[] data = ReadExactly(conn, len);
                conn.WriteByte((byte)'K'); conn.Flush();
                string s = new UTF8Encoding(false).GetString(data);
                int nl = s.IndexOf('\n');
                if (nl < 0) return;
                mode = s.Substring(0, nl); raw = s.Substring(nl + 1);
            }
            if (mode == "quit") { Log("tts-hook.log", "player quit"); Environment.Exit(0); }
            if (mode != "speak" && mode != "stop" && mode != "elicit") return;
            if (File.Exists(P("tts-muted.flag"))) return;
            Run(mode, raw, false, true);
        }
        catch (Exception ex)
        {
            Log("tts-hook-errors.log", "player request: " + ex.GetType().Name + ": " + ex.Message);
        }
    }

    static byte[] ReadExactly(Stream s, int n)
    {
        byte[] buf = new byte[n]; int got = 0;
        while (got < n) { int r = s.Read(buf, got, n - got); if (r <= 0) throw new EndOfStreamException(); got += r; }
        return buf;
    }

    // ---- per-mode text building --------------------------------------------------------------
    static string BuildText(string mode, string raw, out string gender, out bool high)
    {
        Dictionary<string, object> data = null;
        try { data = Json.DeserializeObject(raw) as Dictionary<string, object>; } catch { }

        string transcript = Str(Get(data, "transcript_path"));
        string msg; high = false;

        if (mode == "speak")
        {
            gender = "male";
            string m = Str(Get(data, "message"));
            msg = !string.IsNullOrEmpty(m) ? m.Trim() : "Claude needs your attention.";
            if (Regex.IsMatch(msg, "waiting for your input|needs your attention", RegexOptions.IgnoreCase)
                && RecentCron(transcript))
            {
                string sk = LastSkill(transcript);
                msg = sk != null ? "A scheduled job just ran : " + Regex.Replace(sk, "[-:_]", " ") : "A scheduled job just ran";
            }
            string name = SessionName(transcript, true);
            if (name != null) msg = msg + " Session : " + name;
        }
        else if (mode == "stop")
        {
            gender = "male";
            string m = Str(Get(data, "last_assistant_message"));
            msg = !string.IsNullOrEmpty(m) ? m.Trim() : "Claude is done.";

            string insights = null;
            Match im = Regex.Match(msg, @"^#{1,6}\s*Insights\s*\r?\n(.*?)(?=^#{1,6}\s|\z)",
                RegexOptions.Multiline | RegexOptions.Singleline | RegexOptions.IgnoreCase);
            if (im.Success)
            {
                insights = Regex.Replace(Clean(im.Groups[1].Value.Trim()), @"\s+", " ").Trim();
                msg = msg.Remove(im.Index, im.Length).Trim();
            }
            msg = Clean(msg);
            Stage("parse");
            string sk = LastSkill(transcript);
            if (sk != null) msg = msg + " ... Done by skill : " + Regex.Replace(sk, "[-:_]", " ") + ";";
            string name = SessionName(transcript, false);
            if (name != null) msg = msg + " ... Session : " + name + ";";
            if (!string.IsNullOrEmpty(insights)) msg = msg + " ... Insights : " + insights + ".";
        }
        else
        {
            gender = "female"; high = true;
            string m = null;
            object qs = Get(Get(data, "tool_input"), "questions");
            object[] qa = qs as object[];
            if (qa != null && qa.Length > 0)
            {
                List<string> parts = new List<string>();
                foreach (object q in qa) parts.Add(Str(Get(q, "question")) ?? "");
                m = string.Join(". ", parts.ToArray());
                if (string.IsNullOrWhiteSpace(m)) m = "We have a question for you";
            }
            else if (!string.IsNullOrEmpty(Str(Get(data, "message")))) m = Str(Get(data, "message")).Trim();
            else if (!string.IsNullOrEmpty(Str(Get(data, "last_assistant_message")))) m = Str(Get(data, "last_assistant_message")).Trim();
            else m = "Claude has a question for you.";
            msg = Clean(m);
            string sk = LastSkill(transcript);
            if (sk != null) msg = msg + " Question by skill : " + Regex.Replace(sk, "[-:_]", " ");
            string name = SessionName(transcript, false);
            if (name != null) msg = msg + " Session : " + name;
        }
        Stage("text");
        return msg;
    }

    static void Run(string mode, string raw, bool dry, bool inPlayer)
    {
        StartTiming();
        string gender; bool high;
        string msg = BuildText(mode, raw, out gender, out high);

        if (dry)
        {
            Console.Error.WriteLine("mode=" + mode + " gender=" + gender + " high=" + high + " " + Stages);
            Console.Out.WriteLine(msg);
            return;
        }

        if (inPlayer)
        {
            try { StreamIn(msg, gender, mode, high); }
            catch (Exception ex)
            {
                Log("tts-client-errors.log", ex.GetType().FullName + ": " + ex.Message);
                Log("tts-hook.log", mode + "[player] FAILED chars=" + msg.Length + " " + Stages);
                Log("tts-failures.log", mode + ": " + msg);
            }
            return;
        }

        bool spoken = Speak(msg, gender, high, true);
        Log("tts-hook.log", mode + "[worker] " + (spoken ? "ok" : "FAILED") + " chars=" + msg.Length + " " + Stages);
        if (!spoken) Log("tts-failures.log", mode + ": " + msg);
    }

    // Same cleanup the PowerShell scripts applied (Clean-ForSpeech / inline equivalents).
    static string Clean(string t)
    {
        t = Regex.Replace(t, @"\*\*([^*]+)\*\*", "$1");
        t = Regex.Replace(t, @"\*([^*]+)\*", "$1");
        t = Regex.Replace(t, @"`([^`]+)`", "$1");
        t = Regex.Replace(t, @"#{1,6}\s*", "");
        t = Regex.Replace(t, @"\[([^\]]+)\]\([^)]+\)", "$1");
        t = Regex.Replace(t, "[\u2014\u2013]", " ");
        t = Regex.Replace(t, @"\s+-\s+", " ");
        t = Regex.Replace(t, @"[|~^_]", " ");
        t = Regex.Replace(t, "[^\\x20-\\x7E\\r\\n\u4E00-\u9FFF\u3400-\u4DBF\uF900-\uFAFF]", "  ");
        return t;
    }

    // ---- JSON helpers ------------------------------------------------------------------------
    static object Get(object o, string key)
    {
        Dictionary<string, object> d = o as Dictionary<string, object>;
        object v;
        if (d != null && d.TryGetValue(key, out v)) return v;
        return null;
    }
    static string Str(object o) { return o as string; }

    // ---- transcript / session lookups --------------------------------------------------------
    // Last n lines of a file, read from the end (a 14 MB transcript costs a few ms, not seconds).
    static List<string> TailLines(string path, int n)
    {
        List<string> result = new List<string>();
        if (string.IsNullOrEmpty(path) || !File.Exists(path)) return result;
        const long cap = 64L * 1024 * 1024;
        const int block = 1 << 20;
        using (FileStream fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
        {
            long pos = fs.Length, total = 0; int newlines = 0;
            List<byte[]> blocks = new List<byte[]>();
            while (pos > 0 && newlines <= n && total < cap)
            {
                int take = (int)Math.Min(block, pos);
                pos -= take;
                byte[] buf = new byte[take];
                fs.Seek(pos, SeekOrigin.Begin);
                int read = 0;
                while (read < take) { int r = fs.Read(buf, read, take - read); if (r <= 0) break; read += r; }
                blocks.Insert(0, buf);
                total += take;
                for (int i = 0; i < take; i++) if (buf[i] == (byte)'\n') newlines++;
            }
            byte[] all = new byte[total];
            int off = 0;
            foreach (byte[] b in blocks) { Buffer.BlockCopy(b, 0, all, off, b.Length); off += b.Length; }
            string[] lines = Encoding.UTF8.GetString(all).Split('\n');
            int start = pos > 0 ? 1 : 0;                      // first line is partial unless we reached the file start
            for (int i = start; i < lines.Length; i++)
            {
                string l = lines[i].TrimEnd('\r');
                if (l.Length > 0) result.Add(l);
            }
        }
        if (result.Count > n) result.RemoveRange(0, result.Count - n);
        return result;
    }

    static Regex _skillTool;
    static Regex SkillTool { get { if (_skillTool == null) _skillTool = new Regex("\"name\"\\s*:\\s*\"Skill\""); return _skillTool; } }

    // Newest Skill tool_use in the last 400 transcript lines (same window as the PowerShell version).
    static string LastSkill(string transcript)
    {
        try
        {
            List<string> lines = TailLines(transcript, 400);
            for (int i = lines.Count - 1; i >= 0; i--)
            {
                if (!SkillTool.IsMatch(lines[i])) continue;       // cheap text filter before any JSON parse
                object entry; try { entry = Json.DeserializeObject(lines[i]); } catch { continue; }
                object content = Get(Get(entry, "message"), "content");
                object[] items = content as object[];
                if (items == null) continue;
                foreach (object item in items)
                    if (Str(Get(item, "type")) == "tool_use" && Str(Get(item, "name")) == "Skill")
                    {
                        string s = Str(Get(Get(item, "input"), "skill"));
                        if (!string.IsNullOrEmpty(s)) return s;
                    }
            }
        }
        catch { }
        Stage("skill");
        return null;
    }

    static bool RecentCron(string transcript)
    {
        try
        {
            foreach (string line in TailLines(transcript, 20))
            {
                if (line.IndexOf("<<autonomous-loop", StringComparison.Ordinal) < 0) continue;
                object entry; try { entry = Json.DeserializeObject(line); } catch { continue; }
                if (Str(Get(entry, "type")) != "user") continue;
                object content = Get(Get(entry, "message"), "content");
                string text = content as string;
                if (text == null && content is object[])
                {
                    List<string> parts = new List<string>();
                    foreach (object item in (object[])content)
                        if (Str(Get(item, "type")) == "text") parts.Add(Str(Get(item, "text")) ?? "");
                    text = string.Join(" ", parts.ToArray());
                }
                if (text != null && Regex.IsMatch(text, "<<autonomous-loop(-dynamic)?>>")) return true;
            }
        }
        catch { }
        return false;
    }

    // Session's friendly name. always=false: only a deliberate user rename (nameSource == "user").
    static string SessionName(string transcript, bool always)
    {
        try
        {
            if (string.IsNullOrEmpty(transcript)) return null;
            string id = Path.GetFileNameWithoutExtension(transcript);
            string sdir = P("sessions");
            if (!Directory.Exists(sdir)) return null;
            foreach (string f in Directory.GetFiles(sdir, "*.json"))
            {
                object info; try { info = Json.DeserializeObject(File.ReadAllText(f)); } catch { continue; }
                if (Str(Get(info, "sessionId")) != id) continue;
                string name = Str(Get(info, "name"));
                if (string.IsNullOrEmpty(name)) return null;
                if (always || Str(Get(info, "nameSource")) == "user") return name;
                return null;
            }
        }
        catch { }
        return null;
    }

    // ---- config + cache ----------------------------------------------------------------------
    static double? ConfigSpeed()
    {
        try
        {
            object cfg = Json.DeserializeObject(File.ReadAllText(P("tts-config.json")));
            object s = Get(cfg, "speed");
            if (s != null) { double v = Convert.ToDouble(s); if (v >= 0.5 && v <= 1.5 && Math.Abs(v - 1.0) > 1e-9) return v; }
        }
        catch { }
        return null;
    }
    static int ConfigInt(string key, int dflt)
    {
        try { object v = Get(Json.DeserializeObject(File.ReadAllText(P("tts-config.json"))), key); if (v != null) return Convert.ToInt32(v); }
        catch { }
        return dflt;
    }

    static string CachePath(string text, string gender, double? speed)
    {
        string key = gender + "|" + (speed.HasValue ? speed.Value.ToString("0.###", System.Globalization.CultureInfo.InvariantCulture) : "1") + "|" + text;
        using (SHA1 sha = SHA1.Create())
        {
            byte[] h = sha.ComputeHash(Encoding.UTF8.GetBytes(key));
            return Path.Combine(P("tts-cache"), BitConverter.ToString(h).Replace("-", "").ToLowerInvariant() + ".wav");
        }
    }

    static byte[] Synthesize(string text, string gender)
    {
        double? speed = ConfigSpeed();
        bool cacheable = text.Length <= ConfigInt("cacheMaxChars", 200);
        string cache = cacheable ? CachePath(text, gender, speed) : null;
        if (cache != null && File.Exists(cache))
        {
            try
            {
                byte[] hit = File.ReadAllBytes(cache);
                if (hit.Length > 44) { try { File.SetLastWriteTime(cache, DateTime.Now); } catch { } Stage("cachehit"); return hit; }
            }
            catch { }
        }

        Dictionary<string, object> body = new Dictionary<string, object>();
        body["text"] = text; body["voice_gender"] = gender;
        if (speed.HasValue) body["speed"] = speed.Value;
        byte[] payload = new UTF8Encoding(false).GetBytes(Json.Serialize(body));

        ServicePointManager.Expect100Continue = false;
        HttpWebRequest req = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:8880/tts");
        req.Method = "POST"; req.ContentType = "application/json"; req.Proxy = null; req.Timeout = 30000; req.ReadWriteTimeout = 30000;
        req.ContentLength = payload.Length;
        using (Stream rs = req.GetRequestStream()) rs.Write(payload, 0, payload.Length);
        byte[] wav;
        using (HttpWebResponse resp = (HttpWebResponse)req.GetResponse())
        using (Stream s = resp.GetResponseStream())
        using (MemoryStream ms = new MemoryStream())
        {
            s.CopyTo(ms); wav = ms.ToArray();
        }
        Stage("synth");

        if (cache != null) SaveCache(cache, wav);
        return wav;
    }

    static void SaveCache(string cache, byte[] wav)
    {
        if (wav.Length <= 44) return;
        try
        {
            Directory.CreateDirectory(P("tts-cache"));
            string tmp = cache + "." + Process.GetCurrentProcess().Id + "." + Thread.CurrentThread.ManagedThreadId + ".tmp";
            File.WriteAllBytes(tmp, wav);
            if (File.Exists(cache)) File.Delete(cache);
            File.Move(tmp, cache);
            Prune(ConfigInt("cacheMaxFiles", 300));
        }
        catch { }
    }

    static bool ConfigBool(string key, bool dflt)
    {
        try { object v = Get(Json.DeserializeObject(File.ReadAllText(P("tts-config.json"))), key); if (v != null) return Convert.ToBoolean(v); }
        catch { }
        return dflt;
    }

    // ---- streaming input (player) ------------------------------------------------------------
    // Cache hit -> one chunk. Otherwise POST /tts/stream and queue the message as soon as the first phrase
    // arrives, so playback starts after one phrase instead of after the whole message. Falls back to the
    // whole-message /tts if the stream cannot even start (old server, config "streaming": false).
    static void StreamIn(string msg, string gender, string mode, bool high)
    {
        double? speed = ConfigSpeed();
        bool cacheable = msg.Length <= ConfigInt("cacheMaxChars", 200);
        string cache = cacheable ? CachePath(msg, gender, speed) : null;
        PlayItem it = new PlayItem(); it.High = high; it.Mode = mode; it.Chars = msg.Length;

        if (cache != null && File.Exists(cache))
        {
            try
            {
                byte[] hit = File.ReadAllBytes(cache);
                if (hit.Length > 44)
                {
                    try { File.SetLastWriteTime(cache, DateTime.Now); } catch { }
                    Stage("cachehit");
                    it.Add(hit); it.Finish(); it.Stages = Stages; it.Enqueued = DateTime.UtcNow; EnqueuePlay(it);
                    return;
                }
            }
            catch { }
        }

        if (ConfigBool("streaming", true) && TryStream(msg, gender, speed, it, cache)) return;

        byte[] wav = Synthesize(msg, gender);                  // whole message, as before
        it.Add(wav); it.Finish(); it.Stages = Stages; it.Enqueued = DateTime.UtcNow; EnqueuePlay(it);
    }

    // True once the message has been queued (even if the stream later broke); false = nothing queued, use fallback.
    static bool TryStream(string msg, string gender, double? speed, PlayItem it, string cache)
    {
        Dictionary<string, object> body = new Dictionary<string, object>();
        body["text"] = msg; body["voice_gender"] = gender;
        if (speed.HasValue) body["speed"] = speed.Value;
        byte[] payload = new UTF8Encoding(false).GetBytes(Json.Serialize(body));

        ServicePointManager.Expect100Continue = false;
        HttpWebRequest req = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:8880/tts/stream");
        req.Method = "POST"; req.ContentType = "application/json"; req.Proxy = null; req.Timeout = 30000; req.ReadWriteTimeout = 30000;
        req.ContentLength = payload.Length;
        HttpWebResponse resp;
        try
        {
            using (Stream rs = req.GetRequestStream()) rs.Write(payload, 0, payload.Length);
            resp = (HttpWebResponse)req.GetResponse();
        }
        catch (Exception ex) { Log("tts-client-errors.log", "stream not started (" + ex.Message + "), using whole-message synthesis"); return false; }

        bool queued = false, clean = false; int n = 0;
        List<byte[]> all = cache != null ? new List<byte[]>() : null;
        using (resp)
        using (Stream s = resp.GetResponseStream())
        {
            try
            {
                while (true)
                {
                    int len = BitConverter.ToInt32(ReadExactly(s, 4), 0);
                    if (len == 0) { clean = true; break; }
                    byte[] w = ReadExactly(s, len);
                    n++;
                    if (all != null) all.Add(w);
                    it.Add(w);
                    if (!queued) { Stage("first"); it.Stages = Stages; it.Enqueued = DateTime.UtcNow; EnqueuePlay(it); queued = true; }
                    if (it.Cancelled) { req.Abort(); break; }       // stopped by the hotkey: stop synthesizing the rest
                }
            }
            catch (Exception ex)
            {
                Log("tts-client-errors.log", "stream cut after " + n + " chunks: " + ex.Message);
                if (!queued) return false;
                it.Note = "stream-cut";
            }
        }
        Stage("stream");
        it.Stages = Stages;
        it.Finish();
        if (clean && !it.Cancelled && all != null && all.Count > 0) SaveCache(cache, AssembleWav(all));
        return queued;
    }

    // One WAV from the 16-bit mono 24 kHz chunks the server sends (so a streamed short phrase can be cached).
    static byte[] AssembleWav(List<byte[]> chunks)
    {
        MemoryStream pcm = new MemoryStream();
        foreach (byte[] w in chunks)
        {
            long pos = 12; int off = -1, size = 0; bool format = false;
            while (pos + 8 <= w.Length)
            {
                string id = Encoding.ASCII.GetString(w, (int)pos, 4);
                int csz = BitConverter.ToInt32(w, (int)pos + 4);
                if (id == "fmt ")
                    format = BitConverter.ToInt16(w, (int)pos + 8) == 1 && BitConverter.ToInt16(w, (int)pos + 10) == 1
                        && BitConverter.ToInt32(w, (int)pos + 12) == 24000 && BitConverter.ToInt16(w, (int)pos + 22) == 16;
                else if (id == "data") { off = (int)pos + 8; size = Math.Min(csz, w.Length - off); break; }
                pos += 8 + csz + (csz % 2);
            }
            if (!format || off < 0) return new byte[0];            // unexpected format: just do not cache
            pcm.Write(w, off, size);
        }
        byte[] data = pcm.ToArray();
        MemoryStream o = new MemoryStream();
        BinaryWriter bw = new BinaryWriter(o);
        bw.Write(Encoding.ASCII.GetBytes("RIFF")); bw.Write(36 + data.Length); bw.Write(Encoding.ASCII.GetBytes("WAVEfmt "));
        bw.Write(16); bw.Write((short)1); bw.Write((short)1); bw.Write(24000); bw.Write(48000); bw.Write((short)2); bw.Write((short)16);
        bw.Write(Encoding.ASCII.GetBytes("data")); bw.Write(data.Length); bw.Write(data);
        bw.Flush();
        return o.ToArray();
    }

    static void Prune(int max)
    {
        FileInfo[] files = new DirectoryInfo(P("tts-cache")).GetFiles("*.wav");
        if (files.Length <= max) return;
        Array.Sort(files, delegate (FileInfo a, FileInfo b) { return a.LastWriteTimeUtc.CompareTo(b.LastWriteTimeUtc); });
        for (int i = 0; i < files.Length - max; i++) try { files[i].Delete(); } catch { }
    }

    // ---- playback (same sequencing as tts-client.ps1) ----------------------------------------
    static double? WavSeconds(byte[] b)
    {
        uint byteRate = 0, dataSize = 0; long pos = 12;
        while (pos + 8 <= b.Length)
        {
            string id = Encoding.ASCII.GetString(b, (int)pos, 4);
            uint size = BitConverter.ToUInt32(b, (int)pos + 4);
            if (id == "fmt ") byteRate = BitConverter.ToUInt32(b, (int)pos + 16);
            else if (id == "data") dataSize = size;
            pos += 8 + size + (size % 2);
        }
        if (byteRate > 0 && dataSize > 0) return (double)dataSize / byteRate;
        return null;
    }

    // Plays a (possibly still arriving) message while holding the machine-wide playback mutex shared with
    // one-shot workers. Chunks are complete WAVs; each is handed over to the next ~0.15 s before it ends (the
    // server pads every chunk with 0.25 s of silence, so the overlap eats silence only). Must run on one thread
    // from acquire to release (Mutex is thread-affine). timeline = chunk count and the gaps between chunks.
    static bool PlayItemLocked(PlayItem it, bool recordPid, int mutexWaitSec, out string timeline)
    {
        timeline = "";
        string stopFlag = P("tts-stop.flag");
        Mutex mutex = new Mutex(false, "Global\\KokoroTtsPlayback");
        bool acquired = false;
        SoundPlayer cur = null; MemoryStream curStream = null;
        try
        {
            try { acquired = mutex.WaitOne(TimeSpan.FromSeconds(mutexWaitSec)); }
            catch (AbandonedMutexException) { acquired = true; }
            if (!acquired) { Log("tts-client-errors.log", "mutex timeout after " + mutexWaitSec + "s (another speak in progress)"); return false; }

            if (recordPid) File.WriteAllText(P("audio.pids"), Process.GetCurrentProcess().Id.ToString());
            try { File.Delete(stopFlag); } catch { }

            Stopwatch clock = null;
            double prevStart = 0, prevDur = 0;
            List<string> gaps = new List<string>();
            int i = 0;
            byte[] wav = it.Wait(0, 5000);
            while (wav != null)
            {
                double? durN = WavSeconds(wav);
                SoundPlayer p = new SoundPlayer();
                MemoryStream ms = new MemoryStream(wav);
                p.Stream = ms; p.Load();
                if (!durN.HasValue)                                // unknown length: cannot be stopped mid-way, rare
                {
                    p.PlaySync(); p.Dispose(); ms.Dispose();
                    wav = it.Wait(++i, 30000);
                    continue;
                }
                double dur = durN.Value;
                if (clock == null) { Stage("play"); clock = Stopwatch.StartNew(); }
                p.Play();
                double start = clock.Elapsed.TotalSeconds;
                if (i > 0) gaps.Add(((int)((start - (prevStart + prevDur)) * 1000)).ToString());
                if (cur != null) { cur.Dispose(); curStream.Dispose(); }
                cur = p; curStream = ms; prevStart = start; prevDur = dur;
                i++;

                if (WaitUntil(clock, start + Math.Max(dur - 0.15, dur * 0.5), stopFlag, p)) { it.Cancelled = true; return true; }
                byte[] next = null; bool stopped = false;
                for (int waitedMs = 0; waitedMs < 30000 && !stopped; waitedMs += 50)
                {
                    next = it.Wait(i, 50);
                    if (next != null || it.Done || it.Cancelled) break;
                    if (File.Exists(stopFlag)) { p.Stop(); try { File.Delete(stopFlag); } catch { } stopped = true; }
                }
                if (stopped) { it.Cancelled = true; return true; }
                if (next == null)                                  // no more chunks: let the last one play out
                {
                    if (!it.Done && !it.Cancelled) it.Note = "stalled";
                    if (WaitUntil(clock, start + dur + 0.1, stopFlag, p)) it.Cancelled = true;
                    break;
                }
                wav = next;
            }
            timeline = "chunks=" + i + (gaps.Count > 0 ? " gaps=[" + string.Join(",", gaps.ToArray()) + "]ms" : "")
                + (it.Note != null ? " note=" + it.Note : "");
            return i > 0;
        }
        catch (Exception ex)
        {
            Log("tts-client-errors.log", ex.GetType().FullName + ": " + ex.Message);
            return false;
        }
        finally
        {
            if (cur != null) { try { cur.Dispose(); } catch { } }
            if (curStream != null) { try { curStream.Dispose(); } catch { } }
            if (acquired) { try { mutex.ReleaseMutex(); } catch { } }
            mutex.Dispose();
        }
    }

    // Polls the stop flag while waiting for t seconds on the clock. True = stop requested (and honoured).
    static bool WaitUntil(Stopwatch clock, double t, string stopFlag, SoundPlayer p)
    {
        while (clock.Elapsed.TotalSeconds < t)
        {
            if (File.Exists(stopFlag)) { p.Stop(); try { File.Delete(stopFlag); } catch { } return true; }
            Thread.Sleep(15);
        }
        return false;
    }

    static bool PlayLocked(byte[] wav, bool recordPid, int mutexWaitSec)
    {
        PlayItem it = new PlayItem();
        it.Add(wav); it.Finish();
        string timeline;
        return PlayItemLocked(it, recordPid, mutexWaitSec, out timeline);
    }

    // One-shot worker path (fallback): synthesize, yield to a pending high-priority speaker, play.
    static bool Speak(string text, string gender, bool high, bool recordPid)
    {
        string flag = P("tts-priority-pending.flag");
        bool setFlag = false;
        try
        {
            byte[] wav = Synthesize(text, gender);
            if (high) { File.WriteAllText(flag, DateTime.Now.ToString("o")); setFlag = true; }
            else
            {
                int waited = 0;
                while (File.Exists(flag) && waited < 20000)
                {
                    if ((DateTime.Now - File.GetLastWriteTime(flag)).TotalSeconds > 20) break;
                    Thread.Sleep(250); waited += 250;
                }
            }
            return PlayLocked(wav, recordPid, 30);
        }
        catch (Exception ex)
        {
            Log("tts-client-errors.log", ex.GetType().FullName + ": " + ex.Message);
            return false;
        }
        finally
        {
            if (setFlag) { try { File.Delete(flag); } catch { } }
        }
    }

    // ---- player queue ------------------------------------------------------------------------
    // Requests synthesize in parallel (one thread each); ONE playback thread plays them in order,
    // high-priority (questions) first, so nothing talks over anything and nothing is dropped just
    // because an earlier long message is still playing (the old 30 s mutex wait did that).
    // An item is queued as soon as its FIRST chunk exists; later chunks keep arriving while it plays.
    class PlayItem
    {
        public bool High; public DateTime Enqueued; public string Mode; public int Chars; public string Stages;
        public readonly List<byte[]> Chunks = new List<byte[]>();
        public volatile bool Done, Cancelled; public string Note;

        public void Add(byte[] wav) { lock (Chunks) { Chunks.Add(wav); Monitor.PulseAll(Chunks); } }
        public void Finish() { lock (Chunks) { Done = true; Monitor.PulseAll(Chunks); } }
        // Chunk i, waiting up to timeoutMs for it; null = it does not exist (yet): finished, cancelled or timed out.
        public byte[] Wait(int i, int timeoutMs)
        {
            lock (Chunks)
            {
                DateTime end = DateTime.UtcNow.AddMilliseconds(timeoutMs);
                while (i >= Chunks.Count)
                {
                    if (Done || Cancelled) return null;
                    int left = (int)(end - DateTime.UtcNow).TotalMilliseconds;
                    if (left <= 0) return null;
                    Monitor.Wait(Chunks, left);
                }
                return Chunks[i];
            }
        }
    }
    static readonly List<PlayItem> PlayQueue = new List<PlayItem>();
    static readonly object QueueLock = new object();
    const int StaleAfterSec = 180;                                // an announcement this old is no longer useful

    static void EnqueuePlay(PlayItem it)
    {
        lock (QueueLock) { PlayQueue.Add(it); Monitor.Pulse(QueueLock); }
    }

    static void PlayerLoop()
    {
        while (true)
        {
            PlayItem it;
            lock (QueueLock)
            {
                while (PlayQueue.Count == 0) Monitor.Wait(QueueLock);
                int idx = PlayQueue.FindIndex(delegate (PlayItem x) { return x.High; });
                if (idx < 0) idx = 0;
                it = PlayQueue[idx]; PlayQueue.RemoveAt(idx);
            }
            try
            {
                double waited = (DateTime.UtcNow - it.Enqueued).TotalMilliseconds;
                if (waited > StaleAfterSec * 1000.0)
                {
                    it.Cancelled = true;
                    Log("tts-hook.log", it.Mode + "[player] DROPPED stale (" + (int)(waited / 1000) + "s in queue) chars=" + it.Chars);
                    continue;
                }
                StartTiming();
                string timeline;
                bool ok = PlayItemLocked(it, false, 30, out timeline);
                if (!ok && !it.Cancelled) ok = PlayItemLocked(it, false, 60, out timeline);       // a fallback worker held the mutex
                Log("tts-hook.log", it.Mode + "[player] " + (ok ? "ok" : "FAILED") + " chars=" + it.Chars + " " + it.Stages + "qwait=" + (int)waited + "ms " + timeline);
                if (!ok) Log("tts-failures.log", it.Mode + ": playback failed (" + it.Chars + " chars)");
            }
            catch (Exception ex) { Log("tts-hook-errors.log", "player loop: " + ex.GetType().Name + ": " + ex.Message); }
        }
    }

    // ---- logging -----------------------------------------------------------------------------
    static void Log(string file, string line)
    {
        try
        {
            string path = P(file);
            if (file == "tts-hook.log" && File.Exists(path) && new FileInfo(path).Length > 200 * 1024) File.Delete(path);
            File.AppendAllText(path, DateTime.Now.ToString("o") + " " + line + Environment.NewLine);
        }
        catch { }
    }
}
