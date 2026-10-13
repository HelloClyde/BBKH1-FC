"""Package the release BDA, original diagnostic ROM, instructions and licenses."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from make_test_rom import make_rom


def main():
    release = ROOT / "dist/H1FC.bda"
    metadata = json.loads(release.with_suffix(".build.json").read_text())
    data = release.read_bytes()
    assert metadata["sha256"] == hashlib.sha256(data).hexdigest()
    assert metadata["test_frames"] == 0 and not metadata["benchmark"]
    assert metadata["version"] == (ROOT / "VERSION").read_text(encoding="ascii").strip()
    files = {
        "应用/程序/H1FC.bda": data,
        "H1FC.bda.sha256": release.with_suffix(".bda.sha256").read_bytes(),
        "H1FC.build.json": release.with_suffix(".build.json").read_bytes(),
        "FC/H1FC-TEST.nes": make_rom(),
        "README.md": (ROOT / "README.md").read_bytes(),
        "NOTICE.md": (ROOT / "NOTICE.md").read_bytes(),
        "assets/README.md": (ROOT / "assets/README.md").read_bytes(),
        "LICENSES/H1FC-GPL-2.0.txt": (ROOT / "LICENSE").read_bytes(),
        "LICENSES/FCEUmm-Copying.txt": (ROOT / "third_party/fceumm/Copying").read_bytes(),
        "LICENSES/FCEUmm-Authors.txt": (ROOT / "third_party/fceumm/Authors").read_bytes(),
        "LICENSES/H1SDK-Apache-2.0.txt": (ROOT / "sdk/LICENSE").read_bytes(),
        "LICENSES/H1SDK-NOTICE.txt": (ROOT / "sdk/NOTICE").read_bytes(),
    }
    for path in sorted((ROOT / "assets/licenses").glob("*")):
        if path.is_file():
            files[path.relative_to(ROOT).as_posix()] = path.read_bytes()
    for path in sorted((ROOT / "docs").rglob("*")):
        if path.is_file():
            files[path.relative_to(ROOT).as_posix()] = path.read_bytes()
    output = ROOT / "dist/H1FC-install.zip"
    with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for name, payload in sorted(files.items()):
            entry = ZipInfo(name, (2026, 10, 13, 0, 0, 0))
            entry.compress_type = ZIP_DEFLATED
            entry.external_attr = 0o644 << 16
            archive.writestr(entry, payload)
    with ZipFile(output) as archive:
        assert archive.testzip() is None
        assert hashlib.sha256(archive.read("应用/程序/H1FC.bda")).hexdigest() == metadata["sha256"]
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    checksum = output.with_suffix(".zip.sha256")
    checksum.write_text(f"{digest}  {output.name}\n", encoding="ascii")
    print(f"PASS: {output}\nSHA256 {digest}")


if __name__ == "__main__":
    main()
