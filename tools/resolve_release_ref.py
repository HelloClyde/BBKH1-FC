"""Resolve a workflow target without changing tags or trusting shell input."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def resolve(event: str, ref: str, requested_tag: str = "", git_read=git) -> dict[str, str]:
    tag = requested_tag if event == "workflow_dispatch" else ""
    if event == "push" and ref.startswith("refs/tags/"):
        tag = ref[len("refs/tags/"):]
    if tag and re.fullmatch(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", tag) is None:
        raise ValueError("Release tag must be vMAJOR.MINOR.PATCH")
    commit = git_read("rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}" if tag else "HEAD")
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ValueError("Target must resolve to one full commit SHA")
    if tag:
        target_version = git_read("show", f"{commit}:VERSION")
        if tag != "v" + target_version:
            raise ValueError("Existing release tag differs from its target VERSION")
    return {"commit": commit, "release_tag": tag}


def main() -> None:
    result = resolve(os.environ["GITHUB_EVENT_NAME"], os.environ["GITHUB_REF"],
                     os.environ.get("REQUESTED_RELEASE_TAG", ""))
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8", newline="\n") as output:
        for key, value in result.items():
            output.write(f"{key}={value}\n")
    print("Selected application commit:", result["commit"])
    print("Release tag:", result["release_tag"] or "none (build only)")


if __name__ == "__main__":
    main()
