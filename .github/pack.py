#!/usr/bin/env python3
"""Build and publish the monthly resource-pack releases."""

from __future__ import annotations

import argparse
import fnmatch
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
RELEASE_PREFIX = "Nightly-"


def run(*args: str, capture: bool = False) -> str:
    result = subprocess.run(
        args,
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout.strip() if capture else ""


def release_tag(run_number: str, now: datetime) -> str:
    return f"{RELEASE_PREFIX}{now:%Y-%m-%d-%H}-Build-{run_number}"


def zip_pack(destination: Path, source: Path = ROOT) -> None:
    excluded = (".git/*", ".github/*", "*.zip", "*.psd", "*.bak", "*备用*", "*副本*", "renovate.json")
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in source.rglob("*"):
            if not path.is_file() or path == destination:
                continue
            relative = path.relative_to(source).as_posix()
            if any(fnmatch.fnmatch(relative, pattern) for pattern in excluded):
                continue
            archive.write(path, relative)


def archive_branch(branch: str, destination: Path) -> None:
    branch_ref = next(
        (
            candidate
            for candidate in (branch, f"origin/{branch}")
            if subprocess.run(
                ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{candidate}"],
                cwd=ROOT,
                check=False,
            ).returncode
            == 0
            or subprocess.run(
                ["git", "show-ref", "--verify", "--quiet", f"refs/remotes/{candidate}"],
                cwd=ROOT,
                check=False,
            ).returncode
            == 0
        ),
        None,
    )
    if branch_ref is None:
        raise RuntimeError(
            f"Local Git branch is unavailable: {branch}. Fetch it once with "
            f"'git fetch origin {branch}' before packaging."
        )

    with tempfile.TemporaryDirectory() as temporary:
        source = Path(temporary) / "source"
        source.mkdir()
        result = subprocess.run(
            ["git", "archive", "--format=tar", branch_ref],
            cwd=ROOT,
            check=False,
            stdout=subprocess.PIPE,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Git branch not found or could not be archived: {branch}")
        with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as tar:
            tar.extractall(source)
        zip_pack(destination, source)


def package_locally(args: argparse.Namespace) -> None:
    destination = Path(args.output) if args.output else ROOT / f"{args.filename}.zip"
    if not destination.is_absolute():
        destination = ROOT / destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    zip_pack(destination)
    print(f"Created {destination}")

    extra_packages = (
        ("main-extra/pure-glass", "lRP-C.E.-Pure-Glass.zip"),
        ("main-extra/CRI", "lRP-C.E.-CRI.zip"),
        ("main-extra/april-fool", "lRP-C.E.-April-Fool.zip"),
    )
    for branch, filename in extra_packages:
        extra_destination = destination.parent / filename
        archive_branch(branch, extra_destination)
        print(f"Created {extra_destination}")

    all_destination = Path(args.all_output) if args.all_output else destination.parent / "lRP-Main-All.zip"
    if not all_destination.is_absolute():
        all_destination = ROOT / all_destination
    build_all_archive(destination, all_destination, destination.parent)
    print(f"Created {all_destination}")


def get_release_body(tag: str) -> str:
    return run("gh", "api", f"repos/{os.environ['GITHUB_REPOSITORY']}/releases/tags/{tag}", "-q", ".body", capture=True)


def find_previous_commit(label: str, current_tag: str) -> tuple[str, str]:
    releases = json.loads(
        run(
            "gh",
            "release",
            "list",
            "--limit",
            "100",
            "--json",
            "tagName,publishedAt",
            capture=True,
        )
    )
    anchor = f"<!-- {label.replace(' ', '_')}_ANCHOR -->"
    for release in sorted(releases, key=lambda item: item.get("publishedAt") or "", reverse=True):
        tag = release["tagName"]
        if tag == current_tag or not tag.startswith(RELEASE_PREFIX):
            continue
        body = get_release_body(tag)
        if anchor in body:
            section = body[body.find(anchor) :]
        else:
            branch_heading = f"### Branch: {label}"
            section = body[body.find(branch_heading) :] if branch_heading in body else ""
        if "---" in section:
            section = section.split("---", 1)[0]
        marker = "- **Last Commit**:"
        for line in section.splitlines():
            if marker in line:
                commit = line.split(marker, 1)[1].strip()
                if commit:
                    return commit, tag
    return "", ""


def changelog(previous_commit: str, previous_release: str) -> tuple[str, str]:
    if previous_commit:
        commits = run("git", "log", "--pretty=format:%h - %s", f"{previous_commit}..HEAD", capture=True)
        total = int(run("git", "rev-list", "--count", f"{previous_commit}..HEAD", capture=True) or "0")
        if not commits:
            return f"No new changes since previous Nightly ({previous_release}).", previous_release
        lines = commits.splitlines()[:50]
        if total > 50:
            lines.append(f"... and {total - 50} more changes.")
        return "\n".join(lines), previous_release

    commits = run("git", "log", "--since=1 month ago", "--pretty=format:%h - %s", capture=True)
    total = int(run("git", "rev-list", "--count", "--since=1 month ago", "HEAD", capture=True) or "0")
    if not commits:
        return "No changes in the last month.", ""
    lines = commits.splitlines()[:50]
    if total > 50:
        lines.append(f"... and {total - 50} more changes.")
    return "\n".join(lines), ""


def update_release(args: argparse.Namespace) -> None:
    now = datetime.now(timezone.utc)
    tag = release_tag(os.environ["GITHUB_RUN_NUMBER"], now)
    zip_path = ROOT / f"{args.filename}.zip"
    commit = run("git", "rev-parse", "--short", "HEAD", capture=True)
    timestamp = now.strftime("%Y-%m-%d-%H%M%S-%f")[:-3]

    try:
        zip_pack(zip_path)
        try:
            run("gh", "release", "view", tag)
        except subprocess.CalledProcessError:
            run("gh", "release", "create", tag, "--title", f"Nightly Build #{os.environ['GITHUB_RUN_NUMBER']} ({timestamp})", "--notes", "Initial Nightly Build", "--prerelease")

        run("gh", "release", "upload", tag, str(zip_path), "--clobber")
        body = get_release_body(tag)
        previous_commit, previous_release = find_previous_commit(args.label, tag)
        log, since = changelog(previous_commit, previous_release)
        anchor = f"<!-- {args.label.replace(' ', '_')}_ANCHOR -->"
        section = [
            "",
            anchor,
            f"### Branch: {args.label}",
            f"- **Last Build**: {timestamp}",
            f"- **Last Commit**: {commit}",
        ]
        if since:
            section.append(f"- **Since**: {since}")
        section += ["- **Changelog:**", *[f"  {line}" for line in log.splitlines()], "", "---", ""]
        if not previous_commit:
            section.insert(-2, "  - **Note**: Showing changes from the last 1 month because no previous Nightly release (for this branch) was found.")
        run("gh", "release", "edit", tag, "--notes", body + "\n".join(section), "--prerelease")
    finally:
        zip_path.unlink(missing_ok=True)


def build_all_archive(main_zip: Path, output: Path, asset_directory: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        unpacked = Path(temporary) / "unpacked"
        with zipfile.ZipFile(main_zip) as archive:
            archive.extractall(unpacked)

        extra = unpacked / "extra"
        extra.mkdir()
        for path in asset_directory.glob("*.zip"):
            if path.name != main_zip.name and "C.E." in path.name:
                shutil.copy2(path, extra / path.name)

        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in unpacked.rglob("*"):
                if path.is_file():
                    archive.write(path, path.relative_to(unpacked).as_posix())


def aggregate() -> None:
    now = datetime.now(timezone.utc)
    tag = release_tag(os.environ["GITHUB_RUN_NUMBER"], now)
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)
        run("gh", "release", "download", tag, "--pattern", "*.zip", "--dir", str(work))
        main_zip = work / "lRP-Main.zip"
        if not main_zip.exists():
            raise RuntimeError("lRP-Main.zip was not found in the release assets")
        output = work / "lRP-Main-All.zip"
        build_all_archive(main_zip, output, work)
        run("gh", "release", "upload", tag, str(output), "--clobber")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    package = subparsers.add_parser("package", help="Create a local resource-pack ZIP")
    package.add_argument("--filename", default="lRP-Main", help="ZIP filename without the .zip suffix")
    package.add_argument("--output", help="Output ZIP path; defaults to <repository>/<filename>.zip")
    package.add_argument("--all-output", help="Aggregate ZIP path; defaults to <output-directory>/lRP-Main-All.zip")
    release = subparsers.add_parser("release")
    release.add_argument("--label", required=True)
    release.add_argument("--filename", required=True)
    subparsers.add_parser("aggregate")
    args = parser.parse_args()
    if args.command == "package":
        package_locally(args)
    elif args.command == "release":
        update_release(args)
    else:
        aggregate()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (subprocess.CalledProcessError, RuntimeError) as error:
        print(f"FAILED: {error}", file=sys.stderr)
        raise SystemExit(1)
