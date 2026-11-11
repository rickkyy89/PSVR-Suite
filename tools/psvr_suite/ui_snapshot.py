"""Read-only snapshot of the suite window for UI verification, not lens optics."""
import argparse
import ctypes as C
from ctypes import wintypes as W
from pathlib import Path

import cv2
import numpy as np


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True)
    a=p.parse_args(argv)
    if a.out.exists() or not a.out.parent.is_dir():
        p.error('new output in existing directory required')
    user=C.WinDLL('user32',use_last_error=True);gdi=C.WinDLL('gdi32',use_last_error=True)
    user.FindWindowW.argtypes=[W.LPCWSTR,W.LPCWSTR];user.FindWindowW.restype=W.HWND
    user.GetWindowDC.argtypes=[W.HWND];user.GetWindowDC.restype=W.HDC
    user.ReleaseDC.argtypes=[W.HWND,W.HDC]
    user.GetWindowRect.argtypes=[W.HWND,C.POINTER(W.RECT)]
    user.PrintWindow.argtypes=[W.HWND,W.HDC,W.UINT]
    gdi.CreateCompatibleDC.argtypes=[W.HDC];gdi.CreateCompatibleDC.restype=W.HDC
    gdi.CreateCompatibleBitmap.argtypes=[W.HDC,C.c_int,C.c_int];gdi.CreateCompatibleBitmap.restype=W.HBITMAP
    gdi.SelectObject.argtypes=[W.HDC,W.HGDIOBJ];gdi.SelectObject.restype=W.HGDIOBJ
    gdi.DeleteObject.argtypes=[W.HGDIOBJ];gdi.DeleteDC.argtypes=[W.HDC]
    hwnd=user.FindWindowW(None,'PSVR Suite') or user.FindWindowW(None,'PSVR · Suite utente')
    if not hwnd: raise RuntimeError('suite window not found')
    rect=W.RECT();user.GetWindowRect(hwnd,C.byref(rect));w=rect.right-rect.left;h=rect.bottom-rect.top
    dc=user.GetWindowDC(hwnd);mem=gdi.CreateCompatibleDC(dc);bitmap=gdi.CreateCompatibleBitmap(dc,w,h)
    old=gdi.SelectObject(mem,bitmap)
    class Header(C.Structure):
        _fields_=[('size',W.DWORD),('width',W.LONG),('height',W.LONG),('planes',W.WORD),('bits',W.WORD),
                  ('compression',W.DWORD),('image_size',W.DWORD),('xp',W.LONG),('yp',W.LONG),('used',W.DWORD),('important',W.DWORD)]
    class Info(C.Structure): _fields_=[('header',Header),('colors',W.DWORD*3)]
    info=Info();info.header=Header(C.sizeof(Header),w,-h,1,32,0,0,0,0,0,0)
    data=C.create_string_buffer(w*h*4)
    gdi.GetDIBits.argtypes=[W.HDC,W.HBITMAP,W.UINT,W.UINT,C.c_void_p,C.POINTER(Info),W.UINT]
    try:
        if not user.PrintWindow(hwnd,mem,2): raise RuntimeError('PrintWindow failed')
        if gdi.GetDIBits(mem,bitmap,0,h,data,C.byref(info),0)!=h: raise RuntimeError('GetDIBits failed')
        image=np.frombuffer(data,dtype=np.uint8).reshape(h,w,4)[:,:,:3]
        if not cv2.imwrite(str(a.out),image): raise RuntimeError('snapshot write failed')
    finally:
        gdi.SelectObject(mem,old);gdi.DeleteObject(bitmap);gdi.DeleteDC(mem);user.ReleaseDC(hwnd,dc)
    print(str(a.out.resolve()))


if __name__=='__main__': main()
