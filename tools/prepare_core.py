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
    text = replace_once(text, '#include "../../fceu.h"',
                        '#include "../../fceu.h"\n#include "platform/nes_video.h"')
    text = replace_once(text, "void retro_run(void)\n{", "extern int h1_nes_skip_frame(void);\n\nvoid retro_run(void)\n{")
    text = replace_once(text, "void retro_reset(void)\n{",
        "extern void save_policy_reset(void);\n\nvoid retro_reset(void)\n{\n   save_policy_reset();")
    text = replace_once(text, "   FCEUI_Emulate(&gfx, &sound, &ssize, 0);", "   FCEUI_Emulate(&gfx, &sound, &ssize, h1_nes_skip_frame());")
    text = replace_once(text, "   retro_run_blit(gfx);", "   if (gfx) retro_run_blit(gfx);")
    text = replace_once(text,
        '      gfx    += (crop_overscan_v_top * 256) + crop_overscan_h_left;',
        '      gfx    += (crop_overscan_v_top * 256) + crop_overscan_h_left;\n'
        '      if (GameInfo->type != GIT_NSF && active_pixformat == RETRO_PIXEL_FORMAT_RGB565)\n'
        '      {\n'
        '         h1_nes_video_frame frame = {gfx, XDBuf + (gfx - XBuf), retro_palette,\n'
        '             width, height, 256, use_raw_palette ? 0x3F : 0xFF};\n'
        '         if (h1_nes_present_indexed(&frame)) return;\n'
        '      }')
    text = replace_once(text,
        '      if (!strcmp(var.value, "32KHz"))\n         sndsamplerate_hint = 32000;',
        '      if (!strcmp(var.value, "22KHz"))\n         sndsamplerate_hint = 22050;\n'
        '      else if (!strcmp(var.value, "32KHz"))\n         sndsamplerate_hint = 32000;')
    text = replace_once(text, "   FCEUI_Initialize();",
        "   if (!fceu_video_out || !FCEUI_Initialize())\n"
        "   {\n      free(fceu_video_out);\n      fceu_video_out = NULL;\n"
        "      FCEUI_Kill();\n      return false;\n   }")
    driver.write_text(text, encoding="utf-8", newline="\n")

    # The upstream pair LUT is rebuilt on every background span, even when
    # all sixteen palette bytes are unchanged. Cache the exact priority-bit
    # palette at the SAME call site, including mid-scanline writes/restores.
    path = output / "src/ppu.c"
    text = path.read_text(encoding="utf-8")
    text = replace_once(text, '#include        "fceu.h"', '#include        "fceu.h"\n#include "platform/profile.h"')
    text = replace_once(text,
        'static void FCEU_BuildBgPairLUT(void)\n{\n\tint b;',
        'static void FCEU_BuildBgPairLUT(void)\n{\n'
        '\tstatic uint8_t cached_palette[16];\n\tstatic unsigned valid;\n\tint b;\n'
        '\tif (valid && !memcmp(cached_palette, PALRAM, 16)) return;\n'
        '\tmemcpy(cached_palette, PALRAM, 16);\n\tvalid = 1;')
    text = replace_once(text,
        'static void FASTAPASS(1) RefreshLine(int lastpixel) {',
        'static void FASTAPASS(1) RefreshLineBody(int lastpixel);\n'
        'static void FASTAPASS(1) RefreshLine(int lastpixel) {\n'
        '\tif (h1_profile_ppu_sample) {\n'
        '\t\th1_profile_push(HP_RENDER);\n\t\tRefreshLineBody(lastpixel);\n\t\th1_profile_pop();\n'
        '\t} else RefreshLineBody(lastpixel);\n}\n'
        'static void FASTAPASS(1) RefreshLineBody(int lastpixel) {')
    # Skip only the final colour/emphasis stores. Background fetches, sprite
    # evaluation/composition, hit detection, CPU/APU and mapper hooks remain.
    text = replace_once(text, 'static int spork = 0;',
                        'static int spork = 0;\nstatic unsigned h1_skip_colour_output;')
    text = replace_once(text, '\tif (SpriteON)\n\t\tCopySprites(target);',
                        '\tif (SpriteON) {\n'
                        '\t\tif (h1_skip_colour_output) spork = 0;\n'
                        '\t\telse CopySprites(target);\n\t}')
    # On dropped frames only sprite zero's mask is needed by CheckSpriteHit.
    # FetchSpriteData (including overflow and mapper accesses) is unchanged.
    # Keep numsprites/blurp/spork transitions identical to RefreshSprites.
    text = replace_once(text, '\tFCEU_dwmemset(sprlinebuf, 0x80808080, 256);\n\tnumsprites--;',
        '\tif (h1_skip_colour_output) {\n'
        '\t\tSPRB *zero = (SPRB*)SPRBUF;\n'
        '\t\tuint8_t mask = zero->ca[0] | zero->ca[1];\n'
        '\t\tnumsprites--;\n'
        '\t\tif (mask && SpriteBlurp && !(PPU_status & 0x40)) {\n'
        '\t\t\tsphitx = zero->x;\n\t\t\tsphitdata = mask;\n'
        '\t\t\tif (zero->atr & H_FLIP)\n'
        '\t\t\t\tsphitdata = ((mask << 7) & 0x80) | ((mask << 5) & 0x40) |\n'
        '\t\t\t\t\t((mask << 3) & 0x20) | ((mask << 1) & 0x10) |\n'
        '\t\t\t\t\t((mask >> 1) & 0x08) | ((mask >> 3) & 0x04) |\n'
        '\t\t\t\t\t((mask >> 5) & 0x02) | ((mask >> 7) & 0x01);\n'
        '\t\t}\n\t\tSpriteBlurp = 0;\n\t\tspork = 1;\n\t\treturn;\n\t}\n'
        '\tFCEU_dwmemset(sprlinebuf, 0x80808080, 256);\n\tnumsprites--;')
    begin = text.index('\tif (ScreenON || SpriteON) {\t/* Yes, very el-cheapo. */')
    end = text.index('\n    sphitx = 0x100;', begin)
    text = text[:begin] + '\tif (!h1_skip_colour_output) {\n' + text[begin:end] + '\n\t}\n' + text[end:]
    text = replace_once(text, 'int FCEUPPU_Loop(int skip) {',
                        'int FCEUPPU_Loop(int skip) {\n'
                        '\th1_skip_colour_output = skip && !InputScanlineHook;')
    path.write_text(text, encoding="utf-8", newline="\n")

    # Keep every tile fetch, scroll update and mapper hook. Drop only pixel
    # packing when neither visible output nor sprite-zero hit needs it.
    path = output / "src/pputile.h"
    text = path.read_text(encoding="utf-8")
    text = replace_once(text, 'if (X1 >= 2) {\n\tuint32_t pixdata;',
                        'if (X1 >= 2) {\n'
                        '\tif (!h1_skip_colour_output || sphitx != 0x100) {\n'
                        '\tuint32_t pixdata;')
    text = replace_once(text, '\tP += 8;\n}', '\t}\n\tP += 8;\n}')
    path.write_text(text, encoding="utf-8", newline="\n")

    # Guard the standard pure ROM handler; custom read handlers must still run
    # on every access. The Page mapping remains authoritative after bank changes.
    path = output / "src/x6502.c"
    text = path.read_text(encoding="utf-8")
    text = replace_once(text, '#include "sound.h"', '#include "sound.h"\n#include "cart.h"')
    text = replace_once(text,
        'static INLINE uint8_t RdMemNorm(uint32_t A) {\n\treturn(_DB = ARead[A](A));\n}',
        'static INLINE uint8_t RdMemNorm(uint32_t A) {\n'
        '\treadfunc handler = ARead[A];\n'
        '\tif (handler == CartBR) return (_DB = Page[A >> 11][A]);\n'
        '\treturn (_DB = handler(A));\n}')
    path.write_text(text, encoding="utf-8", newline="\n")

    path = output / "src/fceu.c"
    text = path.read_text(encoding="utf-8")
    text = replace_once(text, '#include  "sound.h"', '#include  "sound.h"\n#include "platform/profile.h"')
    text = replace_once(text, '\tssize = FlushEmulateSound();',
                        '\th1_profile_push(HP_MIX);\n\tssize = FlushEmulateSound();\n\th1_profile_pop();')
    path.write_text(text, encoding="utf-8", newline="\n")

    # N163 LQ integrates sixteen substeps into each PCM sample. Accumulate
    # runs with unchanged wavetable output at once, stopping at BOTH a PCM
    # boundary and the next phase advance. This keeps the original phase,
    # wavetable accesses and integer PCM exactly, including inc <= 0 cases.
    path = output / "src/boards/n106.c"
    text = path.read_text(encoding="utf-8")
    text = replace_once(text,
        'static void DoNamcoSound(int32_t *WaveBuf, int Count) {\n\tint P, V;',
        'static void DoNamcoSound(int32_t *WaveBuf, int Count) {\n'
        '\tstatic uint32_t cached_rate[8], cached_freq[8], cached_channels[8];\n'
        '\tstatic int32_t cached_inc[8];\n\tint P, V;')
    text = replace_once(text,
        '\t\t\t\tinc = (int32_t)((((uint64_t)FSettings.SndRate << 37) * (uint64_t)(c * 45)) /\n'
        '\t\t\t\t\t\t((uint64_t)freq * 21477272ULL));',
        '\t\t\t\t/* Only derived values: keys survive save-state loads safely. */\n'
        '\t\t\t\tif (cached_rate[P] == (uint32_t)FSettings.SndRate &&\n'
        '\t\t\t\t    cached_freq[P] == freq && cached_channels[P] == (uint32_t)c)\n'
        '\t\t\t\t\tinc = cached_inc[P];\n'
        '\t\t\t\telse {\n'
        '\t\t\t\t\tinc = (int32_t)((((uint64_t)FSettings.SndRate << 37) * (uint64_t)(c * 45)) /\n'
        '\t\t\t\t\t\t((uint64_t)freq * 21477272ULL));\n'
        '\t\t\t\t\tcached_rate[P] = FSettings.SndRate; cached_freq[P] = freq;\n'
        '\t\t\t\t\tcached_channels[P] = c; cached_inc[P] = inc;\n'
        '\t\t\t\t}')
    text = replace_once(text,
        '\t\t\tfor (V = 0; V < Count * 16; V++) {',
        '\t\t\tfor (V = 0; V < Count * 16;) {\n'
        '\t\t\t\tunsigned span = 1;')
    text = replace_once(text,
        '\t\t\t\tWaveBuf[V >> 4] += duff2;\n\t\t\t\tvco += 0x8000;',
        '\t\t\t\tif (inc > 0 && vco < inc) {\n'
        '\t\t\t\t\tuint32_t distance = (uint32_t)inc - (uint32_t)vco;\n'
        '\t\t\t\t\tunsigned until_phase = ((distance - 1) >> 15) + 1;\n'
        '\t\t\t\t\tspan = 16u - ((unsigned)V & 15u);\n'
        '\t\t\t\t\tif (span > until_phase) span = until_phase;\n'
        '\t\t\t\t}\n'
        '\t\t\t\tWaveBuf[V >> 4] += duff2 * span;\n'
        '\t\t\t\tvco += 0x8000 * span;\n'
        '\t\t\t\tV += span;')
    path.write_text(text, encoding="utf-8", newline="\n")

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
    from prepare_ppu import prepare_ppu
    prepare_ppu(output)
    from prepare_apu import prepare_apu
    prepare_apu(output)
    (output / "UPSTREAM_COMMIT").write_text(CORE_COMMIT + "\n", encoding="ascii")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[1]
    parser.add_argument("--source", type=Path, default=root / "third_party/fceumm")
    parser.add_argument("--output", type=Path, default=root / "build/fceumm-h1")
    args = parser.parse_args()
    print(prepare(args.source, args.output))
