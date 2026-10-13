"""Check source transparency and the icon bytes in the actual release BDA/ZIP."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys
from zipfile import ZipFile

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sdk"))
from h1_bda.header import decode_header
from h1_bda.resources import build_icon_resources
from h1_bda.validate import validate_bda


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def report_path(path: Path) -> str:
    """Keep published evidence independent of the developer's home directory."""
    resolved = path.resolve()
    return resolved.relative_to(ROOT).as_posix() if resolved.is_relative_to(ROOT) else path.name


def corner_indices(width: int, height: int) -> tuple[int, ...]:
    return (0, width - 1, width * (height - 1), width * height - 1)


def check(icon: Path, bda: Path, archive_path: Path | None) -> dict:
    with Image.open(icon) as source:
        require("A" in source.getbands(), "Source PNG has no alpha channel")
        alpha = source.getchannel("A")
        histogram = alpha.histogram()
        corners = [alpha.getpixel((index % source.width, index // source.width))
                   for index in corner_indices(source.width, source.height)]
        require(corners == [0, 0, 0, 0], f"Source PNG corners are opaque: {corners}")
        require(alpha.getextrema() == (0, 255), "Source needs transparent and opaque pixels")
        source_report = {"file": report_path(icon), "mode": source.mode,
                         "size": list(source.size), "corner_alpha": corners,
                         "transparent_pixels": histogram[0],
                         "opaque_pixels": histogram[255], "sha256": sha256(icon.read_bytes())}

    data = bda.read_bytes()
    validation = validate_bda(bda)
    require(validation["ok"], f"Invalid BDA: {validation}")
    words = struct.unpack_from("<11I", decode_header(data))
    payload_offset, offset = words[5:7]
    require(data[offset:payload_offset] == build_icon_resources(icon),
            "Release BDA still contains an old or mismatched icon")
    resources = []
    for index, length in enumerate(words[7:11]):
        width, height, bits, planes, draw_width, draw_height = struct.unpack_from("<6H", data, offset)
        require(bits in (16, 24) and planes == 1 and (width, height) == (draw_width, draw_height),
                f"Resource {index} has an unsupported pixel layout")
        pixels = data[offset + 12:offset + 12 + width * height * (bits // 8)]
        entry = {"index": index, "size": [width, height], "bits": bits,
                 "supports_alpha": bits == 24}
        if bits == 24:
            # The firmware's '24 bit' format is little-endian RGB565 followed by alpha.
            alpha_bytes = pixels[2::3]
            require(len(alpha_bytes) == width * height, f"Resource {index} is truncated")
            corner_alpha = [alpha_bytes[pixel] for pixel in corner_indices(width, height)]
            require(corner_alpha == [0, 0, 0, 0],
                    f"Resource {index} has opaque corner pixels: {corner_alpha}")
            require(min(alpha_bytes) == 0 and max(alpha_bytes) == 255,
                    f"Resource {index} lost transparent or opaque artwork")
            entry.update(corner_alpha=corner_alpha, transparent_pixels=alpha_bytes.count(0),
                         opaque_pixels=alpha_bytes.count(255))
        else:
            entry["note"] = "RGB565 has no alpha; SDK composites source transparency on black"
        resources.append(entry)
        offset += length

    metadata = json.loads(bda.with_suffix(".build.json").read_text(encoding="utf-8"))
    digest = sha256(data)
    require(metadata["sha256"] == digest, "BDA metadata hash is stale")
    require(metadata["icon_sha256"] == source_report["sha256"], "Source icon metadata hash is stale")
    require(metadata["test_frames"] == 0 and not metadata["benchmark"],
            "Expected the unrestricted release BDA, not a test or benchmark build")
    require(bda.with_suffix(".bda.sha256").read_text().split() == [digest, bda.name],
            "BDA checksum file is stale")
    report = {"ok": True, "source": source_report,
              "bda": {"file": report_path(bda), "sha256": digest, "bytes": len(data)},
              "resources": resources}
    if archive_path:
        with ZipFile(archive_path) as archive:
            require(archive.testzip() is None, "Install ZIP integrity check failed")
            require(archive.read("应用/程序/H1FC.bda") == data, "Install ZIP contains an outdated BDA")
            require(json.loads(archive.read("H1FC.build.json")) == metadata,
                    "Install ZIP contains outdated build metadata")
            require(archive.read("H1FC.bda.sha256").decode("ascii").split() == [digest, "H1FC.bda"],
                    "Install ZIP contains an outdated BDA checksum")
        archive_digest = sha256(archive_path.read_bytes())
        require(archive_path.with_suffix(".zip.sha256").read_text().split() ==
                [archive_digest, archive_path.name], "Install ZIP checksum file is stale")
        report["install_zip"] = {"file": report_path(archive_path), "sha256": archive_digest}
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--icon", type=Path, default=ROOT / "assets/fc-icon.png")
    parser.add_argument("--bda", type=Path, default=ROOT / "dist/H1FC.bda")
    parser.add_argument("--zip", dest="archive", type=Path, help="Also verify the install ZIP")
    parser.add_argument("--output", type=Path, help="Save the verification evidence as JSON")
    args = parser.parse_args()
    report = check(args.icon, args.bda, args.archive)
    rendered = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
