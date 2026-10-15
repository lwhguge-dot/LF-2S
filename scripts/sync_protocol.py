"""Explicit, hash-tracked snapshot; runtime never imports the authority tree."""

import argparse
import hashlib
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT.parent / "01_通信协议"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(source=SOURCE):
    saved = json.loads((ROOT / "protocol/manifest.json").read_text("utf-8"))
    authority_available = source.is_dir()
    for entry in saved["files"]:
        target = ROOT / entry["target"]
        if digest(target) != entry["sha256"]:
            raise ValueError(f"Bundled protocol hash mismatch: {entry['target']}")
        if authority_available and digest(source / entry["source"]) != entry["sha256"]:
            raise ValueError(f"Authority protocol hash mismatch: {entry['source']}")
    return {
        "passed": True,
        "files": len(saved["files"]),
        "authority_checked": authority_available,
        "authority_unchanged": True if authority_available else None,
        "dialect_hash": saved["dialect_hash"],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--source", type=Path, default=SOURCE)
    args = parser.parse_args()
    authority = args.source.resolve()
    source = authority / "02_参考实现/python/lf2s_protocol"
    target = ROOT / "protocol/lf2s_protocol"
    manifest = ROOT / "protocol/manifest.json"
    if args.check:
        print(json.dumps(verify(authority)))
        return
    if not source.is_dir():
        parser.error("同步需要原协议工程；请用 --source 指定。独立仓库可运行 --check。")
    target.mkdir(parents=True, exist_ok=True)
    files = []
    for item in sorted(source.glob("*.py")):
        copy = target / item.name
        shutil.copyfile(item, copy)
        files.append(
            {
                "source": item.relative_to(authority).as_posix(),
                "target": copy.relative_to(ROOT).as_posix(),
                "sha256": digest(item),
            }
        )
    license_source = authority / "04_资料依据/pymavlink-2.4.50-COPYING.txt"
    license_target = ROOT / "protocol/pymavlink-COPYING.txt"
    shutil.copyfile(license_source, license_target)
    files.append(
        {
            "source": license_source.relative_to(authority).as_posix(),
            "target": license_target.relative_to(ROOT).as_posix(),
            "sha256": digest(license_source),
        }
    )
    manifest.write_text(
        json.dumps(
            {
                "authority": "P1/01_通信协议",
                "version": "2.0.0-draft1",
                "dialect_hash": "5889088805303263",
                "files": files,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        "utf-8",
    )
    fixture = json.loads(
        (authority / "03_验证/fixtures/simulation_profile.json").read_text("utf-8")
    )["profile"]
    fixture.update(config_valid=0, validated_mask=0)
    (ROOT / "simulation/profile.json").write_text(
        json.dumps(fixture, indent=2) + "\n", "utf-8"
    )
    print(f"Snapshotted {len(files)} protocol files; all original bytes retained")


if __name__ == "__main__":
    main()
