# PSVR display inventory (M5 development)

Windows x64, read-only. Commands:

```powershell
.\psvr_display.exe --help
.\psvr_display.exe --json
```

Queries active Windows display paths, exact monitor device paths, adapter/source/
target identifiers, desktop and scanout geometry, rational refresh rates,
rotation/scanline/scaling and available GDI modes. Raw EDID bytes are not read;
EDID-reported manufacturer/product IDs have explicit availability flags.

No mode changes, USB access, EDID override or automatic selection. A 1920x1080
candidate hint is not proof that a monitor is PSVR. GDI integer-Hz modes are not
evidence of exact fractional scanout. Exit 0 success, 1 bad arguments,
3 enumeration/output failure. The --help path does not enumerate displays.

The driver accepts only an explicitly selected, active, unrotated, progressive,
identity-scaled 1920x1080 target with a unique non-cloned source and matching
desktop coordinates/refresh. M5 compositor output remains manually unverified.
See source repository docs/MILESTONE_5_REPORT.md for the later runtime procedure.
