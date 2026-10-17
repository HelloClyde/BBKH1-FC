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
    parser.add_argument("--diagnostic", action="store_true", help="Build the isolated automatic SGZ2 diagnosis application")
    parser.add_argument("--jit", action="store_true",
                        help="Compatibility option: the optimized CPU/event core is now always built; does not enable native code")
    parser.add_argument("--native-jit", action="store_true",
                        help="Experimental: enable MIPS32 native dispatch with NOP admission and thrash backoff")
    parser.add_argument("--jit-cache-mib", type=int, choices=(1, 2, 4, 8, 10), default=8,
                        help="Retained heap code cache size (default 8 MiB; entry/index tables are additional)")
    parser.add_argument("--build-dir", type=Path,
                        help="Use a separate output directory and imply --no-dist")
    parser.add_argument("--no-dist", action="store_true",
                        help="Keep comparison binaries in their build variant; do not replace dist artifacts")
    parser.add_argument("--optimize", choices=("O2", "O3"), default="O2",
                        help="Compare optimization levels without changing MIPS32 ISA")
    parser.add_argument("--lto", action="store_true", help="Enable GCC link-time optimization")
    parser.add_argument("--jobs", type=int, default=min(8, os.cpu_count() or 1),
                        help="Number of concurrent compiler processes (default: up to 8)")
    args = parser.parse_args()
    if args.diagnostic:
        if args.test_frames or args.benchmark or args.verbose_log:
            parser.error("--diagnostic owns its replay; test-frames/benchmark/verbose-log are incompatible")
        args.native_jit = args.profile = args.no_dist = True
    if args.build_dir is not None:
        args.no_dist = True
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
    if args.native_jit:
        name += "-jit"
    if args.jit_cache_mib != 8:
        name += f"-{args.jit_cache_mib}m"
    if args.optimize != "O2":
        name += "-" + args.optimize.lower()
    if args.lto:
        name += "-lto"
    if args.diagnostic:
        name += "-diagnostic"
    build = args.build_dir.resolve() if args.build_dir else ROOT / "build" / name
    build.mkdir(parents=True, exist_ok=True)
    # Variant-local prepared sources: concurrent release/profile/LTO builds
    # must never compile a tree another process is copying and patching.
    core = prepare(CORE, build / "fceumm-h1")
    # The optimized C loop, event boundaries and existing HLE are injected by
    # this preparation too; disabling native dispatch must not remove them.
    from prepare_jit import prepare_jit
    prepare_jit(core)
    if args.profile:
        from prepare_profile_cpu import prepare_profile_cpu
        from prepare_profile_ppu import prepare_profile_ppu
        prepare_profile_cpu(core, jit=True)
        prepare_profile_ppu(core)
    includes = [ROOT / "src", ROOT / "src/libc/include", SDK / "sdk/include", *core_includes(core)]
    flags = [
        "-EL", "-march=mips32", "-mabi=32", "-msoft-float", "-mno-abicalls", "-G0", "-fno-pic",
        "-" + args.optimize, "-ffreestanding", "-fno-builtin", "-fno-stack-protector", "-ffunction-sections",
        "-fdata-sections", "-fno-strict-aliasing", "-g0", "-Wall", "-Wextra",
        f"-ffile-prefix-map={ROOT}=h1-fc", f"-DH1_TEST_FRAMES={args.test_frames}",
        f"-DH1_BENCHMARK={int(args.benchmark)}", f"-DH1_TRACE={int(args.verbose_log)}",
        f"-DH1_PROFILE={int(args.profile)}",
        f"-DH1_DIAGNOSTIC={int(args.diagnostic)}",
        "-DH1_NES_JIT=1", f"-DH1_NATIVE_JIT={int(args.native_jit)}",
        f"-DH1_JIT_CACHE_MIB={args.jit_cache_mib}",
    ]
    if args.lto:
        flags.append("-flto")
    flags += [value if value.startswith("-D") else "-D" + value for value in CORE_DEFINES]
    flags += [value for folder in includes for value in ["-I", str(folder)]]
    sources = [ROOT / source for source in APP_SOURCES]
    sources.append(ROOT / "src/platform/nes_jit.c")
    if args.diagnostic:
        sources.append(ROOT / "src/platform/diagnostic_replay.c")
    for source in core_sources(core):
        source = Path(source)
        sources.append(source if source.is_absolute() else core / source)
    missing = [str(source) for source in sources if not source.is_file()]
    if missing:
        parser.error("Missing source files:\n" + "\n".join(missing))
    headers = hashlib.sha256()
    for directory in (ROOT / "src", SDK / "sdk/include", core):
        for header in sorted(p for p in directory.rglob("*")
                             if p.suffix in (".h", ".inc")):
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
        if args.lto and source == ROOT / "src/libc/freestanding.c":
            # Compiler-generated late memset/memcpy calls need externally
            # visible runtime definitions after LTO internalization/GC.
            compile_flags = [flag for flag in compile_flags if flag != "-flto"]
            compile_flags.append("-fno-lto")
        is_core = source.is_relative_to(core)
        if is_core:
            compile_flags = [flag for flag in compile_flags if not flag.startswith(
                ("-DH1_TEST_FRAMES=", "-DH1_BENCHMARK=", "-DH1_TRACE="))]
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
    # VERSION also labels the install metadata. Reject a stale application
    # log or About string before publishing a misleading release artifact.
    version_bytes = VERSION.encode("ascii")
    if (b"APP_BEGIN version=" + version_bytes + b" " not in payload or
            b"v" + version_bytes + b"\0" not in payload):
        raise RuntimeError("VERSION disagrees with the compiled APP_BEGIN/About strings")
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
    title = "NES-D" if args.diagnostic else "NES"
    data = (encode_header(fields, title=title, build_time=build_time) + resources +
            payload + bytes(padding))
    dest = build / "H1FC.bda"
    dest.write_bytes(data)
    report = validate_bda(dest)
    if not report["ok"]:
        raise RuntimeError(report)
    digest = hashlib.sha256(data).hexdigest()
    metadata = {
        "title": title, "version": VERSION, "sha256": digest, "size": len(data),
        "payload_sha256": hashlib.sha256(data[PAYLOAD_OFFSET:]).hexdigest(),
        "sdk_commit": SDK_COMMIT, "fceumm_commit": CORE_COMMIT, "core": "FCEUmm",
        "entry_va": "0x83c00020", "formats": ["nes"],
        "audio": "H1 native PCM 32000 Hz mono", "log_path": "A:\\FC\\h1fc.log",
        "test_frames": args.test_frames, "benchmark": args.benchmark,
        "verbose_log": args.verbose_log, "profile": args.profile,
        "compiler": compiler_version, "build_time": build_time,
        "optimization": args.optimize, "lto": args.lto,
        "optimized_event_core": True,
        "jit": args.native_jit, "native_jit": args.native_jit,
        "experimental": args.native_jit,
        "cpu_backend": "experimental_native_with_C_fallback" if args.native_jit else "optimized_C_with_original_fallback",
        "jit_backend": "MIPS32_EVENT_V6_BACKOFF2" if args.native_jit else "disabled",
        "jit_cache_mib": args.jit_cache_mib,
        "native_cache_retained_when_eligible": True,
        "icon": icon.relative_to(ROOT).as_posix(),
        "icon_sha256": hashlib.sha256(icon.read_bytes()).hexdigest(),
    }
    if args.profile:
        metadata["profile_layout"] = "core_detail_v1"
    if args.diagnostic:
        metadata.update(diagnostic=True, diagnostic_revision=1, replay_groups=15,
                        log_path="A:\\FC\\nesdiag.log", profile_layout="diagnostic_frames_v1")
    dest.with_suffix(".bda.sha256").write_text(f"{digest}  H1FC.bda\n", encoding="ascii")
    dest.with_suffix(".build.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    if not args.test_frames and not args.no_dist:
        target = "H1FC"
        if args.native_jit:
            target += "-jit"
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
