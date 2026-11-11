# SPDX-License-Identifier: MIT
# Read-only public NVAPI display/EDID queries. No setters, acquisition or overrides.
# Signatures/IDs: NVIDIA nvapi.h and nvapi_interface.h (public NVAPI SDK).
param([switch]$IncludeDirectEnumeration)
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public static class PsvrNvDisplayProbe {
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern IntPtr LoadLibraryEx(string path, IntPtr file, uint flags);
    [DllImport("kernel32.dll", CharSet=CharSet.Ansi, ExactSpelling=true)]
    static extern IntPtr GetProcAddress(IntPtr module, string name);
    [DllImport("kernel32.dll")] static extern bool FreeLibrary(IntPtr module);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate IntPtr Query(uint id);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate int Simple();
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate int Enumerate([Out,MarshalAs(UnmanagedType.LPArray,SizeConst=64)] IntPtr[] handles,out uint count);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate int Connected(IntPtr gpu,IntPtr rows,ref uint count,uint flags);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate int Map(uint display,out IntPtr gpu,out uint output);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate int Edid(IntPtr gpu,uint output,IntPtr data);
    // Optional private read-only enumeration, ABI observed in installed SteamVR
    // 2.17.10: vendor, count pointer, opaque 16-byte rows, enumeration mode.
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate int DirectEnum(uint vendor,ref uint count,IntPtr rows,uint mode);
    static T Function<T>(Query query,uint id) where T:class {
        var p=query(id); if(p==IntPtr.Zero) throw new Exception("NVAPI function unavailable: 0x"+id.ToString("X8"));
        return Marshal.GetDelegateForFunctionPointer(p,typeof(T)) as T;
    }
    static void Check(int result,string name) { if(result!=0) throw new Exception(name+" NVAPI status="+result); }
    public class Display {
        public uint display_id; public bool active,os_visible,connected,physically_connected;
        public int edid_status; public uint edid_vendor,edid_product; public bool base_checksum_valid;
    }
    public class DirectList {
        public uint vendor,mode,count; public int status;
    }
    public static DirectList[] InspectDirect() {
        var dll=LoadLibraryEx("nvapi64.dll",IntPtr.Zero,0x800);
        if(dll==IntPtr.Zero) throw new Exception("Cannot load System32 nvapi64.dll");
        Simple unload=null; bool initialized=false;
        try {
            var ptr=GetProcAddress(dll,"nvapi_QueryInterface");
            if(ptr==IntPtr.Zero) throw new Exception("NVAPI query export unavailable");
            var query=(Query)Marshal.GetDelegateForFunctionPointer(ptr,typeof(Query));
            Check(Function<Simple>(query,0x0150e828)(),"Initialize"); initialized=true;
            unload=Function<Simple>(query,0xd22bdd7e);
            var enumerate=Function<DirectEnum>(query,0x602f60d8);
            var result=new List<DirectList>();
            foreach(uint vendor in new uint[]{0xd94d,0x4dd9}) {
                foreach(uint mode in new uint[]{0,1}) {
                    uint count=1024; var rows=Marshal.AllocHGlobal(1024*16);
                    try {
                        for(int i=0;i<1024*16;i++) Marshal.WriteByte(rows,i,0);
                        int status=enumerate(vendor,ref count,rows,mode);
                        if(count>1024) throw new Exception("Private enumeration exceeded observed SteamVR limit");
                        result.Add(new DirectList{vendor=vendor,mode=mode,count=count,status=status});
                    } finally { Marshal.FreeHGlobal(rows); }
                }
            }
            return result.ToArray();
        } finally { if(initialized && unload!=null) unload(); FreeLibrary(dll); }
    }
    public static Display[] Inspect() {
        var dll=LoadLibraryEx("nvapi64.dll",IntPtr.Zero,0x800); // System32 only.
        if(dll==IntPtr.Zero) throw new Exception("Cannot load System32 nvapi64.dll");
        Simple unload=null; bool initialized=false;
        try {
            var qptr=GetProcAddress(dll,"nvapi_QueryInterface");
            if(qptr==IntPtr.Zero) throw new Exception("NVAPI query export unavailable");
            var query=(Query)Marshal.GetDelegateForFunctionPointer(qptr,typeof(Query));
            Check(Function<Simple>(query,0x0150e828)(),"Initialize"); initialized=true;
            unload=Function<Simple>(query,0xd22bdd7e);
            var enumerate=Function<Enumerate>(query,0xe5ac921f);
            var connected=Function<Connected>(query,0x0078dba2);
            var map=Function<Map>(query,0x112ba1a5);
            var edid=Function<Edid>(query,0x37d32e69);
            var gpus=new IntPtr[64]; uint gpuCount; Check(enumerate(gpus,out gpuCount),"EnumPhysicalGPUs");
            if(gpuCount>64) throw new Exception("GPU count exceeds limit");
            var result=new List<Display>();
            for(int g=0;g<gpuCount;g++) {
                uint count=0; Check(connected(gpus[g],IntPtr.Zero,ref count,1),"GetConnectedDisplayIds count");
                if(count>128) throw new Exception("Display count exceeds limit");
                if(count==0) continue;
                uint capacity=count; var rows=Marshal.AllocHGlobal((int)count*16);
                try {
                    for(int i=0;i<count*16;i++) Marshal.WriteByte(rows,i,0);
                    for(int i=0;i<count;i++) Marshal.WriteInt32(rows,i*16,0x30010); // NV_GPU_DISPLAYIDS v3, 16 bytes.
                    Check(connected(gpus[g],rows,ref count,1),"GetConnectedDisplayIds");
                    if(count>capacity) throw new Exception("Display topology changed");
                    for(int i=0;i<count;i++) {
                        uint id=(uint)Marshal.ReadInt32(rows,i*16+8), flags=(uint)Marshal.ReadInt32(rows,i*16+12);
                        var d=new Display {display_id=id,active=(flags&4)!=0,os_visible=(flags&16)!=0,connected=(flags&64)!=0,physically_connected=(flags&(1u<<17))!=0};
                        IntPtr gpu; uint output; Check(map(id,out gpu,out output),"GetGpuAndOutputIdFromDisplayId");
                        var data=Marshal.AllocHGlobal(272);
                        try {
                            for(int b=0;b<272;b++) Marshal.WriteByte(data,b,0);
                            Marshal.WriteInt32(data,0x30110); // NV_EDID_V3.
                            d.edid_status=edid(gpu,output,data);
                            if(d.edid_status==0) {
                                d.edid_vendor=(uint)(Marshal.ReadByte(data,12)|(Marshal.ReadByte(data,13)<<8));
                                d.edid_product=(uint)(Marshal.ReadByte(data,14)|(Marshal.ReadByte(data,15)<<8));
                                int sum=0; for(int b=0;b<128;b++) sum+=Marshal.ReadByte(data,4+b);
                                d.base_checksum_valid=(sum&255)==0 && Marshal.ReadByte(data,4)==0 && Marshal.ReadByte(data,5)==255;
                            }
                        } finally { Marshal.FreeHGlobal(data); }
                        result.Add(d);
                    }
                } finally { Marshal.FreeHGlobal(rows); }
            }
            return result.ToArray();
        } finally { if(initialized && unload!=null) unload(); FreeLibrary(dll); }
    }
}
'@
$result = [ordered]@{ read_only=$true; source='Public NVAPI GPU-reported EDID and uncached display flags'; displays=@([PsvrNvDisplayProbe]::Inspect()) }
if ($IncludeDirectEnumeration) {
    $result.direct_enumeration = @([PsvrNvDisplayProbe]::InspectDirect())
    $result.direct_enumeration_note = 'Private read-only function 0x602F60D8; ABI observed in SteamVR2.17.10. Raw modes 0/1 are not independently documented. Both vendor byte orders tested; no setters called.'
}
[pscustomobject]$result | ConvertTo-Json -Depth 5
