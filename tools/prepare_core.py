"""Create a reproducible H1 build tree from the pinned, unmodified FCEUmm.

Only the generated tree is patched. No download occurs during a build.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
from dependency import check_dependency

CORE_URL = "https://github.com/libretro/libretro-fceumm.git"
CORE_COMMIT = "ac0d00a7fc01166f84c7bac01d1084ebfd2b865e"
CORE_DEFINES = ["__LIBRETRO__", "FRONTEND_SUPPORTS_RGB565=1", "HAVE_NO_LANGEXTRA",
                "PATH_MAX=1024", "FCEU_VERSION_NUMERIC=9900", "LSB_FIRST", "H1_FC"]
_SOURCES = """cart cheat crc32 fceu-endian fceu-memory fceu fds fds_apu file
filter general input md5 nsf palette ppu sound state video vsuni ines unif x6502""".split()


def core_sources(core: Path) -> list[Path]:
    src = Path(core) / "src"
    common = src / "drivers/libretro/libretro-common"
    return [*(src / (name + ".c") for name in _SOURCES),
            *sorted((src / "boards").glob("*.c")),
            *sorted((src / "input").glob("*.c")),
            src / "drivers/libretro/libretro.c",
            src / "drivers/libretro/libretro_dipswitch.c",
            common / "streams/memory_stream.c", common / "compat/compat_strl.c"]


def core_includes(core: Path) -> list[Path]:
    src = Path(core) / "src"
    return [src, src / "input", src / "boards", src / "drivers/libretro",
            src / "drivers/libretro/libretro-common/include"]


def replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise RuntimeError(f"Pinned FCEUmm patch context mismatch: {old[:80]!r}")
    return text.replace(old, new, 1)


def prepare(source: Path, output: Path) -> Path:
    source, output = Path(source).resolve(), Path(output).resolve()
    if source == output or source in output.parents or output in source.parents:
        raise ValueError("Source and generated tree must be separate directories")
    check_dependency(source, CORE_COMMIT)
    output.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source / "src", output / "src", dirs_exist_ok=True)
    for name in ("Copying", "Authors", "README.md", "Makefile.common"):
        shutil.copyfile(source / name, output / name)

    types = output / "src/fceu-types.h"
    text = types.read_text(encoding="utf-8")
    text = replace_once(text, "#include <stdint.h>",
                        '#include <stdint.h>\n#include "compat/fceumm_compat.h"')
    types.write_text(text, encoding="utf-8", newline="\n")

    # Avoid libc/libm dependencies for the NSF visualizer. H1-FC accepts only
    # cartridge ROMs, but shared NSF sound support is used by mapper code.
    nsf = output / "src/nsf.c"
    text = nsf.read_text(encoding="utf-8")
    begin = text.index("void DrawNSF(uint8_t *XBuf) {")
    end = text.index("\nvoid DoNSFFrame(void)", begin)
    text = text[:begin] + "void DrawNSF(uint8_t *XBuf) { (void)XBuf; }\n" + text[end:]
    nsf.write_text(text.replace("#include <math.h>\n", ""), encoding="utf-8", newline="\n")
    # These headers have no remaining users in cartridge builds.
    for filename, header in (("palette.c", "math.h"), ("ines.c", "math.h"),
                             ("boards/emu2413.h", "math.h"), ("general.c", "sys/stat.h")):
        path = output / "src" / filename
        text = path.read_text(encoding="utf-8").replace(f"#include <{header}>\n", "")
        path.write_text(text, encoding="utf-8", newline="\n")

    driver = output / "src/drivers/libretro/libretro.c"
    text = driver.read_text(encoding="utf-8")
    text = replace_once(text, "void retro_run(void)\n{", "extern int h1_nes_skip_frame(void);\n\nvoid retro_run(void)\n{")
    text = replace_once(text, "   FCEUI_Emulate(&gfx, &sound, &ssize, 0);", "   FCEUI_Emulate(&gfx, &sound, &ssize, h1_nes_skip_frame());")
    text = replace_once(text, "   retro_run_blit(gfx);", "   if (gfx) retro_run_blit(gfx);")
    text = replace_once(text, "   FCEUI_Initialize();",
        "   if (!fceu_video_out || !FCEUI_Initialize())\n"
        "   {\n      free(fceu_video_out);\n      fceu_video_out = NULL;\n"
        "      FCEUI_Kill();\n      return false;\n   }")
    driver.write_text(text, encoding="utf-8", newline="\n")

    # A mapper rejection must release the PRG/CHR/trainer allocations made
    # before mapper dispatch, since there will be no successful unload call.
    path = output / "src/ines.c"
    text = path.read_text(encoding="utf-8")
    text = replace_once(text,
        '            iNESCart.mapper);\n      return 0;\n   }\n\n   GameInterface = iNESGI;',
        '            iNESCart.mapper);\n      iNESGI(GI_CLOSE);\n      return 0;\n   }\n\n   GameInterface = iNESGI;')
    text = replace_once(text, '      if (!ExtraNTARAM)\n         return 0;',
        '      if (!ExtraNTARAM) { iNESGI(GI_CLOSE); return 0; }')
    path.write_text(text, encoding="utf-8", newline="\n")

    # Do not execute partially read cartridge buffers after a removable-media
    # error. The upstream full-path loader otherwise ignores a short read.
    path = output / "src/file.c"
    text = path.read_text(encoding="utf-8")
    text = replace_once(text,
        "   if (tmp->size)\n      filestream_read(tz, tmp->data_int, tmp->size);",
        "   if (tmp->size && filestream_read(tz, tmp->data_int, tmp->size) != (int64_t)tmp->size)\n"
        "   {\n      free(tmp->data_int);\n      free(tmp);\n      return NULL;\n   }")
    path.write_text(text, encoding="utf-8", newline="\n")
    (output / "UPSTREAM_COMMIT").write_text(CORE_COMMIT + "\n", encoding="ascii")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[1]
    parser.add_argument("--source", type=Path, default=root / "third_party/fceumm")
    parser.add_argument("--output", type=Path, default=root / "build/fceumm-h1")
    args = parser.parse_args()
    print(prepare(args.source, args.output))
