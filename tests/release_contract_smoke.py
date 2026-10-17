"""Host regressions for public-build identity and isolated runtime evidence."""
from __future__ import annotations

import copy
import ast
import json
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import package_release
import verify_release
from resolve_release_ref import resolve


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

    def test_workflow_passes_required_test_arguments(self):
        workflow = (ROOT / ".github/workflows/build-release.yml").read_text(encoding="utf-8")
        for line in workflow.splitlines():
            if not line.strip().startswith("python tests/"):
                continue
            args = shlex.split(line.strip())
            tree = ast.parse((ROOT / args[1]).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and
                        node.func.attr == "add_argument" and node.args):
                    continue
                required = any(k.arg == "required" and isinstance(k.value, ast.Constant) and
                               k.value.value is True for k in node.keywords)
                name = node.args[0]
                if required and isinstance(name, ast.Constant) and str(name.value).startswith("--"):
                    with self.subTest(command=args[1], argument=name.value):
                        self.assertIn(name.value, args)


class ReleaseTarget(unittest.TestCase):
    commit = "a" * 40

    def fake_git(self, *args):
        self.calls.append(args)
        if args == ("show", self.commit + ":VERSION"):
            return "0.1.16"
        if args in (("rev-parse", "--verify", "HEAD"),
                    ("rev-parse", "--verify", "refs/tags/v0.1.16^{commit}")):
            return self.commit
        raise RuntimeError("Tag does not exist")

    def setUp(self):
        self.calls = []

    def test_main_pr_and_empty_manual_never_release(self):
        for event, ref in (("push", "refs/heads/main"), ("pull_request", "refs/pull/1/merge"),
                           ("workflow_dispatch", "refs/heads/main")):
            self.assertEqual(resolve(event, ref, "", self.fake_git),
                             dict(commit=self.commit, release_tag=""))
        self.assertNotIn(("show", self.commit + ":VERSION"), self.calls)

    def test_manual_and_tag_push_resolve_existing_target_version(self):
        for event, ref, requested in (("workflow_dispatch", "refs/heads/main", "v0.1.16"),
                                      ("push", "refs/tags/v0.1.16", "")):
            self.assertEqual(resolve(event, ref, requested, self.fake_git),
                             dict(commit=self.commit, release_tag="v0.1.16"))
        self.assertIn(("show", self.commit + ":VERSION"), self.calls)

    def test_malformed_input_rejected_before_git(self):
        for tag in ("main", "v0.1.16\ncommit=bad", "v0.1.16;echo x", " v0.1.16", "v01.1.16",
                    "v0.1.16^{commit}", "v0.1.16/other"):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                resolve("workflow_dispatch", "refs/heads/main", tag, self.fake_git)
        self.assertEqual(self.calls, [])

    def test_missing_tag_refused(self):
        with self.assertRaises(RuntimeError):
            resolve("workflow_dispatch", "refs/heads/main", "v0.1.17", self.fake_git)

    def test_version_mismatch_refused(self):
        def mismatched(*args):
            return "0.1.17" if args[0] == "show" else self.commit
        with self.assertRaises(ValueError):
            resolve("workflow_dispatch", "refs/heads/main", "v0.1.16", mismatched)

    def test_invalid_commit_output_refused(self):
        for value in ("main", "a" * 39, self.commit + "\nrelease_tag=v0.1.16"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve("workflow_dispatch", "refs/heads/main", "v0.1.16", lambda *args: value)


if __name__ == "__main__":
    unittest.main(verbosity=2)
