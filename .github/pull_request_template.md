# Release checklist (`0.3.0-alpha`, pre-release, not stable)

- [ ] Version `0.3.0-alpha` (aligned with CMake 0.3.0 and VERSION file)
- [ ] Accepted choices intact: identity / One Euro 2.5-8 / fallback default off / diagnostic FOV quoted as personal
- [ ] Known limits stated: no 90 fps claim, -202 workaround only, fallback opt-in, optics uncalibrated
- [ ] Offline checks green: `build.ps1` + CTest + `pytest tests -k suite` + `test_projection_trial.ps1` + `--headless-check` + `collect_licenses.py --check`
- [ ] Source zip `dist/psvr-suite-0.3.0-alpha-source.zip` (no binaries, no user paths, no credentials)
