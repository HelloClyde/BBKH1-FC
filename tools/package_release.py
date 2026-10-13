"""Package final assets and complete pinned source; verify downloaded assets."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import zipfile

from verify_release import DEPENDENCIES, ROOT, check_tag, require, sha256, verify as verify_build, version

INSTALL_BDA = "应用/程序/H1FC.bda"
FORBIDDEN_SUFFIXES = {
    ".nes", ".fc", ".fds", ".rom", ".gba", ".gb", ".gbc", ".bda", ".raw",
    ".bin", ".elf", ".o", ".exe", ".dll", ".sav", ".s0", ".s1", ".img", ".iso",
    ".zip", ".7z", ".rar", ".pyc",
}
FORBIDDEN_PARTS = {".git", ".tools", "__pycache__", "node_modules"}


def git(repository: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(repository), *args])


def tracked(repository: Path):
    for entry in git(repository, "ls-tree", "-rz", "--full-tree", "HEAD").split(b"\0"):
        if entry:
            info, encoded = entry.split(b"\t", 1)
            mode, kind, oid = info.decode("ascii").split()
            yield mode, kind, oid, encoded.decode("utf-8")


def safe_name(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(name) and not path.is_absolute() and ".." not in path.parts and "\\" not in name and ":" not in name


def source_allowed(name: str) -> None:
    path = PurePosixPath(name)
    require(safe_name(name), "Unsafe source path: " + name)
    require(not any(part in FORBIDDEN_PARTS for part in path.parts), "Private/cache path in source: " + name)
    require(path.parts[0] not in ("build", "dist"), "Generated output in source: " + name)
    require(path.suffix.lower() not in FORBIDDEN_SUFFIXES, "ROM, firmware or binary in source: " + name)


def add(archive: zipfile.ZipFile, name: str, data: bytes, mode: str = "100644") -> None:
    entry = zipfile.ZipInfo(name, (2026, 10, 13, 0, 0, 0))
    entry.compress_type = zipfile.ZIP_DEFLATED
    entry.create_system = 3
    entry.external_attr = int(mode, 8) << 16
    archive.writestr(entry, data)


def make_source(destination: Path) -> dict:
    require(git(ROOT, "rev-parse", "--show-toplevel").decode().strip().replace("\\", "/").lower() ==
            ROOT.as_posix().lower(), "Package from the independent application repository")
    require(not git(ROOT, "status", "--porcelain").strip(), "Package a clean, committed application tree")
    manifest = {"application_commit": git(ROOT, "rev-parse", "HEAD").decode().strip(),
                "dependencies": DEPENDENCIES, "files": {}}
    for relative, expected in DEPENDENCIES.items():
        repository = ROOT / relative
        require(git(ROOT, "rev-parse", "HEAD:" + relative).decode().strip() == expected,
                "Committed submodule gitlink differs: " + relative)
        require(git(repository, "rev-parse", "HEAD").decode().strip() == expected,
                "Dependency checkout differs: " + relative)
        require(not git(repository, "status", "--porcelain").strip(), "Modified dependency: " + relative)
    prefix = f"BBKH1-FC-v{version()}/"
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative in ("", *DEPENDENCIES):
            repository = ROOT / relative
            for mode, kind, oid, name in sorted(tracked(repository), key=lambda entry: entry[3]):
                target = (relative + "/" if relative else "") + name
                if mode == "160000":
                    require(not relative and target in DEPENDENCIES, "Unexpected nested dependency: " + target)
                    continue
                require(kind == "blob" and mode in ("100644", "100755"), "Unsupported source entry: " + target)
                source_allowed(target)
                data = git(repository, "cat-file", "blob", oid)
                manifest["files"][target] = hashlib.sha256(data).hexdigest()
                add(archive, prefix + target, data, mode)
        add(archive, prefix + "source-manifest.json", (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())
    return manifest


def verify_source(archive: zipfile.ZipFile, expected_commit: str, expected_version: str) -> None:
    prefix = f"BBKH1-FC-v{expected_version}/"
    manifest_name = prefix + "source-manifest.json"
    manifest = json.loads(archive.read(manifest_name))
    require(manifest["application_commit"] == expected_commit, "Source application commit mismatch")
    require(manifest["dependencies"] == DEPENDENCIES, "Source dependency revisions mismatch")
    files = manifest["files"]
    require(set(archive.namelist()) == {prefix + name for name in files} | {manifest_name},
            "Source archive differs from the complete source manifest")
    for name, digest in files.items():
        source_allowed(name)
        require(hashlib.sha256(archive.read(prefix + name)).hexdigest() == digest, "Source hash mismatch: " + name)
    for name in ("VERSION", "LICENSE", "src/app.c", "sdk/LICENSE", "sdk/h1_bda/build.py",
                 "third_party/fceumm/Copying", "third_party/fceumm/src/x6502.c"):
        require(name in files, "Missing corresponding source: " + name)
    require(archive.read(prefix + "VERSION").decode().strip() == expected_version, "Source version mismatch")


def collect_mips_evidence(metadata: dict) -> dict:
    reports = {}
    for name, hash_key in (("menu-smoke.json", "bda_sha256"), ("pause-menu-smoke.json", "sha256")):
        report = json.loads((ROOT / "build/verification" / name).read_text(encoding="utf-8"))
        require(report.get("ok") is True and report.get(hash_key) == metadata["sha256"],
                "Missing, stale or failed MIPS evidence: " + name)
        reports[name] = report
    require(reports["menu-smoke.json"].get("menu") is True, "Smoke evidence must execute the release build")
    require(reports["pause-menu-smoke.json"].get("state_slots_restored") == 3,
            "Pause evidence must restore all three slots")
    return reports


def verify_assets(output: Path, tag: str | None = None, expected_commit: str | None = None) -> None:
    if tag is not None:
        check_tag(tag)
    record = json.loads((output / "release-manifest.json").read_text(encoding="utf-8"))
    release_version = version()
    require(record["version"] == release_version, "Release version mismatch")
    require(record["dependencies"] == DEPENDENCIES, "Release dependency mismatch")
    require(record["hardware_tested"] is False and record["full_firmware_in_ci"] is False,
            "Incorrect validation scope")
    if expected_commit:
        require(record["application_commit"] == expected_commit, "Release does not belong to this commit")
    if tag:
        require(record["firmware_evidence_verified"] is True and record["runtime_evidence"],
                "Tagged release has no exact-BDA firmware evidence")
    expected = {"H1FC.bda", "H1FC.build.json", "RELEASE-NOTES.md", "release-manifest.json",
                f"BBKH1-FC-v{release_version}-install.zip", f"BBKH1-FC-v{release_version}-source.zip"}
    entries = {}
    for line in (output / "SHA256SUMS.txt").read_text(encoding="ascii").splitlines():
        digest, name = line.split("  ", 1)
        require(safe_name(name) and PurePosixPath(name).name == name and name not in entries,
                "Invalid or duplicate checksum filename")
        require(len(digest) == 64 and all(char in "0123456789abcdef" for char in digest), "Invalid SHA256 digest")
        entries[name] = digest
        require(sha256(output / name) == digest, "Release asset hash mismatch: " + name)
    require(set(entries) == expected, "Release asset manifest has unexpected or missing files")
    require({path.name for path in output.iterdir()} == expected | {"SHA256SUMS.txt"}, "Unexpected release output file")
    metadata = json.loads((output / "H1FC.build.json").read_text(encoding="utf-8"))
    require(metadata == record["build"], "Release metadata mismatch")
    require(metadata["sha256"] == entries["H1FC.bda"] and metadata["version"] == release_version,
            "Release BDA identity mismatch")
    require(not metadata["test_frames"] and not metadata["benchmark"] and not metadata["verbose_log"] and
            not metadata["profile"], "Release contains a diagnostic build")
    if record["firmware_evidence_verified"]:
        require(record["runtime_evidence"]["bda_sha256"] == entries["H1FC.bda"], "Runtime evidence BDA mismatch")
    for name, hash_key in (("menu-smoke.json", "bda_sha256"), ("pause-menu-smoke.json", "sha256")):
        report = record["mips_evidence"][name]
        require(report["ok"] is True and report[hash_key] == metadata["sha256"], "Release MIPS evidence mismatch")
    for name in sorted(expected):
        if not name.endswith(".zip"):
            continue
        with zipfile.ZipFile(output / name) as archive:
            require(archive.testzip() is None, "Damaged ZIP: " + name)
            require(len(archive.namelist()) == len(set(archive.namelist())), "Duplicate ZIP entries")
            require(all(safe_name(item) for item in archive.namelist()), "Unsafe ZIP entry")
            if name.endswith("-source.zip"):
                verify_source(archive, record["application_commit"], release_version)
            else:
                require(archive.read(INSTALL_BDA) == (output / "H1FC.bda").read_bytes(), "Installer BDA mismatch")
                require(json.loads(archive.read("H1FC.build.json")) == metadata, "Installer metadata mismatch")
    print("PASS exact release files, install layout, complete source and SHA256SUMS.txt")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", help="Only verify previously packaged/downloaded assets")
    parser.add_argument("--tag", help="Require exact version tag and locally recorded firmware evidence")
    parser.add_argument("--require-verified", action="store_true", help="Require local firmware evidence even without a tag")
    parser.add_argument("--commit", help="Expected application commit when verifying downloaded assets")
    args = parser.parse_args()
    output = ROOT / "dist/release"
    if args.verify:
        verify_assets(output, args.tag, args.commit)
        return
    if args.tag:
        check_tag(args.tag)
    metadata, evidence = verify_build(args.require_verified or bool(args.tag))
    output.mkdir(parents=True, exist_ok=True)
    expected_names = {"H1FC.bda", "H1FC.build.json", "RELEASE-NOTES.md", "release-manifest.json", "SHA256SUMS.txt",
                      f"BBKH1-FC-v{version()}-install.zip", f"BBKH1-FC-v{version()}-source.zip"}
    require(all(path.name in expected_names for path in output.iterdir()),
            "Release output contains another version or unexpected files; use a fresh checkout")
    manifest = make_source(output / f"BBKH1-FC-v{version()}-source.zip")
    for name in ("H1FC.bda", "H1FC.build.json"):
        shutil.copyfile(ROOT / "dist" / name, output / name)
    shutil.copyfile(ROOT / "dist/H1FC-install.zip", output / f"BBKH1-FC-v{version()}-install.zip")
    shutil.copyfile(ROOT / f"docs/release-v{version()}.md", output / "RELEASE-NOTES.md")
    record = {"version": version(), "application_commit": manifest["application_commit"],
              "dependencies": DEPENDENCIES, "build": metadata,
              "firmware_evidence_verified": evidence is not None, "runtime_evidence": evidence,
              "mips_evidence": collect_mips_evidence(metadata),
              "ci_runtime": "Actual MIPS release binary with mocked H1 services; no firmware ROM in CI",
              "full_firmware_in_ci": False, "hardware_tested": False}
    (output / "release-manifest.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "SHA256SUMS.txt").write_text("".join(
        sha256(output / name) + "  " + name + "\n" for name in sorted(expected_names - {"SHA256SUMS.txt"})), encoding="ascii")
    verify_assets(output, args.tag, manifest["application_commit"])


if __name__ == "__main__":
    main()
