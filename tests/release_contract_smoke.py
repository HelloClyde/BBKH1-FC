"""Host regressions for public-build identity and isolated runtime evidence."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import package_release
import verify_release


class ReleaseContract(unittest.TestCase):
    def setUp(self):
        self.metadata = dict(test_frames=0, benchmark=False, verbose_log=False, profile=False,
                             optimized_event_core=True, native_jit=False, jit=False, experimental=False,
                             cpu_backend="optimized_C_with_original_fallback", sha256="a" * 64)

    def test_default_backend(self):
        verify_release.require_default_build(self.metadata)

    def test_other_modes_are_not_public_default(self):
        changes = dict(test_frames=20, benchmark=True, verbose_log=True, profile=True, diagnostic=True,
                       optimized_event_core=False, native_jit=True, jit=True, experimental=True,
                       cpu_backend="mips32_native_jit")
        for key, value in changes.items():
            with self.subTest(key=key), self.assertRaises(ValueError):
                verify_release.require_default_build({**self.metadata, key: value})

    def test_missing_backend_identity_is_not_accepted(self):
        for key in ("optimized_event_core", "native_jit", "jit", "experimental", "cpu_backend"):
            data = copy.copy(self.metadata)
            del data[key]
            with self.subTest(key=key), self.assertRaises(ValueError):
                verify_release.require_default_build(data)

    def test_native_report_cannot_replace_default_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "build/verification"
            (out / "native").mkdir(parents=True)
            normal = dict(ok=True, menu=True, bda_sha256=self.metadata["sha256"])
            native = {**normal, "bda_sha256": "b" * 64}
            pause = dict(ok=True, sha256=self.metadata["sha256"], state_slots_restored=3)
            (out / "menu-smoke.json").write_text(json.dumps(normal), encoding="utf-8")
            (out / "native/menu-smoke.json").write_text(json.dumps(native), encoding="utf-8")
            (out / "pause-menu-smoke.json").write_text(json.dumps(pause), encoding="utf-8")
            with patch.object(package_release, "ROOT", root):
                self.assertEqual(package_release.collect_mips_evidence(self.metadata)["menu-smoke.json"], normal)
                (out / "menu-smoke.json").write_text(json.dumps(native), encoding="utf-8")
                with self.assertRaises(ValueError):
                    package_release.collect_mips_evidence(self.metadata)

    def test_incomplete_pause_evidence_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "build/verification"
            out.mkdir(parents=True)
            (out / "menu-smoke.json").write_text(json.dumps(dict(ok=True, menu=True,
                                                    bda_sha256=self.metadata["sha256"])), encoding="utf-8")
            (out / "pause-menu-smoke.json").write_text(json.dumps(dict(ok=True, state_slots_restored=2,
                                                          sha256=self.metadata["sha256"])), encoding="utf-8")
            with patch.object(package_release, "ROOT", root), self.assertRaises(ValueError):
                package_release.collect_mips_evidence(self.metadata)

    def test_private_binary_source_rejected(self):
        for name in ("build/log.json", "dist/output.txt", "tests/game.nes", "firmware.bin",
                     "../outside.txt", "C:/outside.txt", "a\\file.txt", ".git/config"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                package_release.source_allowed(name)
        package_release.source_allowed("src/platform/jit_backoff.inc")


if __name__ == "__main__":
    unittest.main(verbosity=2)
