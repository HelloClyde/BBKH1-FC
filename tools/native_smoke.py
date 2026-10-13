"""Exercise H1 FC in a private full-firmware NAND, never on a physical device.

Requires explicitly supplied local firmware, kernel and emulator support tools.
These private inputs are not included in this source repository or CI.
The first run builds a private NAND; --rebuild-image explicitly refreshes it.
Only the new frontend/process owned by this invocation is stopped at the end.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import zlib

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "sdk/scripts"))
from make_test_rom import make_rom
from navigate_emulator_game_slot import api, capture, press_key, tap


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def collect_evidence(work: Path, package: Path, nand_reader: Path) -> dict:
    """Read the stopped private NAND and publish auditable test evidence."""
    spec = importlib.util.spec_from_file_location("h1_nand_reader", nand_reader)
    reader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reader)
    nand = reader.H1Nand(work / "h1-system.raw")
    try:
        fat = reader.Fat16(nand)
        log = fat.file("FC/h1fc.log")
        slots = []
        for index in (0, 1):
            try:
                slots.append(fat.file(f"FC/H1TEST.nes.s{index}"))
            except FileNotFoundError:
                pass
        torn = nand.torn
    finally:
        nand.stream.close()
    assert slots, "No persisted SRAM slot in the private NAND"
    slot = max(slots, key=lambda value: int.from_bytes(value[12:16], "little"))
    assert len(slot) == 28 + 8192
    assert zlib.crc32(slot[:24]) == int.from_bytes(slot[24:28], "little"), "Header CRC"
    assert zlib.crc32(slot[28:]) == int.from_bytes(slot[20:24], "little"), "SRAM CRC"
    assert slot[28:30] == b"\x5a\x11" and slot[31:33] == b"\x42\x1e", "Diagnostic SRAM"
    text = log.decode("utf-8", errors="replace")
    stops = re.findall(r"STOP video=(\d+) errors=(\d+)", text)
    assert stops and int(stops[-1][0]) > 0 and int(stops[-1][1]) == 0
    assert "APP_END result=0" in text
    audio_lines = re.findall(r"AUDIO_CLOSE_BEGIN ([^\r\n]+)", text)
    app_audio = {name: int(value) for name, value in re.findall(r"(\w+)=(\d+)", audio_lines[-1])}
    out = work / "evidence"
    out.mkdir(exist_ok=True)
    (out / "h1fc.log").write_bytes(log)
    (out / "H1TEST.nes.s0").write_bytes(slot)
    interaction = json.loads((out / "input-audio-status.json").read_text(encoding="utf-8"))
    stopped = json.loads((out / "stopped-status.json").read_text(encoding="utf-8-sig"))
    assert stopped["running"] is False and stopped["returncode"] == 0
    image_build = json.loads((work / "image-build.json").read_text(encoding="utf-8"))
    assert image_build["bda_sha256"] == sha(package), "NAND contains a different BDA"
    names = ["menu", "picker", "gameplay", "a-pressed", "a-released", "returned-picker", "exit-desktop"]
    destination = ROOT / "docs/evidence"
    destination.mkdir(parents=True, exist_ok=True)
    screenshots = {}
    for name in names:
        source = out / (name + ".png")
        target = destination / ("native-" + name + ".png")
        target.write_bytes(source.read_bytes())
        screenshots[target.name] = sha(target)
    (destination / "native-run.log").write_bytes(log)
    report = {
        "ok": True, "environment": "full H1 V1.41 firmware in QEMU bbkh1, 64 MiB",
        "physical_device_tested": False, "original_dump_modified": False,
        "bda_sha256": sha(package), "kernel_sha256": image_build["kernel_sha256"],
        "rom": "original redistributable H1TEST.nes, NROM mapper 0, 24,592 bytes",
        "rom_sha256": sha(work / "files/FC/H1TEST.nes"),
        "a_pressed_rgb": interaction["a_pressed_rgb"],
        "a_released_rgb": interaction["a_released_rgb"],
        "audio": interaction["status"]["audio"],
        "app_audio_at_close": app_audio,
        "video_frames": int(stops[-1][0]), "video_errors": int(stops[-1][1]),
        "sram": {"slot_bytes": len(slot), "payload_bytes": 8192, "header_crc_ok": True,
                 "payload_crc_ok": True, "first_six_bytes": slot[28:34].hex()},
        "ftl_torn_records": torn, "app_end_result": 0, "qemu_returncode": stopped["returncode"],
        "screenshots": screenshots,
        "limitations": ["QEMU results do not establish physical-device speed or long-term stability",
                        "Audio output was observed; underrun counters are retained, not treated as zero"],
    }
    (destination / "native-smoke.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dump", type=Path, help="User-owned H1 firmware dump directory")
    parser.add_argument("--kernel", type=Path, help="Local H1 project.bin firmware kernel")
    parser.add_argument("--qemu", type=Path, help="QEMU executable with bbkh1 machine support")
    parser.add_argument("--dll-dir", type=Path, help="Optional Windows QEMU DLL directory")
    parser.add_argument("--emulator", type=Path, help="Path to h1_emulator.py frontend")
    parser.add_argument("--image-builder", type=Path, help="Path to build_h1_test_image.py support script")
    parser.add_argument("--nand-reader", type=Path, required=True, help="Path to read_h1_test_file.py support script")
    parser.add_argument("--work-dir", type=Path, default=ROOT / "build/h1-firmware")
    parser.add_argument("--port", type=int, default=8813)
    parser.add_argument("--gdb-port", type=int, default=8814)
    parser.add_argument("--rebuild-image", action="store_true")
    parser.add_argument("--collect-only", action="store_true", help="Collect saved evidence after a stopped run")
    args = parser.parse_args()
    work, package = args.work_dir.resolve(), ROOT / "dist/H1FC.bda"
    if not work.is_relative_to((ROOT / "build").resolve()):
        parser.error("--work-dir must stay inside h1-fc/build")
    if args.collect_only:
        print(json.dumps(collect_evidence(work, package, args.nand_reader), indent=2))
        return
    for field in ("dump", "kernel", "qemu", "emulator", "image_builder"):
        if getattr(args, field) is None:
            parser.error("--" + field.replace("_", "-") + " is required unless --collect-only")
    if args.port in (8793, 8794) or args.gdb_port in (8793, 8794) or args.port == args.gdb_port:
        parser.error("Use distinct private ports; the existing 8793/8794 frontend is excluded")
    for port in (args.port, args.gdb_port):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
    work.mkdir(parents=True, exist_ok=True)
    out = work / "evidence"
    out.mkdir(exist_ok=True)
    if args.rebuild_image or not (work / "h1-system.raw").is_file():
        rom = work / "files/FC/H1TEST.nes"
        rom.parent.mkdir(parents=True, exist_ok=True)
        rom.write_bytes(make_rom())
        subprocess.run([sys.executable, str(args.image_builder),
                        "--reuse-stage", "--work-dir", str(work), "--dump", str(args.dump),
                        "--kernel", str(args.kernel), "--bda", str(package)], check=True)
    built = json.loads((work / "image-build.json").read_text(encoding="utf-8"))
    if built["bda_sha256"] != sha(package):
        parser.error("Private image contains another BDA; use --rebuild-image")
    server = f"http://127.0.0.1:{args.port}"
    command = [sys.executable, "-u", str(args.emulator),
               "--qemu", str(args.qemu), "--kernel", str(work / "project.bin"),
               "--nand", str(work / "h1-system.raw"),
               "--port", str(args.port), "--gdb-port", str(args.gdb_port), "--no-browser", "--writable"]
    if args.dll_dir:
        command += ["--dll-dir", str(args.dll_dir)]
    owned_server = False
    with (work / "frontend.stdout.log").open("wb") as stdout, (work / "frontend.stderr.log").open("wb") as stderr:
        frontend = subprocess.Popen(command, stdout=stdout, stderr=stderr,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        (work / "frontend.pid").write_text(str(frontend.pid), encoding="ascii")
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if frontend.poll() is not None:
                    raise RuntimeError("Private frontend exited; inspect frontend.stderr.log")
                try:
                    status = api(server, "/api/status")
                    assert Path(status["nand"]).resolve() == work / "h1-system.raw"
                    owned_server = True
                    break
                except OSError:
                    time.sleep(0.2)
            if not owned_server:
                raise TimeoutError("Private frontend did not become ready")
            subprocess.run([sys.executable, str(ROOT / "sdk/scripts/navigate_emulator_game_slot.py"),
                            "--server", server, "--no-reset", "--capture", str(out / "menu.png")], check=True)
            tap(server, 42, 51)
            time.sleep(3)
            capture(server, out / "picker.png")
            press_key(server, 39)
            time.sleep(5)
            capture(server, out / "gameplay.png")
            api(server, "/api/key", {"code": 16, "down": True})
            time.sleep(1)
            capture(server, out / "a-pressed.png")
            api(server, "/api/key", {"code": 16, "down": False})
            time.sleep(1)
            capture(server, out / "a-released.png")
            pressed = Image.open(out / "a-pressed.png").convert("RGB").getpixel((108, 136))
            released = Image.open(out / "a-released.png").convert("RGB").getpixel((108, 136))
            assert min(pressed) > 200, ("A did not turn blue stripe white", pressed)
            assert released[2] > max(released[:2]), ("A release did not restore blue", released)
            status = api(server, "/api/status")
            assert status["audio"]["sample_rate"] == 32000 and status["audio"]["packets"] > 0
            (out / "input-audio-status.json").write_text(json.dumps(
                {"a_pressed_rgb": pressed, "a_released_rgb": released, "status": status}, indent=2), encoding="utf-8")
            press_key(server, 36)
            time.sleep(3)
            capture(server, out / "returned-picker.png")
            press_key(server, 24)
            time.sleep(3)
            capture(server, out / "exit-desktop.png")
        finally:
            try:
                if owned_server and frontend.poll() is None:
                    stopped = api(server, "/api/stop", {})
                    (out / "stopped-status.json").write_text(json.dumps(stopped, indent=2), encoding="utf-8")
            finally:
                if frontend.poll() is None:
                    frontend.terminate()
                    frontend.wait(timeout=10)
    report = collect_evidence(work, package, args.nand_reader)
    print(f"PASS: native firmware, {report['video_frames']} video frames, verified A and SRAM; private processes stopped")


if __name__ == "__main__":
    main()
