"""ZIP the whole self-contained bundle; verify hashes and run the extracted app."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = json.loads((ROOT / "package.json").read_text("utf-8"))["version"]
NAME = f"LF2S-Station-{VERSION}-win-x64"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    assets = ROOT / "release/assets"
    archive = assets / f"{NAME}.zip"
    checksum = assets / f"{NAME}.zip.sha256"
    if not args.verify:
        latest = json.loads((ROOT / "release/latest.json").read_text("utf-8"))
        source = (ROOT / "release" / latest["directory"]).resolve()
        if source.parent != (ROOT / "release").resolve():
            raise ValueError("Bundle must be within release/")
        assets.mkdir(parents=True, exist_ok=True)
        if archive.exists() or checksum.exists():
            raise FileExistsError(
                "Release assets already exist; move the old assets before rebuilding"
            )
        with zipfile.ZipFile(
            archive, "x", zipfile.ZIP_DEFLATED, compresslevel=6
        ) as bundle:
            for item in sorted(source.rglob("*")):
                if item.is_file():
                    bundle.write(item, Path(NAME) / item.relative_to(source))
        checksum.write_text(f"{digest(archive)}  {archive.name}\n", "ascii")
        print(json.dumps({"archive": archive.name, "bytes": archive.stat().st_size}))
        return
    if digest(archive) != checksum.read_text("ascii").split()[0]:
        raise ValueError("ZIP SHA-256 does not match")
    scratch = ROOT / ".work"
    scratch.mkdir(exist_ok=True)
    extracted = Path(tempfile.mkdtemp(prefix="zip-check-", dir=scratch))
    with zipfile.ZipFile(archive) as bundle:
        if bundle.testzip() is not None:
            raise ValueError("ZIP CRC verification failed")
        for name in bundle.namelist():
            if not (extracted / name).resolve().is_relative_to(extracted.resolve()):
                raise ValueError("Unsafe ZIP path")
        bundle.extractall(extracted)
    directory = extracted / NAME
    manifest = json.loads((directory / "bundle-manifest.json").read_text("utf-8"))
    if manifest["version"] != VERSION:
        raise ValueError("Package version differs from source")
    for item in manifest["files"]:
        target = (directory / item["path"]).resolve()
        if not target.is_relative_to(directory.resolve()):
            raise ValueError("Unsafe manifest path")
        if digest(target) != item["sha256"] or target.stat().st_size != item["bytes"]:
            raise ValueError(f"Bundle integrity failed: {item['path']}")
    subprocess.run(
        [shutil.which("node"), str(ROOT / "tests/desktop.cjs"), "--packaged"],
        cwd=ROOT,
        env={
            **os.environ,
            "LF2S_TEST_DISTRIBUTION": str(directory),
            "LF2S_TEST_REPORT": "desktop-archive.json",
        },
        check=True,
    )
    report = {
        "passed": True,
        "archive": archive.name,
        "sha256": digest(archive),
        "bytes": archive.stat().st_size,
        "manifest_files": len(manifest["files"]),
        "extracted_desktop_report": "desktop-archive.json",
        "scope": "Extracted Windows bundle on the same host; not cross-host or hardware acceptance",
    }
    (ROOT / "evidence/archive-integrity.json").write_text(
        json.dumps(report, indent=2) + "\n", "utf-8"
    )
    desktop = json.loads((ROOT / "evidence/desktop-archive.json").read_text("utf-8"))
    (assets / f"{NAME}.verification.json").write_text(
        json.dumps(
            {**report, "checks": desktop["checks"], "runtime": desktop["runtime"]},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        "utf-8",
    )
    shutil.copyfile(
        directory / "bundle-manifest.json", assets / f"{NAME}.manifest.json"
    )
    print(json.dumps(report))


if __name__ == "__main__":
    main()
