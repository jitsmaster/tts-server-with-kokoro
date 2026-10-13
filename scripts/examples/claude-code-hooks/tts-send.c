/* tts-send.exe -- tiny native hook client for the resident TTS player (tts-hook.exe --daemon).
 *
 * Why: a .NET Framework exe costs ~110-150 ms just to start on this machine (powershell.exe ~1 s).
 * This C client starts in a few ms: it reads the hook JSON from stdin, sends one framed message to the
 * player over loopback TCP (127.0.0.1:8882), waits for the 1-byte ack and exits.
 *
 *   tts-send.exe speak|stop|elicit  < hook-payload.json
 *
 * Why TCP and not the named pipe: on this machine opening a named pipe from a freshly started process
 * stalls for 0.5-9 s at random (measured with a probe: pipe open 0-2600 ms, loopback TCP connect 0-16 ms
 * in the same conditions; a normal file open is also 0-16 ms). The player still listens on the pipe
 * too (tts-hook.exe's own client and --quit use it).
 *
 * Frame = int32 length + "mode\npayload" (UTF-8), ack = 'K'. Same protocol as the pipe.
 * If no player is running it starts one (tts-hook.exe --daemon) and retries for ~5 s; if that fails it
 * hands the payload to a detached one-shot worker (tts-hook.exe <mode> --worker <file>).
 *
 * Build (mingw gcc, GUI subsystem so no console window flashes):
 *   gcc -O2 -s -mwindows -o tts-send.exe tts-send.c -lws2_32
 */
#include <winsock2.h>
#include <windows.h>
#include <stdint.h>
#include <string.h>
#include <stdlib.h>
#include <stdio.h>

#define PLAYER_PORT 8882

static char claude_dir[MAX_PATH];
static void spawn(const char *args, int breakaway);
static int spawned = 0;               /* 1 = this send had to start the player */
static unsigned refused_n = 0;        /* connect attempts that failed, for the SLOW log */
static DWORD max_call = 0;            /* longest single connect(), ms */

/* One connect attempt with a time limit. A plain blocking connect() to a port nobody listens on takes ~2 s
   on Windows (SYN retransmits before the refusal shows), so give up on each attempt after 200 ms and
   try again; once the player listens, a fresh attempt completes in under a millisecond. */
static SOCKET try_connect(DWORD limitMs)
{
    SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    struct sockaddr_in a; u_long nb = 1; fd_set w, e; struct timeval tv; int err = 0, el = sizeof err;
    if (s == INVALID_SOCKET) return s;
    ioctlsocket(s, FIONBIO, &nb);
    ZeroMemory(&a, sizeof a);
    a.sin_family = AF_INET; a.sin_port = htons(PLAYER_PORT); a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (connect(s, (struct sockaddr *)&a, sizeof a) != 0 && WSAGetLastError() != WSAEWOULDBLOCK) { closesocket(s); return INVALID_SOCKET; }
    FD_ZERO(&w); FD_ZERO(&e); FD_SET(s, &w); FD_SET(s, &e);
    tv.tv_sec = 0; tv.tv_usec = (long)limitMs * 1000;
    if (select(0, NULL, &w, &e, &tv) > 0 && FD_ISSET(s, &w)
        && getsockopt(s, SOL_SOCKET, SO_ERROR, (char *)&err, &el) == 0 && err == 0) {
        nb = 0; ioctlsocket(s, FIONBIO, &nb);
        return s;
    }
    closesocket(s);
    return INVALID_SOCKET;
}

/* Connects to the player. The player holds the named mutex Global\TtsHookDaemon for its whole life: if
   that does not exist there is no player, so start one at once instead of waiting for connects to fail.
   With a player present a short retry window covers it being busy; without one we wait up to 5 s for the
   new player to come up. */
static SOCKET open_sock(void)
{
    DWORD t0 = GetTickCount(), maxMs = 1500;
    HANDLE m = OpenMutexA(SYNCHRONIZE, FALSE, "Global\\TtsHookDaemon");
    if (m) CloseHandle(m);
    else { spawned = 1; spawn("--daemon", 1); maxMs = 5000; }
    for (;;) {
        DWORD c0 = GetTickCount(), now;
        SOCKET s = try_connect(200);
        if (s != INVALID_SOCKET) {
            if (GetTickCount() - c0 > max_call) max_call = GetTickCount() - c0;
            return s;
        }
        refused_n++;
        now = GetTickCount() - t0;
        if (GetTickCount() - c0 > max_call) max_call = GetTickCount() - c0;
        if (now >= maxMs) return INVALID_SOCKET;
        if (GetTickCount() - c0 < 20) Sleep(20);
    }
}

static int send_all(SOCKET s, const char *p, int n)
{
    while (n > 0) {
        int w = send(s, p, n, 0);
        if (w <= 0) return 0;
        p += w; n -= w;
    }
    return 1;
}

static void spawn(const char *args, int breakaway)
{
    char cmd[2 * MAX_PATH + 64];
    STARTUPINFOA si; PROCESS_INFORMATION pi;
    DWORD flags = CREATE_NO_WINDOW | DETACHED_PROCESS;
    lstrcpyA(cmd, "\""); lstrcatA(cmd, claude_dir); lstrcatA(cmd, "\\tts-hook.exe\" "); lstrcatA(cmd, args);
    ZeroMemory(&si, sizeof si); si.cb = sizeof si;
    ZeroMemory(&pi, sizeof pi);
    if (CreateProcessA(NULL, cmd, NULL, NULL, FALSE, flags | (breakaway ? CREATE_BREAKAWAY_FROM_JOB : 0), NULL, claude_dir, &si, &pi)
        || (breakaway && CreateProcessA(NULL, cmd, NULL, NULL, FALSE, flags, NULL, claude_dir, &si, &pi))) {
        CloseHandle(pi.hThread); CloseHandle(pi.hProcess);
    }
}

/* Appends one line to tts-hook.log when a send took over 250 ms, with where the time went. */
static void log_slow(DWORD t0, DWORD t1, DWORD t2, DWORD t3, const char *mode)
{
    char path[MAX_PATH], line[320]; SYSTEMTIME st; HANDLE f; DWORD w; int n;
    if (t3 - t0 < 250) return;
    lstrcpyA(path, claude_dir); lstrcatA(path, "\\tts-hook.log");
    GetLocalTime(&st);
    n = wsprintfA(line, "%04d-%02d-%02dT%02d:%02d:%02d send[%s] SLOW total=%lums stdin=%lums connect=%lums ack=%lums spawned=%d failed_connects=%u maxcall=%lums\r\n",
                  st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute, st.wSecond, mode,
                  t3 - t0, t1 - t0, t2 - t1, t3 - t2, spawned, refused_n, (unsigned long)max_call);
    f = CreateFileA(path, FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (f != INVALID_HANDLE_VALUE) { WriteFile(f, line, n, &w, NULL); CloseHandle(f); }
}

int WINAPI WinMain(HINSTANCE hi, HINSTANCE hp, LPSTR cmdline, int show)
{
    char mode[16]; char path[MAX_PATH];
    size_t cap = 1 << 16, len = 0; char *body;
    HANDLE in; SOCKET sock; WSADATA wsa; DWORD got; int i;
    DWORD t0 = GetTickCount(), t1, t2;
    (void)hi; (void)hp; (void)show;

    while (*cmdline == ' ') cmdline++;
    for (i = 0; i < 15 && cmdline[i] && cmdline[i] != ' '; i++) mode[i] = cmdline[i];
    mode[i] = 0;
    if (strcmp(mode, "speak") && strcmp(mode, "stop") && strcmp(mode, "elicit")) return 2;

    if (!GetEnvironmentVariableA("USERPROFILE", claude_dir, MAX_PATH - 20)) return 1;
    lstrcatA(claude_dir, "\\.claude");
    lstrcpyA(path, claude_dir); lstrcatA(path, "\\tts-muted.flag");
    if (GetFileAttributesA(path) != INVALID_FILE_ATTRIBUTES) return 0;

    /* frame: 4-byte length, mode, '\n', payload */
    body = (char *)malloc(cap);
    if (!body) return 1;
    memcpy(body + 4, mode, strlen(mode)); len = 4 + strlen(mode); body[len++] = '\n';
    in = GetStdHandle(STD_INPUT_HANDLE);
    if (in && in != INVALID_HANDLE_VALUE) {
        for (;;) {
            if (len + 65536 > cap) { cap *= 2; body = (char *)realloc(body, cap); if (!body) return 1; }
            if (!ReadFile(in, body + len, 65536, &got, NULL) || got == 0) break;
            len += got;
        }
    }
    { int32_t n = (int32_t)(len - 4); memcpy(body, &n, 4); }

    t1 = GetTickCount();
    sock = INVALID_SOCKET;
    if (WSAStartup(MAKEWORD(2, 2), &wsa) == 0) sock = open_sock();
    if (sock != INVALID_SOCKET) {
        char ack = 0; DWORD rcvto = 3000;
        t2 = GetTickCount();
        setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, (const char *)&rcvto, sizeof rcvto);
        if (send_all(sock, body, (int)len) && recv(sock, &ack, 1, 0) == 1 && ack == 'K') {
            closesocket(sock); log_slow(t0, t1, t2, GetTickCount(), mode); return 0;
        }
        closesocket(sock);
        log_slow(t0, t1, t2, GetTickCount(), mode);
        return 1;                 /* delivered-or-not is unknown: do not risk speaking twice */
    }

    /* last resort: detached one-shot worker with the payload in a temp file */
    {
        char tmpdir[MAX_PATH], tmpfile[MAX_PATH], args[2 * MAX_PATH]; HANDLE f; DWORD w;
        GetTempPathA(MAX_PATH, tmpdir); GetTempFileNameA(tmpdir, "tts", 0, tmpfile);
        f = CreateFileA(tmpfile, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
        if (f == INVALID_HANDLE_VALUE) return 1;
        WriteFile(f, body + 4 + strlen(mode) + 1, (DWORD)(len - 4 - strlen(mode) - 1), &w, NULL);
        CloseHandle(f);
        lstrcpyA(args, mode); lstrcatA(args, " --worker \""); lstrcatA(args, tmpfile); lstrcatA(args, "\"");
        spawn(args, 1);
    }
    return 0;
}
