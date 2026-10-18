"""Publish verified assets as a GitHub prerelease; never replace an existing asset."""

import argparse
import hashlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "lwhguge-dot/LF-2S"
API = f"https://api.github.com/repos/{REPOSITORY}"


def credentials():
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    result = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        text=True,
        capture_output=True,
        timeout=30,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"},
    )
    values = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    if not values.get("password"):
        raise RuntimeError(
            "未找到 GitHub 登录；请先通过 Git Credential Manager 或 gh auth login 登录，不要把令牌写入源码。"
        )
    return values["password"]


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", type=Path, default=ROOT / "release/assets")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check local release inputs without remote writes or authentication",
    )
    args = parser.parse_args()
    version = json.loads((ROOT / "package.json").read_text("utf-8"))["version"]
    tag = f"v{version}"
    name = f"LF2S-Station-{version}-win-x64"
    assets = [
        args.assets / (name + suffix)
        for suffix in (".zip", ".zip.sha256", ".manifest.json", ".verification.json")
    ]
    for item in assets:
        if not item.is_file():
            raise FileNotFoundError(item.name)
    zipped, checksum, manifest, verification = assets
    if checksum.read_text("ascii").strip() != f"{digest(zipped)}  {zipped.name}":
        raise ValueError("ZIP/checksum mismatch")
    verified = json.loads(verification.read_text("utf-8"))
    listed = json.loads(manifest.read_text("utf-8"))
    if (
        not verified["passed"]
        or verified["sha256"] != digest(zipped)
        or listed["version"] != version
        or verified["manifest_files"] != len(listed["files"])
        or not all(check["passed"] for check in verified["checks"])
    ):
        raise ValueError("Release must match the extracted, verified ZIP")
    if not listed.get("source_sha256_lf"):
        raise ValueError("Bundle has no recorded source inputs; rebuild it")
    for relative, expected in listed["source_sha256_lf"].items():
        source = (ROOT / relative).resolve()
        if (
            not source.is_relative_to(ROOT.resolve())
            or hashlib.sha256(source.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
            != expected
        ):
            raise ValueError(f"Source changed after packaging: {relative}")
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    tagged = subprocess.check_output(
        ["git", "rev-parse", f"{tag}^{{commit}}"], cwd=ROOT, text=True
    ).strip()
    if commit != tagged:
        raise ValueError("HEAD is not the version tag")
    if subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=ROOT, text=True
    ).strip():
        raise RuntimeError("Commit source and public evidence before publishing")
    notes = (ROOT / "docs/releases" / f"{tag}.md").read_text("utf-8")
    title = notes.splitlines()[0].removeprefix("# ").strip()
    if not title.startswith(tag + " · "):
        raise ValueError("Release notes must start with the matching version and title")
    if args.check:
        print(
            json.dumps(
                {
                    "ready": True,
                    "tag": tag,
                    "commit": commit,
                    "assets": [
                        {
                            "name": item.name,
                            "bytes": item.stat().st_size,
                            "sha256": digest(item),
                        }
                        for item in assets
                    ],
                }
            )
        )
        return
    token = credentials()

    def request(url, method="GET", data=None, file=None, allow_missing=False):
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + token,
            "User-Agent": "LF2S-Station-Release",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        stream = None
        if file:
            stream = file.open("rb")
            body = stream
            headers.update(
                {
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(file.stat().st_size),
                }
            )
        else:
            body = json.dumps(data).encode("utf-8") if data is not None else None
            if body is not None:
                headers["Content-Type"] = "application/json"
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=headers, data=body, method=method),
                timeout=300,
            ) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if allow_missing and error.code == 404:
                return None
            message = json.load(error).get("message", "GitHub request failed")
            raise RuntimeError(f"GitHub HTTP {error.code}: {message}") from None
        finally:
            if stream:
                stream.close()

    remote_commit = request(f"{API}/commits/{tag}")["sha"]
    if remote_commit != commit:
        raise ValueError("Remote tag does not match the local commit")
    release = request(f"{API}/releases/tags/{tag}", allow_missing=True)
    if release and not release["draft"]:
        raise RuntimeError(
            "此版本已发布；不替换已发布版本。请递增 package.json 版本并新建标签。"
        )
    if not release:
        release = request(
            API + "/releases",
            "POST",
            {
                "tag_name": tag,
                "target_commitish": commit,
                "name": title,
                "body": notes,
                "draft": True,
                "prerelease": True,
            },
        )
    if release["target_commitish"] not in (commit, tag):
        raise RuntimeError(
            "Existing draft targets another commit; review it before retrying"
        )
    verified_assets = []
    for item in assets:
        current = next(
            (asset for asset in release["assets"] if asset["name"] == item.name), None
        )
        expected = "sha256:" + digest(item)
        if not current:
            upload = release["upload_url"].split("{")[0]
            print(
                json.dumps({"uploading": item.name, "bytes": item.stat().st_size}),
                flush=True,
            )
            current = request(
                upload + "?" + urllib.parse.urlencode({"name": item.name}),
                "POST",
                file=item,
            )
        deadline = time.monotonic() + 30
        while not current.get("digest") and time.monotonic() < deadline:
            time.sleep(1)
            current = request(current["url"])
        if (
            current["state"] != "uploaded"
            or current["size"] != item.stat().st_size
            or current.get("digest") != expected
        ):
            raise RuntimeError(
                f"Remote asset verification failed: {item.name}; draft remains unpublished"
            )
        print(
            json.dumps({"asset": item.name, "sha256": expected, "verified": True}),
            flush=True,
        )
        verified_assets.append(
            {
                "name": item.name,
                "size": current["size"],
                "sha256": expected.removeprefix("sha256:"),
                "download_url": current["browser_download_url"],
            }
        )
    release = request(
        f"{API}/releases/{release['id']}",
        "PATCH",
        {
            "name": title,
            "body": notes,
            "draft": False,
            "prerelease": True,
            "make_latest": "false",
        },
    )
    record = {
        "published": not release["draft"],
        "prerelease": release["prerelease"],
        "url": release["html_url"],
        "tag": tag,
        "commit": commit,
        "assets": verified_assets,
    }
    (ROOT / "release" / f"published-{tag}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(record, ensure_ascii=False))


if __name__ == "__main__":
    main()
