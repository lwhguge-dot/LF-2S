"""Collect runtime dependency notices into the distributable (no network)."""

import importlib.metadata
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def collect(destination):
    destination.mkdir(parents=True, exist_ok=True)
    records = []
    names = re.findall(
        r"^([a-zA-Z0-9_-]+)==", (ROOT / "requirements.lock").read_text("utf-8"), re.M
    )
    for name in names:
        dist = importlib.metadata.distribution(name)
        folder = destination / name
        folder.mkdir(exist_ok=True)
        notices = []
        for item in dist.files or []:
            if any(
                word in str(item).lower() for word in ("license", "copying", "notice")
            ):
                source = Path(dist.locate_file(item))
                if source.is_file():
                    target = folder / str(item).replace("../", "").replace("..\\", "")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
                    notices.append(target.relative_to(destination).as_posix())
        metadata = dist.read_text("METADATA") or ""
        (folder / "METADATA.txt").write_text(metadata, "utf-8")
        records.append({"name": name, "version": dist.version, "notices": notices})
    for source in [ROOT / "node_modules/vue", *(ROOT / "node_modules/@vue").iterdir()]:
        if not source.is_dir():
            continue
        folder = destination / ("vue" if source.name == "vue" else "vue-" + source.name)
        folder.mkdir(exist_ok=True)
        for pattern in ("LICENSE*", "COPYING*", "NOTICE*", "package.json"):
            for item in source.glob(pattern):
                if item.is_file():
                    shutil.copyfile(item, folder / item.name)
    for candidate in [
        Path(sys.base_prefix) / "LICENSE.txt",
        Path(sys.base_prefix) / "LICENSE",
    ]:
        if candidate.is_file():
            shutil.copyfile(candidate, destination / "Python-LICENSE.txt")
            break
    shutil.copyfile(
        ROOT / "protocol/lf2s_protocol/dialect.py",
        destination / "MAVLink-generated-dialect.py.txt",
    )
    shutil.copyfile(
        ROOT / "protocol/pymavlink-COPYING.txt",
        destination / "MAVLink-generator-COPYING.txt",
    )
    (destination / "runtime-dependencies.json").write_text(
        json.dumps(records, indent=2), "utf-8"
    )


if __name__ == "__main__":
    collect(ROOT / "build/licenses")
