# SPDX-License-Identifier: MIT
"""Raccoglie e verifica le licenze in third_party/licenses + manifest.json.

Sottocomandi (significativi, senza rete, solo piccoli file di testo):

    python tools/collect_licenses.py check
        SELF-CONTAINED: legge solo third_party/licenses/manifest.json e
        verifica ogni file DEST per esistenza, byte e sha256. NON tocca
        build/, out/, .venv-camera, rete o git. Deve passare anche in un
        clone impacchettato senza dipendenze scaricate.

    python tools/collect_licenses.py collect
        Rigenera i file da sorgenti locali quando presenti (checkout
        FetchContent, venv, installazione Python, file gia' in repo);
        se una sorgente locale manca, conserva il file DEST esistente e
        ne registra l'hash (nessun fallimento per sorgente assente).
        Riscrive manifest.json con categoria, url, revisione, versione,
        sha256, byte, retrieval e verification.

Compatibilita': `--check` equivale a `check`; senza argomenti = `collect`.

Categorie manifest: "included" (dipendenze build/stage),
"reference" (solo studio: NESSUN codice incluso, la MIT non rilicenzia),
"runtime" (venv/Python di sistema, non vendorizzati come binari).

Limiti: ogni file <= 256 KiB; percorsi .tmp.drivedownload /
.tmp.driveupload sempre esclusi; nessun binario copiato.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEST = REPO / "third_party" / "licenses"
MANIFEST = DEST / "manifest.json"
MAX_BYTES = 256 * 1024

HIDAPI_PIN = "d6b2a974608dec3b76fb1e36c189f22b9cf3650c"
JSON_PIN = "55f93686c01528224f448c19128836e7df245f72"
OPENVR_PIN = "0924064316de3effbcd1acf1e309182a2deb1c05"
OPENHMD_PIN = "85075b0c7e3c723ded2577edb79d00ee11aac339"
PS3EYE_PIN = "fe03494e648d0bf2aaa77869e5b50965e2e23620"
LIBUSB_PIN = "d52e355daa09f17ce64819122cb067b8a2ee0d4b"
OPENPSVR_PIN = "cb86fd7a2fc7cb47733074bb49856b3af3095357"
LIBPSVR_PIN = "7858e9cffcd17da5c9e26b301d9d4442a1e6de93"
PSVRFW_PIN = "ccda7ca840f8f820197dd8571bbc869a02563ea4"


def git_head(path: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _rel_repo(path: Path) -> str:
    try:
        return path.relative_to(REPO).as_posix()
    except ValueError:
        return path.as_posix()


def load_small(path: Path) -> bytes:
    rel = _rel_repo(path)
    if ".tmp.drivedownload" in rel or ".tmp.driveupload" in rel:
        raise ValueError(f"percorso Drive escluso: {rel}")
    size = path.stat().st_size
    if size > MAX_BYTES:
        raise ValueError(f"file troppo grande ({size} byte): {rel}")
    return path.read_bytes()


def find_source(rels: tuple) -> Path | None:
    for r in rels:
        p = Path(r)
        if not p.is_absolute():
            p = REPO / r
        if p.is_file():
            return p
    return None


# (dest, category, version, source_candidates, url, rev, retrieval, verification)
# source_candidates: percorsi locali preferiti (relativi a REPO) oppure
# assoluti; tupla vuota = solo fetch URL gia' eseguito, si adotta DEST.
ENTRIES: tuple = (
    ("hidapi-LICENSE.txt", "included", "0.15.0",
     ("build/mingw/_deps/hidapi-src/LICENSE.txt",
      "build/vs2022/_deps/hidapi-src/LICENSE.txt"),
     "https://github.com/libusb/hidapi.git", HIDAPI_PIN,
     "copia byte-identica dal checkout FetchContent locale",
     "git rev-parse HEAD == pin"),
    ("hidapi-LICENSE-bsd.txt", "included", "0.15.0",
     ("build/mingw/_deps/hidapi-src/LICENSE-bsd.txt",
      "build/vs2022/_deps/hidapi-src/LICENSE-bsd.txt"),
     "https://github.com/libusb/hidapi.git", HIDAPI_PIN,
     "copia byte-identica dal checkout FetchContent locale",
     "git rev-parse HEAD == pin"),
    ("hidapi-LICENSE-gpl3.txt", "included", "0.15.0",
     ("build/mingw/_deps/hidapi-src/LICENSE-gpl3.txt",
      "build/vs2022/_deps/hidapi-src/LICENSE-gpl3.txt"),
     "https://github.com/libusb/hidapi.git", HIDAPI_PIN,
     "copia byte-identica dal checkout FetchContent locale",
     "git rev-parse HEAD == pin"),
    ("hidapi-LICENSE-orig.txt", "included", "0.15.0",
     ("build/mingw/_deps/hidapi-src/LICENSE-orig.txt",
      "build/vs2022/_deps/hidapi-src/LICENSE-orig.txt"),
     "https://github.com/libusb/hidapi.git", HIDAPI_PIN,
     "copia byte-identica dal checkout FetchContent locale",
     "git rev-parse HEAD == pin"),
    ("json-MIT.txt", "included", "3.12.0",
     ("build/mingw/_deps/nlohmann_json-src/LICENSE.MIT",
      "build/vs2022/_deps/nlohmann_json-src/LICENSE.MIT"),
     "https://github.com/nlohmann/json.git", JSON_PIN,
     "copia byte-identica dal checkout FetchContent locale",
     "git rev-parse HEAD == pin"),
    ("BSL-1.0.txt", "included", "1.0",
     ("third_party/BSL-1.0.txt",),
     "https://github.com/OpenHMD/OpenHMD", OPENHMD_PIN,
     "copia byte-identica da third_party/BSL-1.0.txt",
     "raw LICENSE al pin + confronto byte"),
    ("openvr-LICENSE.txt", "included", "SDK 2.15.6",
     ("build/vs2022/_deps/openvr_sdk-src/LICENSE",
      "build/mingw/_deps/openvr_sdk-src/LICENSE"),
     "https://github.com/ValveSoftware/openvr.git", OPENVR_PIN,
     "copia byte-identica dal checkout FetchContent locale",
     "git rev-parse HEAD == pin"),
    ("PS3EYEDriver-LICENSE.txt", "included", "pin 2017-06-16",
     ("out/pseye/PS3EYEDriver-LICENSE.txt",
      "tools/psvr_pseye/.deps/PS3EYEDriver/LICENSE"),
     "https://github.com/inspirit/PS3EYEDriver.git", PS3EYE_PIN,
     "copia byte-identica dallo stage out/pseye",
     "raw LICENSE al pin + confronto byte"),
    ("GPL-2.0.txt", "included", "2.0",
     ("tools/psvr_pseye/COPYING.GPL-2.0",),
     "https://github.com/inspirit/PS3EYEDriver.git "
     "(tabelle ov534 dal kernel Linux, via PS3EYEDriver LICENSE)",
     PS3EYE_PIN,
     "copia byte-identica da tools/psvr_pseye/COPYING.GPL-2.0",
     "testo FSF standard, hash registrato"),
    ("LGPL-2.1.txt", "included", "2.1",
     ("tools/psvr_pseye/COPYING.LGPL-2.1",),
     "https://github.com/libusb/libusb.git", LIBUSB_PIN,
     "copia byte-identica da tools/psvr_pseye/COPYING.LGPL-2.1",
     "testo FSF standard, hash registrato"),
    # Runtime/reference notices are retained in categorized subdirectories.
)


def verify_manifest(manifest_path: Path, dest_dir: Path) -> list:
    """SELF-CONTAINED: solo manifest + file DEST (sha256 e byte).

    Non legge build/, out/, venv, rete o git. Ritorna la lista errori
    (vuota = OK). Ogni errore e' una stringa leggibile.
    """
    errors = []
    try:
        manifest = json.loads(manifest_path.read_bytes().decode("utf-8"))
    except (OSError, ValueError) as exc:
        return [f"manifest illeggibile {manifest_path}: {exc}"]
    entries = manifest.get("licenses", [])
    if not entries:
        return ["manifest senza voci licenses"]
    seen = set()
    for item in entries:
        rel = item.get("path", "")
        prefix = "third_party/licenses/"
        relative = Path(rel[len(prefix):]) if rel.startswith(prefix) else Path("..")
        target = (dest_dir / relative).resolve()
        if (not rel.startswith(prefix) or relative.is_absolute() or ".." in relative.parts
                or dest_dir.resolve() not in target.parents):
            errors.append("invalid license path: " + rel)
            continue
        name = relative.as_posix()
        if name in seen:
            errors.append("duplicate license path: " + rel)
            continue
        seen.add(name)
        want_sha = item.get("sha256", "")
        want_bytes = item.get("bytes", -1)
        if not target.is_file():
            errors.append(f"mancante: {rel}")
            continue
        try:
            data = target.read_bytes()
        except OSError as exc:
            errors.append(f"illeggibile: {rel}: {exc}")
            continue
        if len(data) > MAX_BYTES:
            errors.append(f"troppo grande ({len(data)} byte): {rel}")
        if len(data) != want_bytes:
            errors.append(
                f"byte mismatch {rel}: manifest={want_bytes} disco={len(data)}")
        if sha256_of(data) != want_sha:
            errors.append(f"sha256 mismatch: {rel}")
    try:
        on_disk = set()
        for folder, directories, files in os.walk(dest_dir, followlinks=False):
            directories[:] = [name for name in directories
                              if name not in (".tmp.driveupload", ".tmp.drivedownload")]
            for name in files:
                path = Path(folder) / name
                if path != manifest_path:
                    on_disk.add(path.relative_to(dest_dir).as_posix())
    except OSError as exc:
        return errors + [f"dest illeggibile {dest_dir}: {exc}"]
    for extra in sorted(on_disk - seen):
        errors.append(f"file extra non in manifest: third_party/licenses/{extra}")
    return errors


def run_check(manifest_path: Path = MANIFEST, dest_dir: Path = DEST) -> int:
    errors = verify_manifest(manifest_path, dest_dir)
    count = 0 if errors else len(json.loads(manifest_path.read_text(encoding="utf-8")).get("licenses", []))
    if errors:
        print(f"collect_licenses check: FALLITO ({len(errors)} problemi)",
              file=sys.stderr)
        for err in errors:
            print(f"  ! {err}", file=sys.stderr)
        return 1
    print(f"collect_licenses check: OK ({count} licenze verificate, "
          f"solo manifest+DEST)")
    return 0


def check_manifest(root=REPO) -> dict:
    """Raise on invalid distributed notices; independent of upstream checkouts."""
    folder = Path(root) / "third_party/licenses"
    manifest_path = folder / "manifest.json"
    errors = verify_manifest(manifest_path, folder)
    if errors:
        raise ValueError("; ".join(errors))
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def supplemental_entries(dest_dir):
    """Notices collected in the categorized runtime/reference directories."""
    references = {"OpenPSVR-LICENSE.txt": ("alatnet/OpenPSVR", OPENPSVR_PIN, "LICENSE"),
                  "libpsvr-LICENSE.txt": ("alatnet/libpsvr", LIBPSVR_PIN, "LICENSE"),
                  "PSVRFramework-LICENSE.txt": ("gusmanb/PSVRFramework", PSVRFW_PIN, "LICENSE.md")}
    for folder, directories, files in os.walk(dest_dir, followlinks=False):
        directories[:] = sorted(name for name in directories
                                if name not in (".tmp.driveupload", ".tmp.drivedownload"))
        for name in sorted(files):
            relative = (Path(folder) / name).relative_to(dest_dir).as_posix()
            if "/" not in relative:
                continue
            if relative.startswith("references/"):
                repo, pin, source = references[name]
                category, version = "reference", pin
                url = f"https://raw.githubusercontent.com/{repo}/{pin}/{source}"
            else:
                category = "runtime"
                package = relative.split("/")[1]
                versions = {"numpy": "2.4.6", "opencv-contrib-python-headless": "5.0.0.93", "pytest": "9.1.1"}
                version = versions.get(package, "3.11.9" if package.startswith("CPython") else "8.6")
                pin = version
                if package in versions:
                    url = f"https://pypi.org/project/{package}/{version}/"
                elif package.startswith("CPython"):
                    url = "https://docs.python.org/3.11/license.html"
                else:
                    url = "https://github.com/tcltk/tcl" if package.startswith("Tcl") else "https://github.com/tcltk/tk"
            yield (relative, category, version, (), url, pin,
                   "original notice retained from pinned upstream/installed distribution",
                   "byte-preserving copy; sha256 recorded; no runtime/code bundled")


def run_collect(dest_dir: Path = DEST, manifest_path: Path = MANIFEST) -> dict:
    """Rigenera DEST dalle sorgenti locali quando presenti.

    Se la sorgente locale manca ma DEST esiste, conserva DEST (adozione)
    e ne registra l'hash: non fallisce mai per sorgente assente.
    Fallisce solo se mancano sia sorgente sia DEST.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    for (dest_name, category, version, rels, url, rev,
            retrieval, verification) in (*ENTRIES, *supplemental_entries(dest_dir)):
        src = find_source(rels)
        ver_note = verification
        if src is not None:
            data = load_small(src)
            if "FetchContent" in retrieval:
                head = git_head(src.parent)
                if head is not None and head != rev:
                    raise ValueError(
                        f"pin mismatch per {dest_name}: HEAD={head} atteso={rev}")
                if head is None:
                    ver_note = ("checkout senza .git; pin da CMakeLists, "
                                "hash registrato")
            src_rel: str | None = _rel_repo(src) if REPO in src.resolve().parents else "installed runtime: " + src.name
            (dest_dir / dest_name).write_bytes(data)
        else:
            target = dest_dir / dest_name
            if not target.is_file():
                raise FileNotFoundError(
                    f"sorgente e DEST mancanti per {dest_name} "
                    f"(cercato: {rels if rels else 'solo fetch URL'})")
            data = load_small(target)
            src_rel = None
            ver_note = verification + " [sorgente locale assente: DEST adottato]"
        manifest.append({
            "path": f"third_party/licenses/{dest_name}",
            "category": category,
            "version": version,
            "source_file": src_rel,
            "source_url": url,
            "revision": rev,
            "sha256": sha256_of(data),
            "bytes": len(data),
            "retrieval": retrieval,
            "verification": ver_note,
        })
    result = {"licenses": manifest}
    manifest_path.write_text(json.dumps(result, indent=2) + "\n",
                             encoding="utf-8")
    return result


def main(argv: list | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    # Compat: --check flag storico.
    if args == ["--check"]:
        args = ["check"]
    if not args:
        args = ["collect"]
    cmd = args[0]
    rest = args[1:]
    parser = argparse.ArgumentParser(prog="collect_licenses",
                                     description=__doc__)
    parser.add_argument("command", choices=("check", "collect"),
                        help="check=self-contained su manifest+DEST; "
                             "collect=rigenera da sorgenti locali")
    parser.add_argument("--dest", default=str(DEST),
                        help="directory licenze (default third_party/licenses)")
    parser.add_argument("--manifest", default=str(MANIFEST),
                        help="manifest json (default third_party/licenses/manifest.json)")
    try:
        ns = parser.parse_args([cmd] + rest)
    except SystemExit as exc:
        return int(exc.code or 0)
    dest_dir = Path(ns.dest)
    manifest_path = Path(ns.manifest)
    try:
        if ns.command == "check":
            return run_check(manifest_path, dest_dir)
        result = run_collect(dest_dir, manifest_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"collect_licenses: ERRORE: {exc}", file=sys.stderr)
        return 1
    print(f"collect_licenses: {len(result['licenses'])} licenze OK")
    for item in result["licenses"]:
        print(f"  [{item['category']}] {item['path']} "
              f"sha256={item['sha256'][:16]}... {item['bytes']}B")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
