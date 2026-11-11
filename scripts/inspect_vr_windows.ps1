# Read-only window enumeration scoped to live SteamVR compositor/monitor PIDs.
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
if (-not ('PSVR.ViewWindows' -as [type])) { Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
namespace PSVR {
    public class ViewWindow {
        public long hwnd; public uint pid; public string title, class_name;
        public bool visible, minimized; public int x, y, width, height;
    }
    public static class ViewWindows {
        private delegate bool Callback(IntPtr window, IntPtr argument);
        [StructLayout(LayoutKind.Sequential)] private struct RECT { public int l,t,r,b; }
        [DllImport("user32.dll")] private static extern bool EnumWindows(Callback callback, IntPtr argument);
        [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr window, out uint pid);
        [DllImport("user32.dll", CharSet=CharSet.Unicode)] private static extern int GetWindowText(IntPtr window, StringBuilder text, int length);
        [DllImport("user32.dll", CharSet=CharSet.Unicode)] private static extern int GetClassName(IntPtr window, StringBuilder text, int length);
        [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr window);
        [DllImport("user32.dll")] private static extern bool IsIconic(IntPtr window);
        [DllImport("user32.dll")] private static extern bool GetWindowRect(IntPtr window, out RECT rect);
        [DllImport("user32.dll")] private static extern IntPtr SetThreadDpiAwarenessContext(IntPtr context);
        public static ViewWindow[] Inspect(uint[] pids) {
            var result = new List<ViewWindow>(); var wanted = new HashSet<uint>(pids);
            var previous = SetThreadDpiAwarenessContext(new IntPtr(-4));
            try { EnumWindows((window,arg) => {
                uint pid; GetWindowThreadProcessId(window,out pid);
                if (!wanted.Contains(pid)) return true;
                var title = new StringBuilder(512); GetWindowText(window,title,title.Capacity);
                var cls = new StringBuilder(256); GetClassName(window,cls,cls.Capacity);
                RECT rect; GetWindowRect(window,out rect);
                result.Add(new ViewWindow { hwnd=window.ToInt64(), pid=pid, title=title.ToString(), class_name=cls.ToString(),
                    visible=IsWindowVisible(window), minimized=IsIconic(window), x=rect.l,y=rect.t,width=rect.r-rect.l,height=rect.b-rect.t });
                return true;
            },IntPtr.Zero); } finally { if (previous != IntPtr.Zero) SetThreadDpiAwarenessContext(previous); }
            return result.ToArray();
        }
    }
}
'@ }
$pids = @((Get-Process -Name vrcompositor,vrmonitor,vrwebhelper -ErrorAction SilentlyContinue).Id | ForEach-Object { [uint32]$_ })
[PSVR.ViewWindows]::Inspect([uint32[]]$pids) | ConvertTo-Json -Depth 4
