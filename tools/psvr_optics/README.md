# Offline optical profile validator

Windows x64 standalone. No USB, display enumeration or SteamVR calls.

```powershell
.\psvr_optics.exe --help
.\psvr_optics.exe --validate "C:\path\profile.json" --json
```

Validates strict JSON schema v1, finite values, radial monotonicity and separate
SDK float numerical-usability policy. Unicode Windows paths are supported.
The `optical_acceptance_verified` field is always false: validation cannot
establish lens calibration or correct physical UV mapping.

Exit codes: 0 valid math and SDK policy; 1 invalid arguments;
3 malformed/unreadable profile; 4 mathematically valid but SDK-unusable profile.

Packaged `identity_unverified.json` is an explicit diagnostic identity example,
not measured PSVR coefficients. See repository docs/MILESTONE_6_SOFTWARE.md.
