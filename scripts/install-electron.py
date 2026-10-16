"""Install the pinned Windows x64 Electron runtime with SHA256 verification.

2026-10-16. Uses Python's standard HTTPS/system-proxy support.
Only the local npm package's version and checksums are trusted; no engine is
bundled in this source repository. Run after npm ci and backend venv setup.
"""
from pathlib import Path, PurePosixPath
import hashlib
import json
import os
import platform
import shutil
import sys
import tempfile
import urllib.request
import zipfile


def main():
    if sys.platform != "win32" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise RuntimeError("This installer is verified for Windows x64 only; use Electron's official installer on other platforms.")
    root = Path(__file__).resolve().parent.parent
    package = root / "tianshu_ui/node_modules/electron"
    if not (package / "package.json").is_file():
        raise RuntimeError("Run npm ci in tianshu_ui first.")
    version = json.loads((package / "package.json").read_text(encoding="utf-8"))["version"]
    if not version or any(c not in "0123456789." for c in version):
        raise RuntimeError("Expected a pinned stable Electron version.")
    filename = f"electron-v{version}-win32-x64.zip"
    expected = json.loads((package / "checksums.json").read_text(encoding="utf-8"))[filename]
    cache = Path(tempfile.gettempdir()) / "tianshu-electron-cache"
    cache.mkdir(exist_ok=True)
    archive = cache / filename

    def valid(path):
        if not path.is_file():
            return False
        with path.open("rb") as f:
            return hashlib.file_digest(f, "sha256").hexdigest() == expected

    if not valid(archive):
        url = f"https://github.com/electron/electron/releases/download/v{version}/{filename}"
        print(f"Downloading Electron {version} (Windows x64)...", flush=True)
        temporary = archive.with_suffix(".download")
        request = urllib.request.Request(url, headers={"User-Agent": "Tianshu-source-installer"})
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        if not valid(temporary):
            raise RuntimeError("Electron SHA256 verification failed; refusing extraction.")
        os.replace(temporary, archive)
    destination = (package / "dist").resolve()
    with zipfile.ZipFile(archive) as z:
        for member in z.infolist():
            name = PurePosixPath(member.filename.replace("\\", "/"))
            if name.is_absolute() or ".." in name.parts or ":" in member.filename:
                raise RuntimeError("Unsafe archive member; refusing extraction.")
            if not (destination / member.filename).resolve().is_relative_to(destination):
                raise RuntimeError("Archive member escapes Electron directory.")
        z.extractall(destination)
    if not (destination / "electron.exe").is_file():
        raise RuntimeError("Electron executable is missing after extraction.")
    (package / "path.txt").write_text("electron.exe", encoding="utf-8")
    print(f"Electron {version} installed; SHA256 verified.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # URLs/settings from proxy errors can contain credentials; keep errors terse.
        print(f"Electron setup failed ({type(exc).__name__}). Check network/proxy access to GitHub and retry; runtime was not accepted without its expected checksum.", file=sys.stderr)
        raise SystemExit(1)
