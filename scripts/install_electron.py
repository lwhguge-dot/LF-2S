"""Restore the official pinned Windows Electron ZIP with bounded download retry."""

import hashlib
import json
import shutil
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    saved = json.loads((ROOT / "runtime-lock.json").read_text("utf-8"))["electron"]
    wrapper = ROOT / "node_modules/electron"
    metadata = json.loads((wrapper / "package.json").read_text("utf-8"))
    if sys.platform != "win32" or metadata["version"] != saved["version"]:
        raise ValueError("Only the pinned Windows x64 Electron runtime is supported")
    cache = ROOT / ".work/cache/electron"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / saved["archive"]
    if not archive.exists() or digest(archive) != saved["sha256"]:
        partial = archive.with_suffix(".part")
        url = f"https://github.com/electron/electron/releases/download/v{saved['version']}/{saved['archive']}"
        for attempt in range(1, 4):
            try:
                print(
                    f"Official Electron download {attempt}/3: {saved['archive']}",
                    flush=True,
                )
                started = last_notice = time.monotonic()
                size = 0
                with (
                    urllib.request.urlopen(url, timeout=30) as response,
                    partial.open("wb") as output,
                ):
                    while chunk := response.read(1024 * 1024):
                        size += len(chunk)
                        if size > 400 * 1024 * 1024 or time.monotonic() - started > 240:
                            raise TimeoutError(
                                "Electron download exceeded its size/time budget"
                            )
                        output.write(chunk)
                        if time.monotonic() - last_notice > 15:
                            print(f"Downloaded {size // (1024 * 1024)} MiB", flush=True)
                            last_notice = time.monotonic()
                if digest(partial) != saved["sha256"]:
                    raise ValueError("Official Electron archive SHA-256 mismatch")
                partial.replace(archive)
                break
            except (OSError, urllib.error.URLError):
                if attempt == 3:
                    raise
                time.sleep(attempt)
    if digest(archive) != saved["sha256"]:
        raise ValueError("Electron archive SHA-256 mismatch")
    destination = wrapper / "dist"
    destination.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        for item in bundle.infolist():
            target = (destination / item.filename).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise ValueError("Unsafe archive path")
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(item) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
    if digest(destination / "electron.exe") != saved["executable_sha256"]:
        raise ValueError("Electron executable SHA-256 mismatch")
    (wrapper / "path.txt").write_text("electron.exe", "ascii")
    print(
        json.dumps({"electron": saved["version"], "verified_sha256": saved["sha256"]})
    )


if __name__ == "__main__":
    main()
