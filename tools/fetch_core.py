"""Initialize the pinned FCEUmm submodule without modifying existing checkouts.

Normal setup is ``git submodule update --init --recursive``. This helper also
accepts --destination for a separate, pristine copy of the same pinned core.
"""
from pathlib import Path
import subprocess
from prepare_core import CORE_COMMIT, CORE_URL

ROOT = Path(__file__).resolve().parents[1]


def fetch(destination: Path) -> Path:
    destination = Path(destination).resolve()
    if destination == ROOT / "third_party/fceumm" and (ROOT / ".gitmodules").is_file():
        if not (destination / ".git").exists():
            subprocess.run(["git", "-C", str(ROOT), "submodule", "update", "--init", "--checkout",
                            "--", "third_party/fceumm"], check=True)
    if destination.exists():
        if not (destination / ".git").exists():
            raise RuntimeError(f"Existing destination is not a Git checkout: {destination}")
        actual = subprocess.check_output(["git", "-C", str(destination), "rev-parse", "HEAD"], text=True).strip()
        dirty = subprocess.check_output(["git", "-C", str(destination), "status", "--porcelain"], text=True).strip()
        if dirty or actual != CORE_COMMIT:
            raise RuntimeError(f"Refusing to overwrite existing FCEUmm checkout: {destination}; expected clean {CORE_COMMIT}")
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(destination)], check=True)
    subprocess.run(["git", "-C", str(destination), "remote", "add", "origin", CORE_URL], check=True)
    subprocess.run(["git", "-C", str(destination), "fetch", "--depth", "1", "origin", CORE_COMMIT], check=True)
    subprocess.run(["git", "-C", str(destination), "checkout", "--detach", CORE_COMMIT], check=True)
    return destination


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path,
                        default=Path(__file__).resolve().parents[1] / "third_party/fceumm")
    print(fetch(parser.parse_args().destination))
