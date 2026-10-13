"""Build the H1 FC emulator with the pinned FCEUmm core and H1 SDK."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SDK = ROOT / "sdk"
CORE = ROOT / "third_party/fceumm"
SDK_COMMIT = "067fe072477861dfc8949d7b1a55279fb92d2548"
VERSION = (ROOT / "VERSION").read_text(encoding="ascii").strip()
APP_SOURCES = [
    "src/runtime/entry.S", "src/runtime/startup.c", "src/libc/freestanding.c",
    "src/platform/frontend.c", "src/platform/linear_mxu.S", "src/platform/save.c",
    "src/platform/file_selector.c", "src/platform/diagnostics.c",
    "src/platform/audio.c", "src/platform/loading.c", "src/platform/menu.c",
    "src/platform/state.c", "src/platform/profile.c", "src/app.c",
    "src/nes_core.c", "src/compat/fceumm_compat.c", "src/compat/file_stream.c",
]


def run(arguments, **kwargs):
    return subprocess.run(list(map(str, arguments)), check=True, **kwargs)


def response_file(path, arguments):
    # GCC response files use shell-style quoting, even in a Windows build.
    path.write_text("\n".join('"' + str(value).replace("\\", "/").replace('"', '\\"') + '"'
                              for value in arguments) + "\n", encoding="utf-8")


def check_dependency(parser, repository, expected):
    from dependency import check_dependency as verify
    try:
        verify(repository, expected)
    except RuntimeError as error:
        parser.error(str(error))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--toolchain", type=Path,
                        default=Path(os.environ.get("H1_GNU_BIN", ROOT / ".tools/toolchain/bin")))
    parser.add_argument("--test-frames", type=int, default=0,
                        help="Exit after N frames; test packages remain in build/ (0..600)")
    parser.add_argument("--benchmark", action="store_true",
                        help="Disable audio/pacing; requires a bounded --test-frames build")
    parser.add_argument("--verbose-log", action="store_true", help="Enable detailed diagnostic logging")
    parser.add_argument("--profile", action="store_true", help="Collect H1 TCU timing diagnostics")
    parser.add_argument("--jobs", type=int, default=min(8, os.cpu_count() or 1),
                        help="Number of concurrent compiler processes (default: up to 8)")
    args = parser.parse_args()
    if not 0 <= args.test_frames <= 600:
        parser.error("--test-frames must be 0..600")
    if args.benchmark and not args.test_frames:
        parser.error("--benchmark requires --test-frames")
    if args.jobs < 1:
        parser.error("--jobs must be positive")

    def tool(name):
        prefix = args.toolchain / ("mipsel-none-elf-" + name)
        path = prefix.with_suffix(".exe") if prefix.with_suffix(".exe").is_file() else prefix
        if not path.is_file():
            parser.error(f"Missing tool: {prefix}; set H1_GNU_BIN or pass --toolchain")
        return path.resolve()

    gcc, gxx, objcopy, size_tool = (tool(name) for name in ("gcc", "g++", "objcopy", "size"))
    from prepare_core import CORE_COMMIT, CORE_DEFINES, core_includes, core_sources, prepare
    check_dependency(parser, SDK, SDK_COMMIT)
    check_dependency(parser, CORE, CORE_COMMIT)
    name = f"test-{args.test_frames}" if args.test_frames else "release"
    if args.benchmark:
        name += "-benchmark"
    if args.verbose_log:
        name += "-debug"
    if args.profile:
        name += "-profile"
    build = ROOT / "build" / name
    build.mkdir(parents=True, exist_ok=True)
    core = prepare(CORE, ROOT / "build/fceumm-h1")
    includes = [ROOT / "src", ROOT / "src/libc/include", SDK / "sdk/include", *core_includes(core)]
    flags = [
        "-EL", "-march=mips32", "-mabi=32", "-msoft-float", "-mno-abicalls", "-G0", "-fno-pic",
        "-O2", "-ffreestanding", "-fno-builtin", "-fno-stack-protector", "-ffunction-sections",
        "-fdata-sections", "-fno-strict-aliasing", "-g0", "-Wall", "-Wextra",
        f"-ffile-prefix-map={ROOT}=h1-fc", f"-DH1_TEST_FRAMES={args.test_frames}",
        f"-DH1_BENCHMARK={int(args.benchmark)}", f"-DH1_TRACE={int(args.verbose_log)}",
        f"-DH1_PROFILE={int(args.profile)}",
    ]
    flags += [value if value.startswith("-D") else "-D" + value for value in CORE_DEFINES]
    flags += [value for folder in includes for value in ["-I", str(folder)]]
    sources = [ROOT / source for source in APP_SOURCES]
    for source in core_sources(core):
        source = Path(source)
        sources.append(source if source.is_absolute() else core / source)
    missing = [str(source) for source in sources if not source.is_file()]
    if missing:
        parser.error("Missing source files:\n" + "\n".join(missing))
    headers = hashlib.sha256()
    for directory in (ROOT / "src", SDK / "sdk/include", core):
        for header in sorted(directory.rglob("*.h")):
            headers.update(str(header.relative_to(directory)).encode())
            headers.update(header.read_bytes())
    header_digest = headers.digest()
    compiler_version = run([gcc, "--version"], capture_output=True, text=True).stdout.splitlines()[0]
    cache_directory = ROOT / "build/object-cache"
    cache_directory.mkdir(parents=True, exist_ok=True)

    def compile_source(item):
        index, source = item
        obj = build / f"{index:03d}-{source.stem}.o"
        if source.suffix in (".cc", ".cpp"):
            compiler, language = gxx, ["-std=c++17", "-fno-exceptions", "-fno-rtti",
                                       "-fno-threadsafe-statics", "-fno-use-cxa-atexit"]
        elif source.suffix == ".S":
            compiler, language = gcc, ["-x", "assembler-with-cpp"]
        else:
            compiler, language = gcc, ["-std=c11"]
        compile_flags = list(flags)
        is_core = source.is_relative_to(core)
        if is_core:
            compile_flags = [flag for flag in compile_flags if not flag.startswith(
                ("-DH1_TEST_FRAMES=", "-DH1_BENCHMARK=", "-DH1_TRACE=", "-DH1_PROFILE="))]
        warnings = ["-Werror"] if source.is_relative_to(ROOT / "src") else []
        command = [compiler, *compile_flags, *warnings, *language, "-c", source, "-o", obj]
        signature = hashlib.sha256()
        signature.update((str(compiler) + compiler_version).encode())
        signature.update(json.dumps([str(value) for value in command[1:-2]]).encode())
        signature.update(source.read_bytes())
        signature.update(header_digest)
        cache = cache_directory / (signature.hexdigest() + ".o")
        if cache.is_file():
            shutil.copyfile(cache, obj)
        else:
            print(f"Compile {source.relative_to(ROOT)}", flush=True)
            result = subprocess.run(list(map(str, command)), cwd=ROOT, capture_output=True, text=True)
            if result.stdout:
                print(result.stdout, end="", flush=True)
            if result.stderr:
                print(result.stderr, end="", file=sys.stderr, flush=True)
            result.check_returncode()
            shutil.copyfile(obj, cache)
        return obj

    objects = [None] * len(sources)
    failed = []
    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        pending = {executor.submit(compile_source, item): item[0]
                   for item in enumerate(sources)}
        for future in as_completed(pending):
            index = pending[future]
            try:
                objects[index] = future.result()
            except subprocess.CalledProcessError:
                failed.append(str(sources[index].relative_to(ROOT)))
    if failed:
        raise RuntimeError("Compilation failed:\n" + "\n".join(sorted(failed)))
    elf, raw = build / "H1FC.elf", build / "H1FC.bin"
    link_arguments = [*flags, "-nostdlib", "-Wl,--build-id=none", "-Wl,--gc-sections",
                      f"-Wl,-T,{ROOT / 'src/runtime/h1.ld'}", f"-Wl,-Map,{build / 'H1FC.map'}",
                      "-o", elf, *objects, "-lgcc"]
    response = build / "link.rsp"
    response_file(response, link_arguments)
    run([gcc, "@" + str(response)], cwd=ROOT)
    run([size_tool, elf])
    run([objcopy, "-O", "binary", elf, raw])

    sys.path.insert(0, str(SDK))
    from h1_bda.header import HeaderFields, encode_header
    from h1_bda.resources import PAYLOAD_OFFSET, RESOURCE_OFFSET, RESOURCE_SIZES, build_icon_resources
    from h1_bda.validate import validate_bda
    payload = raw.read_bytes()
    icon = ROOT / "assets/fc-icon.png"
    if not icon.is_file():
        parser.error(f"Missing application icon: {icon}")
    resources = build_icon_resources(icon)
    package_size = PAYLOAD_OFFSET + len(payload)
    padding = (-package_size) & 3
    fields = HeaderFields(category=0x48, file_size_minus_4=package_size + padding - 4,
                          payload_offset=PAYLOAD_OFFSET, resource_offset=RESOURCE_OFFSET,
                          resource_sizes=RESOURCE_SIZES)
    build_time = datetime.fromtimestamp(int(os.environ.get("SOURCE_DATE_EPOCH", "1791849600")),
                                       timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    data = (encode_header(fields, title="H1 FC", build_time=build_time) + resources +
            payload + bytes(padding))
    dest = build / "H1FC.bda"
    dest.write_bytes(data)
    report = validate_bda(dest)
    if not report["ok"]:
        raise RuntimeError(report)
    digest = hashlib.sha256(data).hexdigest()
    metadata = {
        "title": "H1 FC", "version": VERSION, "sha256": digest, "size": len(data),
        "payload_sha256": hashlib.sha256(data[PAYLOAD_OFFSET:]).hexdigest(),
        "sdk_commit": SDK_COMMIT, "fceumm_commit": CORE_COMMIT, "core": "FCEUmm",
        "entry_va": "0x83c00020", "formats": ["nes"],
        "audio": "H1 native PCM 32000 Hz mono", "log_path": "A:\\FC\\h1fc.log",
        "test_frames": args.test_frames, "benchmark": args.benchmark,
        "verbose_log": args.verbose_log, "profile": args.profile,
        "compiler": compiler_version, "build_time": build_time,
        "icon": icon.relative_to(ROOT).as_posix(),
        "icon_sha256": hashlib.sha256(icon.read_bytes()).hexdigest(),
    }
    dest.with_suffix(".bda.sha256").write_text(f"{digest}  H1FC.bda\n", encoding="ascii")
    dest.with_suffix(".build.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    if not args.test_frames:
        target = "H1FC"
        if args.verbose_log:
            target += "-debug"
        if args.profile:
            target += "-profile"
        for directory in (ROOT / "dist",):
            directory.mkdir(exist_ok=True)
            for suffix in (".bda", ".bda.sha256", ".build.json"):
                output = directory / (target + suffix)
                if suffix == ".bda.sha256":
                    output.write_text(f"{digest}  {target}.bda\n", encoding="ascii")
                else:
                    shutil.copyfile(build / ("H1FC" + suffix), output)
    print(f"PASS: {dest} ({len(data)} bytes)\nSHA256 {digest}")


if __name__ == "__main__":
    main()
