"""Keep release metadata and verified archive hashes before removing old software."""

import hashlib
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HISTORY = ROOT / "evidence/history/distribution-records"
VERSION = json.loads((ROOT / "package.json").read_text("utf-8"))["version"]
ASSET = re.compile(r"LF2S-Station-(\d+\.\d+\.\d+)-win-x64")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    HISTORY.mkdir(parents=True, exist_ok=True)
    index_path = HISTORY / "index.json"
    saved = json.loads(index_path.read_text("utf-8")) if index_path.exists() else {}
    archives = saved.get("archives", {})
    files = {row["path"]: row for row in saved.get("files", [])}

    def retain(source, relative):
        target = (HISTORY / relative).resolve()
        if not target.is_relative_to(HISTORY.resolve()):
            raise ValueError("Retention metadata escaped history directory")
        source_hash = digest(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and digest(target) != source_hash:
            raise ValueError(f"Historical metadata changed: {relative}")
        if not target.exists():
            shutil.copyfile(source, target)
        files[relative] = {
            "path": relative,
            "sha256": source_hash,
            "bytes": source.stat().st_size,
        }

    for archive in sorted((ROOT / "release/assets").glob("*.zip")):
        match = ASSET.fullmatch(archive.stem)
        if not match or match[1] == VERSION:
            continue
        archive_hash = digest(archive)
        checksum = archive.with_suffix(".zip.sha256")
        verification = archive.with_suffix(".verification.json")
        if checksum.read_text("ascii").split()[0] != archive_hash:
            raise ValueError(f"Old ZIP checksum mismatch: {archive.name}")
        verified = json.loads(verification.read_text("utf-8"))
        if not verified["passed"] or verified["sha256"] != archive_hash:
            raise ValueError(f"Old ZIP verification mismatch: {archive.name}")
        archives[archive.name] = {
            "version": match[1],
            "sha256": archive_hash,
            "bytes": archive.stat().st_size,
            "checked_while_original_present": True,
            "scope": "Retained historical digest; not a fresh execution or retained ZIP",
        }
        for suffix in (".zip.sha256", ".manifest.json", ".verification.json"):
            metadata = archive.with_suffix(suffix)
            retain(metadata, "assets/" + metadata.name)
    for directory in sorted((ROOT / "release").glob("LF2S-Station-*-win-x64*")):
        manifest = directory / "bundle-manifest.json"
        if manifest.is_file():
            retain(manifest, "bundles/" + directory.name + ".json")
    for row in files.values():
        if digest(HISTORY / row["path"]) != row["sha256"]:
            raise ValueError("Retained release metadata integrity failed")
    record = {
        "timestamp": datetime.now().astimezone().isoformat(),
        "scope": "Metadata only; sources and original measurements remain at their original paths",
        "archives": archives,
        "files": sorted(files.values(), key=lambda row: row["path"]),
    }
    index_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(json.dumps({"retained_metadata": len(files), "historical_archive_hashes": len(archives)}))


if __name__ == "__main__":
    main()
