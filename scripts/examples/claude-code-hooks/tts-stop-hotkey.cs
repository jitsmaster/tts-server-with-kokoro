using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Windows.Forms;

// Persistent replacement for the old Desktop-shortcut "Shortcut key" hotkey.
// That mechanism is handled by Explorer, which measured 5+ seconds to actually
// launch the target process after the keypress -- Explorer has no dedicated
// low-latency hotkey dispatch, it just eventually gets around to it. A real
// process holding a message loop and calling RegisterHotKey directly reacts
// to WM_HOTKEY essentially instantly, with no process-spawn overhead at all.
class HotkeyForm : Form
{
    [DllImport("user32.dll")] static extern bool RegisterHotKey(IntPtr hWnd, int id, uint fsModifiers, uint vk);
    [DllImport("user32.dll")] static extern bool UnregisterHotKey(IntPtr hWnd, int id);

    const int HOTKEY_ID = 1;
    const uint MOD_ALT = 0x1;
    const uint MOD_CONTROL = 0x2;
    const uint VK_M = 0x4D;
    const int WM_HOTKEY = 0x0312;

    const string FlagPath = @"C:\Users\awang\.claude\tts-stop.flag";
    const string PidsPath = @"C:\Users\awang\.claude\audio.pids";

    public HotkeyForm()
    {
        // This form only exists to own a window handle for RegisterHotKey /
        // WM_HOTKEY -- keep it real (so Load/CreateHandle fire normally) but
        // make it visually a no-op: off-screen, zero size, no taskbar entry.
        // (An earlier version overrode SetVisibleCore(false) instead, which
        // silently prevented OnLoad -- and RegisterHotKey -- from ever running.)
        this.ShowInTaskbar = false;
        this.FormBorderStyle = FormBorderStyle.None;
        this.StartPosition = FormStartPosition.Manual;
        this.Location = new System.Drawing.Point(-2000, -2000);
        this.Size = new System.Drawing.Size(1, 1);
        this.Opacity = 0;
    }

    protected override void OnLoad(EventArgs e)
    {
        base.OnLoad(e);
        if (!RegisterHotKey(this.Handle, HOTKEY_ID, MOD_ALT | MOD_CONTROL, VK_M))
        {
            MessageBox.Show(
                "Failed to register Ctrl+Alt+M -- another app (or a leftover copy of this one) already owns it.",
                "TTS Stop Hotkey");
        }
    }

    protected override void WndProc(ref Message m)
    {
        if (m.Msg == WM_HOTKEY && m.WParam.ToInt32() == HOTKEY_ID)
        {
            StopAudio();
        }
        base.WndProc(ref m);
    }

    static void StopAudio()
    {
        // Belt-and-suspenders: the flag lets a script mid-poll-loop stop
        // gracefully, but Kill() below is what actually guarantees silence
        // immediately even if a script is stuck in a blocking PlaySync().
        try { File.WriteAllText(FlagPath, ""); } catch { }

        if (!File.Exists(PidsPath)) return;
        foreach (var line in File.ReadAllLines(PidsPath))
        {
            int pid;
            if (int.TryParse(line.Trim(), out pid))
            {
                try { Process.GetProcessById(pid).Kill(); } catch { }
            }
        }
        try { File.WriteAllText(PidsPath, ""); } catch { }
    }

    protected override void Dispose(bool disposing)
    {
        UnregisterHotKey(this.Handle, HOTKEY_ID);
        base.Dispose(disposing);
    }
}

class Program
{
    [STAThread]
    static void Main()
    {
        Application.Run(new HotkeyForm());
    }
}
