"""Check release identity and optional exact-binary firmware evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
DEPENDENCIES = {
    "sdk": "067fe072477861dfc8949d7b1a55279fb92d2548",
    "third_party/fceumm": "ac0d00a7fc01166f84c7bac01d1084ebfd2b865e",
}
REQUIRED_FIRMWARE_CHECKS = (
    "launch", "about_author", "about_return", "cancel_to_desktop", "clean_exit",
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def version() -> str:
    value = (ROOT / "VERSION").read_text(encoding="ascii").strip()
    require(re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", value) is not None,
            "VERSION must be MAJOR.MINOR.PATCH")
    return value


def check_tag(tag: str) -> None:
    require(tag == "v" + version(), "Release tag must exactly match v + VERSION")


def evidence_file(name: str) -> Path:
    relative = Path(name)
    require(not relative.is_absolute() and ".." not in relative.parts and "\\" not in name,
            "Evidence paths must be repository-relative POSIX paths")
    target = ROOT / relative
    require(target.resolve().is_relative_to(ROOT.resolve()), "Evidence path escapes repository")
    require(name.startswith("docs/evidence/"), "Keep release evidence in docs/evidence/")
    return target


def verify_evidence(metadata: dict) -> dict:
    record = json.loads((ROOT / f"docs/evidence/release-v{version()}.json").read_text(encoding="utf-8"))
    require(record.get("version") == version(), "Firmware evidence version is stale")
    for record_key, metadata_key in (("bda_sha256", "sha256"), ("payload_sha256", "payload_sha256"),
                                     ("sdk_commit", "sdk_commit"), ("fceumm_commit", "fceumm_commit")):
        require(record.get(record_key) == metadata[metadata_key], "Firmware evidence mismatch: " + record_key)
    require(record.get("full_firmware_tested") is True, "Release needs complete H1 firmware validation")
    require(record.get("hardware_tested") is False, "This prerelease records emulator-only validation")
    checks = record.get("checks", {})
    require(all(checks.get(name) is True for name in REQUIRED_FIRMWARE_CHECKS),
            "Required firmware checks are missing or failed")
    require(checks and all(value is True for value in checks.values()), "A firmware check did not pass")
    for key in ("screenshots", "evidence_sha256"):
        files = record.get(key, {})
        require(bool(files), "Missing release evidence: " + key)
        for name, digest in files.items():
            require(sha256(evidence_file(name)) == digest, "Evidence hash mismatch: " + name)
    return record


def verify(require_verified: bool = False) -> tuple[dict, dict | None]:
    metadata = json.loads((ROOT / "dist/H1FC.build.json").read_text(encoding="utf-8"))
    require(metadata["version"] == version(), "Build version does not match VERSION")
    require(metadata["sha256"] == sha256(ROOT / "dist/H1FC.bda"), "BDA hash differs from metadata")
    require(metadata["size"] == (ROOT / "dist/H1FC.bda").stat().st_size, "BDA size differs from metadata")
    require(metadata["test_frames"] == 0 and not metadata["benchmark"] and
            not metadata["verbose_log"] and not metadata["profile"], "Expected an unrestricted release build")
    require(metadata["compiler"].endswith("15.2.0"), "Expected the pinned GNU MIPS GCC 15.2.0")
    require(metadata["sdk_commit"] == DEPENDENCIES["sdk"], "SDK commit differs from the validated revision")
    require(metadata["fceumm_commit"] == DEPENDENCIES["third_party/fceumm"], "FCEUmm commit differs from validated revision")
    sys.path.insert(0, str(ROOT / "sdk"))
    from h1_bda.resources import PAYLOAD_OFFSET
    payload = (ROOT / "dist/H1FC.bda").read_bytes()[PAYLOAD_OFFSET:]
    require(metadata["payload_sha256"] == hashlib.sha256(payload).hexdigest(), "MIPS payload hash mismatch")
    subprocess.run([sys.executable, str(ROOT / "tests/check_icon.py"),
                    "--zip", str(ROOT / "dist/H1FC-install.zip")], check=True)
    evidence = verify_evidence(metadata) if require_verified else None
    print("PASS release identity" + (" and exact-BDA firmware evidence" if evidence else " (firmware evidence not required)"))
    return metadata, evidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-verified", action="store_true")
    parser.add_argument("--check-tag", help="Only check the version tag; no build required")
    args = parser.parse_args()
    if args.check_tag is not None:
        check_tag(args.check_tag)
        print("PASS tag matches VERSION")
    else:
        verify(args.require_verified)


if __name__ == "__main__":
    main()
