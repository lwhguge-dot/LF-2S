"""Official manual Electron distribution, with a private frozen onedir backend."""

import hashlib
import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from licenses import collect

ROOT = Path(__file__).resolve().parents[1]
VERSION = json.loads((ROOT / "package.json").read_text("utf-8"))["version"]
PUBLIC_EVIDENCE = (
    "workbench-public-summary.json",
    "workbench-package-idle.png",
    "workbench-package-road.png",
    "workbench-package-cash.png",
    "workbench-package-mobile.png",
    "design-source-desktop.png",
    "design-source-mobile.png",
    "design-comparison-package.png",
    "design-comparison-mobile.png",
    "design-detail-package-header.png",
    "design-detail-package-right.png",
    "design-detail-package-assistant.png",
    "README.md",
    "验收报告.md",
    "acceptance.json",
    "public-verification.json",
    "preview-stability-summary.json",
    "preview-performance-summary.json",
    "package-overview.png",
    "package-controller-fault.png",
    "controller-diagnostics-summary.json",
    "package-stale.png",
    "package-logs.png",
    "package-compact.png",
    "package-task-active.png",
    "package-task-completed.png",
    "package-replay.png",
    "package-bench-disconnected.png",
    "fc-usb-public-summary.json",
    "fc-usb-package-online.png",
    "fc-usb-package-disconnected.png",
    "text-public-summary.json",
    "text-package-review.png",
    "voice-public-summary.json",
    "voice-package-review.png",
)


def source_inputs():
    paths = []
    for name in ("desktop", "frontend", "service", "simulation", "protocol"):
        paths.extend(
            path
            for path in (ROOT / name).rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix != ".pyc"
        )
    paths.extend(
        ROOT / name
        for name in (
            "package.json",
            "package-lock.json",
            "runtime-lock.json",
            "requirements.lock",
            "requirements-dev.lock",
            "tsconfig.json",
            "vite.config.mjs",
            "scripts/package.py",
            "scripts/licenses.py",
            "scripts/sync_protocol.py",
            "scripts/Start-CompanyModel.ps1",
        )
    )
    return {
        path.relative_to(ROOT).as_posix(): hashlib.sha256(
            path.read_bytes().replace(b"\r\n", b"\n")
        ).hexdigest()
        for path in sorted(paths)
    }


def update_documents(destination):
    shutil.copyfile(
        ROOT / "scripts/Start-CompanyModel.ps1", destination / "Start-CompanyModel.ps1"
    )
    for file in ("README.md", "THIRD_PARTY.md", "CHANGELOG.md"):
        shutil.copyfile(ROOT / file, destination / file)
    if (ROOT / "design-qa.md").is_file():
        shutil.copyfile(ROOT / "design-qa.md", destination / "design-qa.md")
    # Electron's own LICENSE must remain under its original name.
    shutil.copyfile(ROOT / "LICENSE", destination / "LF2S-LICENSE.txt")
    shutil.copytree(ROOT / "docs", destination / "docs", dirs_exist_ok=True)
    shutil.copyfile(
        ROOT / "protocol/manifest.json", destination / "protocol-manifest.json"
    )
    collect(destination / "THIRD_PARTY_LICENSES")
    (destination / "evidence").mkdir(exist_ok=True)
    for name in PUBLIC_EVIDENCE:
        item = ROOT / "evidence" / name
        if item.is_file():
            shutil.copyfile(item, destination / "evidence" / name)


def manifest(destination):
    manifest = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "version": VERSION,
        "scope": "Visual task workbench and non-thinking text suggestions, Mandarin review, vendor FC USB and ESP32 benches; S0–S2 simulation/replay; no live vision, company ASR deployment, human Mandarin, UART/control or flight acceptance",
        "directory": destination.name,
        "source_sha256_lf": source_inputs(),
        "files": [],
    }
    for item in sorted(destination.rglob("*")):
        if item.is_file() and item.name != "bundle-manifest.json":
            manifest["files"].append(
                {
                    "path": item.relative_to(destination).as_posix(),
                    "bytes": item.stat().st_size,
                    "sha256": hashlib.sha256(item.read_bytes()).hexdigest(),
                }
            )
    manifest["total_bytes"] = sum(item["bytes"] for item in manifest["files"])
    (destination / "bundle-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8"
    )
    (ROOT / "evidence/package-manifest.json").write_text(
        json.dumps(manifest, indent=2), "utf-8"
    )
    (ROOT / "release/latest.json").write_text(
        json.dumps({"directory": destination.name}, indent=2), "utf-8"
    )
    print(json.dumps({"directory": str(destination), "bytes": manifest["total_bytes"]}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--refresh-docs",
        action="store_true",
        help="Refresh only documentation, evidence and license/manifest data",
    )
    args = parser.parse_args()
    if args.refresh_docs:
        destination = (
            ROOT
            / "release"
            / Path(
                json.loads((ROOT / "release/latest.json").read_text("utf-8"))[
                    "directory"
                ]
            )
        )
        if destination.resolve().parent != (ROOT / "release").resolve():
            raise ValueError(
                "Refusing to modify a destination outside this release directory"
            )
        saved = json.loads((destination / "bundle-manifest.json").read_text("utf-8"))
        if saved.get("source_sha256_lf") != source_inputs():
            raise ValueError(
                "Source inputs changed; rebuild the package before refreshing documents"
            )
        update_documents(destination)
        manifest(destination)
        return
    destination = ROOT / f"release/LF2S-Station-{VERSION}-win-x64"
    if destination.exists():
        # Never remove a potentially running/user-owned directory; keep earlier builds reviewable.
        destination = destination.with_name(
            destination.name + "-" + datetime.now().strftime("%Y%m%d-%H%M%S")
        )
    shutil.copytree(ROOT / "node_modules/electron/dist", destination)
    (destination / "electron.exe").rename(destination / "LF2S-Station.exe")
    app = destination / "resources/app"
    app.mkdir(parents=True)
    package = json.loads((ROOT / "package.json").read_text("utf-8"))
    package.pop("devDependencies", None)
    package.pop(
        "dependencies", None
    )  # Vue is in the compiled bundle, main/preload use only built-ins.
    (app / "package.json").write_text(json.dumps(package, indent=2), "utf-8")
    shutil.copytree(ROOT / "desktop", app / "desktop")
    shutil.copytree(ROOT / "build/ui", app / "build/ui")
    shutil.copytree(
        ROOT / "build/backend/lf2s-service", destination / "resources/backend"
    )
    update_documents(destination)
    manifest(destination)


if __name__ == "__main__":
    main()
