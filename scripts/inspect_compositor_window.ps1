# Read-only window inspection. No activation, placement, input or runtime settings changes.
# The temporary DPI context affects only this querying thread and is restored in finally.
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$runtimeDirectory = 'C:\Program Files (x86)\Steam\steamapps\common\SteamVR\bin\win64'
$expectedPath = Join-Path $runtimeDirectory 'vrcompositor.exe'
$warnings = New-Object 'System.Collections.Generic.List[string]'

if (-not (Test-Path -LiteralPath $expectedPath -PathType Leaf)) {
    $warnings.Add("Confirmed runtime executable is not present: $expectedPath")
}

$source = @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

namespace PSVR.WindowInspection {
    public sealed class Bounds {
        public int x, y, w, h;
        public Bounds(RECT r) { x = r.Left; y = r.Top; w = r.Right - r.Left; h = r.Bottom - r.Top; }
    }
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left, Top, Right, Bottom; }
    public sealed class WindowRecord {
        public string hwnd, executable_path, class_name, window_title, style_hex, exstyle_hex;
        public uint ownerPID, window_thread_id;
        public bool visible, minimized, foreground;
        public Bounds physical_get_window_rect, dwm_extended_frame_bounds;
        public uint? dpi;
        public string dpi_awareness_context;
    }
    public sealed class Report {
        public string query_thread_dpi_context;
        public bool get_window_rect_is_physical;
        public List<WindowRecord> windows = new List<WindowRecord>();
        public List<string> warnings = new List<string>();
    }
    public static class Native {
        private delegate bool EnumProc(IntPtr hwnd, IntPtr param);
        [DllImport("user32.dll", SetLastError=true)] private static extern bool EnumWindows(EnumProc callback, IntPtr param);
        [DllImport("user32.dll", SetLastError=true)] private static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
        [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr hwnd);
        [DllImport("user32.dll")] private static extern bool IsIconic(IntPtr hwnd);
        [DllImport("user32.dll")] private static extern IntPtr GetForegroundWindow();
        [DllImport("user32.dll", SetLastError=true)] private static extern bool GetWindowRect(IntPtr hwnd, out RECT rect);
        [DllImport("user32.dll", EntryPoint="GetWindowLongPtrW", SetLastError=true)] private static extern IntPtr GetWindowLongPtr64(IntPtr hwnd, int index);
        [DllImport("user32.dll", EntryPoint="GetWindowLongW", SetLastError=true)] private static extern int GetWindowLong32(IntPtr hwnd, int index);
        [DllImport("user32.dll", CharSet=CharSet.Unicode, SetLastError=true)] private static extern int GetClassName(IntPtr hwnd, StringBuilder text, int count);
        [DllImport("user32.dll", CharSet=CharSet.Unicode, SetLastError=true)] private static extern int GetWindowText(IntPtr hwnd, StringBuilder text, int count);
        [DllImport("user32.dll", CharSet=CharSet.Unicode, SetLastError=true)] private static extern int GetWindowTextLength(IntPtr hwnd);
        [DllImport("user32.dll", SetLastError=true)] private static extern IntPtr SetThreadDpiAwarenessContext(IntPtr context);
        [DllImport("user32.dll")] private static extern IntPtr GetThreadDpiAwarenessContext();
        [DllImport("user32.dll")] private static extern IntPtr GetWindowDpiAwarenessContext(IntPtr hwnd);
        [DllImport("user32.dll")] private static extern bool AreDpiAwarenessContextsEqual(IntPtr a, IntPtr b);
        [DllImport("user32.dll")] private static extern uint GetDpiForWindow(IntPtr hwnd);
        [DllImport("dwmapi.dll")] private static extern int DwmGetWindowAttribute(IntPtr hwnd, int attribute, out RECT rect, int size);
        [DllImport("kernel32.dll", SetLastError=true)] private static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] private static extern bool QueryFullProcessImageName(IntPtr process, uint flags, StringBuilder path, ref uint size);
        [DllImport("kernel32.dll")] private static extern bool CloseHandle(IntPtr handle);
        [DllImport("kernel32.dll")] private static extern void SetLastError(uint error);

        private static void Warn(Report report, string message) {
            if (!report.warnings.Contains(message)) report.warnings.Add(message);
        }
        private static string Error() { return new Win32Exception(Marshal.GetLastWin32Error()).Message; }
        private static bool Missing(Exception e) { return e is EntryPointNotFoundException || e is DllNotFoundException; }
        private static string Context(IntPtr context) {
            if (context == IntPtr.Zero) return "unknown";
            int[] values = { -4, -3, -2, -1, -5 };
            string[] names = { "PER_MONITOR_AWARE_V2", "PER_MONITOR_AWARE", "SYSTEM_AWARE", "UNAWARE", "UNAWARE_GDISCALED" };
            for (int i = 0; i < values.Length; i++)
                if (AreDpiAwarenessContextsEqual(context, new IntPtr(values[i]))) return names[i];
            return "unknown (0x" + context.ToInt64().ToString("X") + ")";
        }
        private static string Style(IntPtr hwnd, int index, Report report) {
            SetLastError(0);
            long value;
            if (IntPtr.Size == 8) {
                try { value = GetWindowLongPtr64(hwnd, index).ToInt64(); }
                catch (EntryPointNotFoundException) {
                    Warn(report, "GetWindowLongPtrW unavailable; using GetWindowLongW for 32-bit style flags.");
                    value = GetWindowLong32(hwnd, index);
                }
            } else value = GetWindowLong32(hwnd, index);
            if (value == 0 && Marshal.GetLastWin32Error() != 0) {
                Warn(report, "Style read failed for compositor HWND 0x" + hwnd.ToInt64().ToString("X") + ": " + Error());
                return null;
            }
            return "0x" + unchecked((uint)value).ToString("X8");
        }
        private static string ProcessPath(uint pid, Report report) {
            IntPtr process = OpenProcess(0x1000, false, pid); // PROCESS_QUERY_LIMITED_INFORMATION only
            if (process == IntPtr.Zero) {
                Warn(report, "Cannot verify executable path for PID " + pid + ": " + Error());
                return null;
            }
            try {
                StringBuilder path = new StringBuilder(32768);
                uint size = (uint)path.Capacity;
                if (QueryFullProcessImageName(process, 0, path, ref size)) return path.ToString();
                Warn(report, "Cannot query executable path for PID " + pid + ": " + Error());
                return null;
            } finally { CloseHandle(process); }
        }
        public static Report Inspect(string expectedPath) {
            Report report = new Report();
            IntPtr previous = IntPtr.Zero;
            try {
                try {
                    previous = SetThreadDpiAwarenessContext(new IntPtr(-4));
                    if (previous != IntPtr.Zero) {
                        report.query_thread_dpi_context = "PER_MONITOR_AWARE_V2";
                        report.get_window_rect_is_physical = true;
                    } else Warn(report, "Cannot enter query-thread PerMonitorV2 DPI context: " + Error());
                } catch (Exception e) {
                    if (!Missing(e)) throw;
                    Warn(report, "SetThreadDpiAwarenessContext unavailable; retaining existing query-thread context.");
                }
                if (!report.get_window_rect_is_physical) {
                    try {
                        report.query_thread_dpi_context = Context(GetThreadDpiAwarenessContext());
                        report.get_window_rect_is_physical = report.query_thread_dpi_context == "PER_MONITOR_AWARE" || report.query_thread_dpi_context == "PER_MONITOR_AWARE_V2";
                    } catch (Exception e) {
                        if (!Missing(e)) throw;
                        report.query_thread_dpi_context = "unknown";
                        Warn(report, "Query-thread DPI context APIs unavailable.");
                    }
                    if (!report.get_window_rect_is_physical)
                        Warn(report, "Physical GetWindowRect coordinates cannot be guaranteed; physical_get_window_rect is omitted rather than reporting DPI-virtualized bounds.");
                }
                IntPtr foreground = GetForegroundWindow();
                Dictionary<uint, string> paths = new Dictionary<uint, string>();
                EnumProc callback = delegate(IntPtr hwnd, IntPtr unused) {
                    try {
                        uint pid;
                        uint thread = GetWindowThreadProcessId(hwnd, out pid);
                        if (thread == 0 || pid == 0) return true;
                        string path;
                        if (!paths.TryGetValue(pid, out path)) {
                            path = ProcessPath(pid, report);
                            paths[pid] = path;
                        }
                        if (!String.Equals(path, expectedPath, StringComparison.OrdinalIgnoreCase)) return true;
                        // Only verified compositor windows reach title/class queries.
                        WindowRecord window = new WindowRecord();
                        window.hwnd = "0x" + hwnd.ToInt64().ToString("X");
                        window.ownerPID = pid; window.window_thread_id = thread; window.executable_path = path;
                        window.visible = IsWindowVisible(hwnd); window.minimized = IsIconic(hwnd);
                        window.foreground = hwnd == foreground;
                        window.style_hex = Style(hwnd, -16, report); window.exstyle_hex = Style(hwnd, -20, report);
                        StringBuilder text = new StringBuilder(256);
                        if (GetClassName(hwnd, text, text.Capacity) == 0) Warn(report, "GetClassName failed for " + window.hwnd + ": " + Error());
                        window.class_name = text.ToString();
                        text = new StringBuilder(Math.Max(1, GetWindowTextLength(hwnd) + 1));
                        GetWindowText(hwnd, text, text.Capacity); window.window_title = text.ToString();
                        RECT rect;
                        if (GetWindowRect(hwnd, out rect)) {
                            if (report.get_window_rect_is_physical) window.physical_get_window_rect = new Bounds(rect);
                        } else Warn(report, "GetWindowRect failed for " + window.hwnd + ": " + Error());
                        try {
                            int hr = DwmGetWindowAttribute(hwnd, 9, out rect, Marshal.SizeOf(typeof(RECT)));
                            if (hr == 0) window.dwm_extended_frame_bounds = new Bounds(rect);
                            else Warn(report, "DWM extended frame bounds unavailable for " + window.hwnd + ": HRESULT 0x" + unchecked((uint)hr).ToString("X8"));
                        } catch (Exception e) { if (!Missing(e)) throw; Warn(report, "DWM API unavailable; extended frame bounds omitted."); }
                        try {
                            uint dpi = GetDpiForWindow(hwnd);
                            if (dpi != 0) window.dpi = dpi;
                            else Warn(report, "GetDpiForWindow returned zero for " + window.hwnd);
                        } catch (Exception e) { if (!Missing(e)) throw; Warn(report, "GetDpiForWindow unavailable; DPI omitted."); }
                        try { window.dpi_awareness_context = Context(GetWindowDpiAwarenessContext(hwnd)); }
                        catch (Exception e) { if (!Missing(e)) throw; Warn(report, "Window DPI awareness context APIs unavailable."); }
                        // A window may disappear or be recycled while being inspected.
                        uint finalPid;
                        if (GetWindowThreadProcessId(hwnd, out finalPid) == thread && finalPid == pid) report.windows.Add(window);
                        else Warn(report, "Compositor window disappeared or changed ownership during inspection: " + window.hwnd);
                    } catch (Exception e) {
                        Warn(report, "Window inspection failed: " + e.GetType().Name + ": " + e.Message);
                    }
                    return true;
                };
                SetLastError(0);
                if (!EnumWindows(callback, IntPtr.Zero)) Warn(report, "EnumWindows failed: " + Error());
                GC.KeepAlive(callback);
            } finally {
                if (previous != IntPtr.Zero && SetThreadDpiAwarenessContext(previous) == IntPtr.Zero)
                    Warn(report, "Failed to restore original query-thread DPI context: " + Error());
            }
            return report;
        }
    }
}
'@

$nativeReport = $null
try {
    if (-not ('PSVR.WindowInspection.Native' -as [type])) {
        Add-Type -TypeDefinition $source -Language CSharp -ErrorAction Stop
    }
    $nativeReport = [PSVR.WindowInspection.Native]::Inspect($expectedPath)
    foreach ($warning in $nativeReport.warnings) { $warnings.Add($warning) }
    if ($nativeReport.windows.Count -eq 0) {
        $warnings.Add('No top-level window matched the exact live compositor executable path. This does not establish the cause of fullscreen error -202.')
    }
} catch {
    $warnings.Add("Diagnostic unavailable: $($_.Exception.Message)")
}

[pscustomobject][ordered]@{
    timestamp_utc = [DateTime]::UtcNow.ToString('o')
    expected_compositor_path = $expectedPath
    query_thread_dpi_context = $(if ($null -ne $nativeReport) { $nativeReport.query_thread_dpi_context } else { $null })
    get_window_rect_is_physical = $(if ($null -ne $nativeReport) { $nativeReport.get_window_rect_is_physical } else { $false })
    reference_active_source_physical_bounds = [ordered]@{ x = 3840; y = 492; w = 1920; h = 1080 }
    reference_bounds_origin = 'Parent-provided QueryDisplay activeSource; not queried by this script.'
    windows = @($(if ($null -ne $nativeReport) { $nativeReport.windows.ToArray() }))
    warnings = @($warnings.ToArray())
    interpretation = 'Snapshot only. Compare physical_get_window_rect with activeSource bounds; DWM bounds exclude invisible resize borders. Window DPI describes its awareness (GetDpiForWindow may return 96 for DPI-unaware windows). Foreground is sampled once, not focus history. These observations alone cannot prove the cause of fullscreen error -202.'
} | ConvertTo-Json -Depth 8
