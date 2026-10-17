"""Reproduce the legacy wrap failure and verify the diagnostic RTC extension.

Host register-model validation only; this does not establish physical H1 clock
accuracy, cache cost, or application speed. No firmware or failure log is edited.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cc", default=shutil.which("gcc") or "C:/msys64/ucrt64/bin/gcc.exe")
    parser.add_argument("--output", type=Path, default=root / "build/verification/diagnostic-clock")
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    cc = Path(args.cc).resolve()
    env = os.environ.copy()
    env["PATH"] = str(cc.parent) + os.pathsep + env.get("PATH", "")
    source = root / "tests/profile_clock_diag.c"
    header = root / "src/platform/profile_clock.h"
    results = []
    for name, diagnostic in (("legacy-failure", 0), ("rtc-extension", 1)):
        exe = out / (name + (".exe" if os.name == "nt" else ""))
        command = [str(cc), "-std=c99", "-O2", "-Wall", "-Wextra", "-Werror",
                   f"-DH1_DIAGNOSTIC={diagnostic}", str(source), "-o", str(exe)]
        build = subprocess.run(command, env=env, text=True, capture_output=True, timeout=60)
        assert build.returncode == 0, build.stdout + build.stderr
        run = subprocess.run([str(exe)], env=env, text=True, capture_output=True, timeout=60)
        assert run.returncode == 0, run.stdout + run.stderr
        assert "PASS" in run.stdout, run.stdout
        results.append({"name": name, "H1_DIAGNOSTIC": diagnostic,
                        "command": command, "stdout": run.stdout.strip(),
                        "returncode": run.returncode, "executable_sha256": sha(exe)})
        print(run.stdout.strip())
    retained = root / "build/v0114-firmware-profile/restart-evidence/h1fc.log"
    old_report = root / "build/verification/v0114-profile-wrap-replay/report.json"
    failure = {"available": retained.exists(), "path": str(retained),
               "old_replay_report": str(old_report)}
    if retained.exists():
        failure["sha256"] = sha(retained)
        if old_report.exists():
            original = json.loads(old_report.read_text(encoding="utf-8"))
            failure["unchanged"] = failure["sha256"] == original["retained_failure_log_sha256"]
            assert failure["unchanged"], "Retained failure evidence changed"
    report = {
        "ok": True,
        "kind": "host C register-model tests; not hardware timing evidence",
        "source_sha256": sha(source), "header_sha256": sha(header),
        "compiler": str(cc), "results": results, "retained_failure": failure,
        "proof": {
            "snapshot": "RTC seconds before and after a stable TCNT pair must agree",
            "phase": "possible RTC subsecond phase is intersected with 0..32767 after every delta",
            "extension": "for an RTC change of 0 or 1 second, candidate deltas differ by 65536 ticks; the phase interval admits at most one",
            "long_gap": ">=2 RTC seconds is rejected before any total or phase update",
            "full_flag": "recorded and cleared but cannot create elapsed ticks",
        },
        "coverage": ["legacy 65464->14 FULL=0 remains fault2", "all 32768 initial RTC phases",
                     "absent, early, late FULL", "terminal 65535 plateau and zero transition",
                     "torn and stale reads", "persistent backwards counter even with FULL",
                     "multiple missed periods with forward-looking TCNT",
                     "RTC unavailable/backwards/unstable", "clock disagreement",
                     "QEMU-style 65535-period counter rejected against inclusive 65536-period RTC contract",
                     "calibration with edges during reads", "4.29M observations crossing uint32 total wrap",
                     "paused startup delay and fresh epoch on resume", "exact register restoration and busy rejection"],
        "limitations": ["physical H1 RTC/TCU visibility and calibration remain unverified",
                        "existing local QEMU uses TCNT % 65535 and may fail strict RTC agreement after wraps; this is not hardware evidence",
                        "two h1_wall_clock calls add four direct MMIO reads per normal snapshot",
                        "diagnostic light/detail/RTC-only runs must quantify instrumentation effects",
                        "an inconsistent clock disables this group's high-resolution measurements instead of repairing them"],
    }
    path = out / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main()
