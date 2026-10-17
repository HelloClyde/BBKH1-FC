"""Run a user-provided NES game in an independent full H1 firmware image.

ROMs and captured audio remain under build/. This script never downloads ROMs,
publishes them, accesses a physical device, or touches the existing 8793 instance.
Inspect the named screenshots before reporting game-specific action success.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
import wave

from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sdk/scripts"))
from navigate_emulator_game_slot import api, capture, press_key, tap


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def record_audio(port, output, emulator, seconds=3):
    protocol = load_module("classic_audio_protocol", emulator)
    chunks, rate = [], None
    with socket.create_connection(("127.0.0.1", port), timeout=5) as stream:
        key = base64.b64encode(b"H1FCGameCapture!!").decode()
        stream.sendall((f"GET /ws?stream=audio HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                        "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                        f"Sec-WebSocket-Key: {key}\r\n\r\n").encode())
        header = b""
        while not header.endswith(b"\r\n\r\n"):
            block = stream.recv(1)
            if not block:
                raise RuntimeError("Audio websocket closed during handshake")
            header += block
        assert b" 101 " in header.split(b"\r\n")[0], header
        deadline = time.monotonic() + seconds
        stream.settimeout(1)
        while time.monotonic() < deadline:
            try:
                opcode, packet = protocol.read_ws_frame(stream)
            except socket.timeout:
                continue
            if opcode != 2:
                continue
            magic, _, packet_rate, channels, stride, fmt = struct.unpack_from("<4s5I", packet)
            assert magic == b"H1AU" and channels == 2 and stride == 4
            assert fmt == protocol.AUDIO_FORMAT_S16LE
            assert rate is None or rate == packet_rate
            rate = packet_rate
            chunks.append(packet[24:])
    assert chunks, "No audio packets received"
    pcm = b"".join(chunks)
    values = struct.unpack("<" + "h" * (len(pcm) // 2), pcm)
    with wave.open(str(output), "wb") as dest:
        dest.setnchannels(2)
        dest.setsampwidth(2)
        dest.setframerate(rate)
        dest.writeframes(pcm)
    result = {"sample_rate": rate, "packets": len(chunks), "frames": len(pcm) // 4,
              "seconds": len(pcm) / (4 * rate), "min": min(values), "max": max(values),
              "nonzero_samples": sum(v != 0 for v in values)}
    assert result["nonzero_samples"] > 0, "Captured audio was silent"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rom", type=Path, required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--bda", type=Path, default=ROOT / "dist/H1FC.bda")
    parser.add_argument("--dump", type=Path, required=True, help="User-owned H1 firmware dump directory")
    parser.add_argument("--kernel", type=Path, required=True, help="Local H1 project.bin firmware kernel")
    parser.add_argument("--qemu", type=Path, required=True, help="QEMU executable with bbkh1 machine support")
    parser.add_argument("--dll-dir", type=Path, help="Optional Windows QEMU DLL directory")
    parser.add_argument("--emulator", type=Path, required=True, help="Path to h1_emulator.py frontend")
    parser.add_argument("--image-builder", type=Path, required=True, help="Path to build_h1_test_image.py support script")
    parser.add_argument("--nand-reader", type=Path, required=True, help="Path to read_h1_test_file.py support script")
    parser.add_argument("--work-dir", type=Path, default=ROOT / "build/classic-firmware")
    parser.add_argument("--port", type=int, default=8813)
    parser.add_argument("--gdb-port", type=int, default=8814)
    parser.add_argument("--start-count", type=int, default=1)
    parser.add_argument("--title-wait", type=float, default=5)
    parser.add_argument("--level-wait", type=float, default=4)
    parser.add_argument("--audio-seconds", type=float, default=3,
                        help="Duration of the gameplay PCM capture (3..60 seconds)")
    parser.add_argument("--manual-gameplay", action="store_true",
                        help="Wait up to ten minutes at title for local API gameplay; create evidence/gameplay-complete.json to continue")
    parser.add_argument("--from-below", action="store_true", help="Select Classic and exercise From Below 1.0 controls")
    parser.add_argument("--about", action="store_true", help="Capture touch and keyboard entry/return from About")
    parser.add_argument("--legacy-direct-exit", action="store_true",
                        help="Replay historical builds where gameplay Escape exited directly")
    args = parser.parse_args()
    if not 3 <= args.audio_seconds <= 60:
        parser.error("--audio-seconds must be 3..60")
    work = args.work_dir.resolve()
    assert work.is_relative_to((ROOT / "build").resolve()), "Work directory must be inside h1-fc/build"
    assert args.port not in (8793, 8794) and args.gdb_port not in (8793, 8794)
    assert args.port != args.gdb_port
    assert not (work / "h1-system.raw").exists(), "Choose a fresh work directory to retain prior evidence"
    assert sha(args.bda) == args.expected_sha, "BDA changed before test"
    rom_data = args.rom.read_bytes()
    assert rom_data[:4] == b"NES\x1a", "Expected an iNES/NES 2.0 ROM"
    for port in (args.port, args.gdb_port):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
    out = work / "evidence"
    out.mkdir(parents=True)
    stage = work / "files/FC"
    stage.mkdir(parents=True)
    (stage / "GAME.nes").write_bytes(rom_data)
    package = work / "H1FC-tested.bda"
    shutil.copyfile(args.bda, package)
    subprocess.run([sys.executable, str(args.image_builder),
                    "--reuse-stage", "--work-dir", str(work), "--dump", str(args.dump),
                    "--kernel", str(args.kernel), "--bda", str(package)], check=True)
    build = json.loads((work / "image-build.json").read_text(encoding="utf-8"))
    assert build["bda_sha256"] == args.expected_sha
    server = f"http://127.0.0.1:{args.port}"
    command = [sys.executable, "-u", str(args.emulator),
               "--qemu", str(args.qemu), "--kernel", str(work / "project.bin"),
               "--nand", str(work / "h1-system.raw"),
               "--port", str(args.port), "--gdb-port", str(args.gdb_port), "--no-browser", "--writable"]
    if args.dll_dir:
        command += ["--dll-dir", str(args.dll_dir)]
    owned = False
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
                    owned = True
                    break
                except OSError:
                    time.sleep(.2)
            assert owned, "Private frontend did not become ready"
            subprocess.run([sys.executable, str(ROOT / "sdk/scripts/navigate_emulator_game_slot.py"),
                            "--server", server, "--no-reset", "--capture", str(out / "menu.png")], check=True)
            tap(server, 42, 51)
            time.sleep(3)
            capture(server, out / "picker.png")
            press_key(server, 39)
            time.sleep(args.title_wait)
            capture(server, out / "title.png")
            if args.from_below:
                assert sha(args.rom) == "1a3ac4faf4b35640505344059ae5d91dae07cd47e1fb4d9d2a33c76391f1c555"
                # This release has an opening credits screen before its title.
                time.sleep(8)
                capture(server, out / "title-ready.png")
                press_key(server, 25)
                time.sleep(1.5)
                press_key(server, 27)
                press_key(server, 35)
                time.sleep(.3)
                capture(server, out / "classic-options.png")
                press_key(server, 25)
                time.sleep(2)
                capture(server, out / "level-before.png")
                api(server, "/api/key", {"code": 40, "down": True})
                time.sleep(.45)
                api(server, "/api/key", {"code": 40, "down": False})
                capture(server, out / "moved-left.png")
                press_key(server, 16)
                capture(server, out / "rotated.png")
                press_key(server, 37)
                time.sleep(.25)
                capture(server, out / "hard-dropped.png")
            elif args.manual_gameplay:
                (out / "gameplay-ready.json").write_text(json.dumps({"server": server}), encoding="utf-8")
                deadline = time.monotonic() + 600
                while not (out / "gameplay-complete.json").is_file():
                    assert time.monotonic() < deadline, "Manual gameplay timeout"
                    assert frontend.poll() is None, "Frontend exited during manual gameplay"
                    time.sleep(.2)
            else:
                for _ in range(args.start_count):
                    press_key(server, 25)
                    time.sleep(args.level_wait)
                capture(server, out / "level-before.png")
                api(server, "/api/key", {"code": 35, "down": True})
                time.sleep(.6)
                api(server, "/api/key", {"code": 16, "down": True})
                time.sleep(.2)
                capture(server, out / "right-and-a.png")
                api(server, "/api/key", {"code": 16, "down": False})
                api(server, "/api/key", {"code": 35, "down": False})
                time.sleep(.5)
                capture(server, out / "level-after.png")
            audio = record_audio(args.port, out / "game-audio.wav", args.emulator, args.audio_seconds)
            before_pause = api(server, "/api/status")
            # P opens the application menu. Escape is available to the game
            # during gameplay and serves as Return only inside this menu.
            press_key(server, 31)
            time.sleep(2)
            capture(server, out / "pause.png")
            time.sleep(1)
            capture(server, out / "pause-stable.png")
            assert ImageChops.difference(Image.open(out / "pause.png").convert("RGB"),
                                         Image.open(out / "pause-stable.png").convert("RGB")).getbbox() is None
            if args.about:
                tap(server, 240, 252)
                time.sleep(.4)
                capture(server, out / "about-touch.png")
                press_key(server, 41)
                time.sleep(.2)
                press_key(server, 39)
                time.sleep(.4)
                capture(server, out / "about-key.png")
                press_key(server, 39)
                time.sleep(.2)
                press_key(server, 35)
            press_key(server, 39 if args.legacy_direct_exit else 24)
            # Under host load, a fixed-duration synthetic key press can be
            # missed by the emulated GUI. Observe the pause header before
            # recording audio; retry Escape while the root menu is visible.
            pause_header=Image.open(out / "pause.png").convert("RGB").crop((0,0,480,50))
            for attempt in range(4):
                time.sleep(.5)
                capture(server, out / "resume-check.png")
                current_header=Image.open(out / "resume-check.png").convert("RGB").crop((0,0,480,50))
                if ImageChops.difference(pause_header,current_header).getbbox() is not None:
                    break
                if attempt==3:raise AssertionError("Return/Continue did not leave the emulator pause menu")
                if args.legacy_direct_exit:
                    tap(server,120,76)
                else:
                    press_key(server,24)
            time.sleep(2)
            capture(server, out / "resumed.png")
            resumed_audio = record_audio(args.port, out / "resumed-audio.wav", args.emulator, 2)
            resumed_gameplay_changed = None
            if args.from_below:
                capture(server, out / "resumed-later.png")
                # The game's own pause screen still plays music. Check the
                # falling-block well as well, so a leaked Start cannot pass
                # merely because the emulator resumed emitting PCM.
                well = (204, 54, 277, 235)
                first = Image.open(out / "resumed.png").convert("RGB").crop(well)
                later = Image.open(out / "resumed-later.png").convert("RGB").crop(well)
                resumed_gameplay_changed = ImageChops.difference(first, later).getbbox() is not None
                assert resumed_gameplay_changed, "From Below stayed frozen after leaving the emulator pause menu"
            resumed = api(server, "/api/status")
            press_key(server, 24 if args.legacy_direct_exit else 31)
            if not args.legacy_direct_exit:
                time.sleep(1)
                capture(server, out / "exit-menu.png")
                exit_header = Image.open(out / "exit-menu.png").convert("RGB").crop((0, 0, 480, 50))
                assert ImageChops.difference(pause_header, exit_header).getbbox() is None, \
                    "Gameplay P did not open the pause menu"
                # Root cursor 0 -> Up (-2 modulo 9) -> Exit, cursor 7.
                press_key(server, 37)
                press_key(server, 39)
            time.sleep(4)
            capture(server, out / "exit-desktop.png")
            (out / "interaction.json").write_text(json.dumps({"audio": audio, "resumed_audio": resumed_audio,
                "before_pause": before_pause, "resumed": resumed}, indent=2), encoding="utf-8")
        finally:
            try:
                if owned and frontend.poll() is None:
                    stopped = api(server, "/api/stop", {})
                    (out / "stopped-status.json").write_text(json.dumps(stopped, indent=2), encoding="utf-8")
            finally:
                if frontend.poll() is None:
                    frontend.terminate()
                    frontend.wait(timeout=10)
    reader = load_module("classic_nand_reader", args.nand_reader)
    nand = reader.H1Nand(work / "h1-system.raw")
    try:
        log = reader.Fat16(nand).file("FC/h1fc.log")
    finally:
        nand.stream.close()
    (out / "h1fc.log").write_bytes(log)
    text = log.decode("utf-8", errors="replace")
    stops = re.findall(r"STOP video=(\d+) errors=(\d+)", text)
    assert stops and int(stops[-1][0]) > 0 and int(stops[-1][1]) == 0
    assert "PAUSE_MENU_BEGIN" in text and "PAUSE_MENU_END action=0" in text
    if not args.legacy_direct_exit:
        assert "PAUSE_MENU_END action=2" in text, "Exit menu selection did not complete"
    assert "APP_END result=0" in text
    stopped = json.loads((out / "stopped-status.json").read_text(encoding="utf-8"))
    assert stopped["running"] is False and stopped["returncode"] == 0
    report = {"ok": True, "game": args.title, "environment": "Full H1 V1.41 firmware/QEMU bbkh1",
        "physical_device_tested": False, "original_dump_modified": False,
        "bda_sha256": sha(package), "kernel_sha256": build["kernel_sha256"],
        "rom_sha256": sha(args.rom), "rom_bytes": len(rom_data),
        "mapper": (rom_data[6] >> 4) | (rom_data[7] & 0xf0),
        "video_frames": int(stops[-1][0]), "video_errors": int(stops[-1][1]),
        "audio": audio, "resumed_audio": resumed_audio,
        "resumed_gameplay_changed": resumed_gameplay_changed,
        "paused_display_stable": True, "app_end_result": 0, "qemu_returncode": 0,
        "exit_protocol": "legacy-direct" if args.legacy_direct_exit else "p-menu-escape-return-confirm-exit",
        "screenshots": {p.name: sha(p) for p in out.glob("*.png")},
        "limitations": ["Game-specific title/level/action results require visual inspection of screenshots",
                        "Full firmware emulation does not establish physical-device performance"]}
    if (out / "gameplay-complete.json").is_file():
        report["gameplay_observations"] = json.loads((out / "gameplay-complete.json").read_text(encoding="utf-8"))
    (out / "classic-smoke.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
