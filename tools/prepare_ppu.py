"""MIPS32 PPU data-path adaptations; observed fetch/event ordering is unchanged.

Unobserved plain-memory fetch spans can be collapsed on dropped frames while
retaining exact shifter state. Applied after palette-cache/output adaptations.
"""
from pathlib import Path


def _once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise RuntimeError(f"Pinned PPU context mismatch: {old[:80]!r}")
    return text.replace(old, new, 1)


def _protect_sprite_observer_bounds(text: str) -> str:
    # InputScanlineHook receives a writable sprite row. It can add pixels
    # outside the original bounds and remove itself before CopySprites runs.
    # Keep the expanded bounds with that row until RefreshSprites rebuilds it.
    call = 'InputScanlineHook(Plinef, spork ? sprlinebuf : 0, linestartts, lasttile * 8 - 16);'
    for indent in ('\t\t\t', '\t\t'):
        text = _once(text, '\n' + indent + call,
                     '\n' + indent + '/* The observer may write outside the generated sprite bounds. */\n'
                     + indent + 'if (spork) { h1_sprite_first = 0; h1_sprite_end = 256; }\n'
                     + indent + call)
    return text


def _plain_span(compact: bool = False) -> str:
    # The generic path retains nibble pens. The aligned path packs two-bit
    # pens and looks up four final pixels at once; a whole tile shares one
    # palette when fine-X is zero. No CHR pointer or decoded pattern survives
    # this call's CPU synchronization edge.
    emit = '''
        {
            uint32_t pixels = ppulut1[(s0 >> fine) & 255] |
                              ppulut2[(s1 >> fine) & 255] |
                              ppulut3[offset | (at << 3)];
            uint32_t p0 = fceu_bg_pair_lut[pixels & 255];
            uint32_t p1 = fceu_bg_pair_lut[(pixels >> 8) & 255];
            uint32_t p2 = fceu_bg_pair_lut[(pixels >> 16) & 255];
            uint32_t p3 = fceu_bg_pair_lut[(pixels >> 24) & 255];
#ifdef MSB_FIRST
            ((h1_ppu_word *)P)[0] = (p0 << 16) | p1;
            ((h1_ppu_word *)P)[1] = (p2 << 16) | p3;
#else
            ((h1_ppu_word *)P)[0] = p0 | (p1 << 16);
            ((h1_ppu_word *)P)[1] = p2 | (p3 << 16);
#endif
            P += 8;
        }
'''
    if compact:
        emit = '''
        {
            unsigned pens = h1_bg_plane2[(s0 >> 8) & 255] |
                            (h1_bg_plane2[(s1 >> 8) & 255] << 1);
            const uint32_t *palette = h1_bg_quad_lut + ((at & 3) << 8);
            ((h1_ppu_word *)P)[0] = palette[pens & 255];
            ((h1_ppu_word *)P)[1] = palette[pens >> 8];
            P += 8;
        }
'''
    fetch = '''
        {
            unsigned vadr = ((unsigned)names[x] << 4) + vofs;
            const uint8_t *pattern = VRAMADR(vadr);
            at = (at >> 2) | (cc << 2);
            s0 = (s0 << 8) | pattern[0];
            s1 = (s1 << 8) | pattern[8];
            ++x; ++first;
        }
'''
    body = '''/* Plain displayed spans have no callback between tile fetches.
 * Snapshot fine-X and the shifters, split at the horizontal nametable edge,
 * and reuse the common attribute of adjacent even/odd tiles. Midline CPU,
 * CHR-bank, palette and scroll changes are re-read on the next span call.
 * Mapper hooks, MMC5, PEC586 and HD-pack observers use the original loop. */
static uint8_t *H1_PPU_PlainSpan(uint8_t *P, uint32_t *shifts,
                               uint32_t *attributes, uint32_t *address,
                               int first, int end, unsigned vofs) {
    typedef uint32_t h1_ppu_word __attribute__((may_alias));
    uint32_t s0 = shifts[0], s1 = shifts[1], at = *attributes, addr = *address;
    const unsigned offset = XOffset, fine = 8 - offset;
    /* The first two pipeline stages fetch but emit no pixels. */
    while (first < 2 && first < end) {
        const uint8_t *table = vnapage[(addr >> 10) & 3];
        unsigned x = addr & 31;
        unsigned vadr = ((unsigned)table[addr & 0x3ff] << 4) + vofs;
        const uint8_t *pattern = VRAMADR(vadr);
        unsigned cc = (table[0x3c0 + (x >> 2) + ((addr & 0x380) >> 4)] >>
                       ((x & 2) + ((addr & 0x40) >> 4))) & 3;
        at = (at >> 2) | (cc << 2);
        s0 = (s0 << 8) | pattern[0]; s1 = (s1 << 8) | pattern[8];
        addr = x == 31 ? addr ^ 0x41f : addr + 1;
        ++first;
    }
    while (first < end) {
        const uint8_t *table = vnapage[(addr >> 10) & 3];
        const uint8_t *names = table + (addr & 0x3e0);
        const uint8_t *attrs = table + 0x3c0 + ((addr & 0x380) >> 4);
        unsigned x = addr & 31, yshift = (addr & 0x40) >> 4;
        unsigned count = end - first, cc;
        if (count > 32 - x) count = 32 - x;
        if (x & 1) {
            cc = (attrs[x >> 2] >> ((x & 2) + yshift)) & 3;
''' + emit + fetch + '''
            --count;
        }
        while (count >= 2) {
            cc = (attrs[x >> 2] >> ((x & 2) + yshift)) & 3;
''' + emit + fetch + emit + fetch + '''
            count -= 2;
        }
        if (count) {
            cc = (attrs[x >> 2] >> ((x & 2) + yshift)) & 3;
''' + emit + fetch + '''
        }
        addr = (addr & ~31u) | (x & 31);
        if (x == 32) addr ^= 0x400;
    }
    shifts[0] = s0; shifts[1] = s1; *attributes = at; *address = addr;
    return P;
}

'''
    if compact:
        body = body.replace('H1_PPU_PlainSpan(', 'H1_PPU_AlignedSpan(')
        body = body.replace('    const unsigned offset = XOffset, fine = 8 - offset;\n', '')
    return body


def prepare_ppu(core: Path) -> None:
    path = core / "src/fceu-memory.h"
    text = path.read_text(encoding="utf-8")
    # The upstream macro assumes ordinary hosted memcpy recognition. H1's
    # -ffreestanding -fno-builtin otherwise emits a call per four bytes.
    # An explicit builtin retains the defined alias/alignment behavior and
    # works for both PPU row buffers and mapper initialization buffers.
    text = _once(text, "memcpy((uint8_t *)(d) + _fcdms_x, &_fcdms_v, 4);",
                 "__builtin_memcpy((uint8_t *)(d) + _fcdms_x, &_fcdms_v, 4);")
    path.write_text(text, encoding="utf-8", newline="\n")

    path = core / "src/pputile.h"
    text = path.read_text(encoding="utf-8")
    begin = text.index("\t/* Per-tile palette gather:")
    end = text.index("\n\t}\n\tP += 8;", begin)
    # ResetRL starts at XBuf + scanline*256. P only advances by eight;
    # the upstream renderer already requires aligned words at Plinef.
    # Avoid paired SWL/SWR stores and 64-bit packing on this 32-bit CPU.
    text = text[:begin] + '''\t/* P is word aligned throughout every tile span. */
\t{
\t\ttypedef uint32_t h1_ppu_word __attribute__((may_alias));
\t\tuint32_t p0 = fceu_bg_pair_lut[pixdata & 0xFF];
\t\tuint32_t p1 = fceu_bg_pair_lut[(pixdata >> 8) & 0xFF];
\t\tuint32_t p2 = fceu_bg_pair_lut[(pixdata >> 16) & 0xFF];
\t\tuint32_t p3 = fceu_bg_pair_lut[(pixdata >> 24) & 0xFF];
#ifdef MSB_FIRST
\t\t((h1_ppu_word *)P)[0] = (p0 << 16) | p1;
\t\t((h1_ppu_word *)P)[1] = (p2 << 16) | p3;
#else
\t\t((h1_ppu_word *)P)[0] = p0 | (p1 << 16);
\t\t((h1_ppu_word *)P)[1] = p2 | (p3 << 16);
#endif
\t}''' + text[end:]
    path.write_text(text, encoding="utf-8", newline="\n")

    path = core / "src/ppu.c"
    text = path.read_text(encoding="utf-8")
    # H1's environment ignores libretro memory-map exports (app.c). In-core
    # OAM writes use B2004 (including B4014 DMA), power, or state restore.
    # Unknown observers revoke the
    # interval below.  A dirty interval keeps the original content comparison,
    # so repeated writes of unchanged bytes do not rebuild the row index.
    text = _once(text, 'static DECLFW(B2004) {',
                 'static unsigned h1_sprite_oam_dirty = 1;\n'
                 'void H1_PPU_InvalidateOAM(void) { h1_sprite_oam_dirty = 1; }\n'
                 'static DECLFW(B2004) {\n\th1_sprite_oam_dirty = 1;')
    text = _once(text, 'void FCEUPPU_Reset(void) {',
                 'void FCEUPPU_Reset(void) {\n\th1_sprite_oam_dirty = 1;')
    text = _once(text, 'void FCEUPPU_LoadState(int version) {',
                 'void FCEUPPU_LoadState(int version) {\n\th1_sprite_oam_dirty = 1;')
    text = _once(text, 'int FCEUPPU_Loop(int skip) {',
                 'int FCEUPPU_Loop(int skip) {\n\th1_sprite_oam_dirty = 1;')
    # These fixed-size copies otherwise call freestanding memcpy/memcmp.
    # Explicit builtins are alias/alignment safe and lower to bounded loads.
    text = _once(text, "!memcmp(cached_palette, PALRAM, 16)",
                 "!__builtin_memcmp(cached_palette, PALRAM, 16)")
    text = _once(text, "memcpy(cached_palette, PALRAM, 16);",
                 "__builtin_memcpy(cached_palette, PALRAM, 16);")
    # Compact two-bit pens keep one byte per four pixels. The palette table
    # captures the same temporary priority bits as the original pair LUT.
    # Both tables are rebuilt from values, never cached CHR/bank pointers.
    text = _once(text, 'static uint32_t ppulut1[256];',
                 'static uint16_t h1_bg_plane2[256];\n'
                 'static uint32_t h1_bg_quad_lut[1024];\n'
                 'static uint32_t ppulut1[256];')
    text = _once(text, '\t\tppulut2[x] = ppulut1[x] << 1;',
                 '\t\tppulut2[x] = ppulut1[x] << 1;\n'
                 '\t\th1_bg_plane2[x] = 0;\n'
                 '\t\tfor (y = 0; y < 8; ++y)\n'
                 '\t\t\th1_bg_plane2[x] |= ((x >> (7 - y)) & 1) << (y * 2);')
    text = _once(text, '\tvalid = 1;\n\tfor (b = 0;', '''\tvalid = 1;
    for (b = 0; b < 1024; ++b) {
        unsigned p = (b >> 8) << 2;
#ifdef MSB_FIRST
        h1_bg_quad_lut[b] = ((uint32_t)PALRAM[p | (b & 3)] << 24) |
            ((uint32_t)PALRAM[p | ((b >> 2) & 3)] << 16) |
            ((uint32_t)PALRAM[p | ((b >> 4) & 3)] << 8) |
            PALRAM[p | ((b >> 6) & 3)];
#else
        h1_bg_quad_lut[b] = PALRAM[p | (b & 3)] |
            ((uint32_t)PALRAM[p | ((b >> 2) & 3)] << 8) |
            ((uint32_t)PALRAM[p | ((b >> 4) & 3)] << 16) |
            ((uint32_t)PALRAM[p | ((b >> 6) & 3)] << 24);
#endif
    }
\tfor (b = 0;''')
    old = "memcpy(&SPRBUF[ns << 2], &dst, 4);"
    if text.count(old) != 2:
        raise RuntimeError("Pinned sprite copy context mismatch")
    text = text.replace(old, "__builtin_memcpy(&SPRBUF[ns << 2], &dst, 4);")

    # When output is dropped and sprite zero is no longer a candidate,
    # a plain tile fetch has no observer: CPU execution is suspended,
    # reads use RAM/CHR pointers, and there is no mapper/HD/input callback.
    # Keep the last FOUR fetches, not just the two needed for the visible
    # shift window, so all 32 bits of both shift registers remain exact.
    # The attribute latch carries only two attributes. No live register,
    # hook, sprite evaluation, or vertical-scroll boundary is omitted.
    plain = '''\t} else {
\t\tif (PEC586Hack) {
\t\t\t#define PPU_BGFETCH'''
    fast = '''\t} else {
\t\tint firstfetch = firsttile;
\t\tif (h1_skip_colour_output && sphitx == 0x100 && !PEC586Hack
#ifdef HAVE_HDPACK
\t\t    && !hdnes_active
#endif
\t\t) {
\t\t\tint omitted = lasttile - firsttile - 4;
\t\t\tif (omitted > 0) {
\t\t\t\tunsigned coarse = (RefreshAddr & 0x1f) + omitted;
\t\t\t\tfirstfetch += omitted;
\t\t\t\tRefreshAddr = (RefreshAddr & ~0x1f) | (coarse & 0x1f);
\t\t\t\tRefreshAddr ^= (coarse & 0x20) << 5;
\t\t\t\tP += ((firstfetch > 2 ? firstfetch - 2 : 0) -
\t\t\t\t      (firsttile > 2 ? firsttile - 2 : 0)) * 8;
\t\t\t}
\t\t}
\t\tif (PEC586Hack) {
\t\t\t#define PPU_BGFETCH'''
    text = _once(text, plain, fast)
    plain_end = '''\t\t} else {
\t\t\tfor (X1 = firsttile; X1 < lasttile; X1++) {
\t\t\t\t#include "pputile.h"
\t\t\t}
\t\t}
\t}

#undef vofs'''
    text = _once(text, plain_end,
                 plain_end.replace("X1 = firsttile", "X1 = firstfetch"))
    displayed = '''\t\t} else if (!h1_skip_colour_output && lasttile - firstfetch >= 4
#ifdef HAVE_HDPACK
\t\t           && !hdnes_active
#endif
\t\t) {
\t\t\tif (!XOffset)
\t\t\t\tP = H1_PPU_AlignedSpan(P, pshift, &atlatch, &RefreshAddr,
\t\t\t\t                       firstfetch, lasttile, vofs);
\t\t\telse
\t\t\t\tP = H1_PPU_PlainSpan(P, pshift, &atlatch, &RefreshAddr,
\t\t\t\t                     firstfetch, lasttile, vofs);
\t\t} else {
\t\t\tfor (X1 = firstfetch; X1 < lasttile; X1++) {'''
    text = _once(text,
                 '\t\t} else {\n\t\t\tfor (X1 = firstfetch; X1 < lasttile; X1++) {',
                 displayed)
    text = _once(text, '/* lasttile is really "second to last tile." */',
                 _plain_span() + _plain_span(True) +
                 '/* lasttile is really "second to last tile." */')
    # A skipped output with no sprite-zero candidate never reads the LUT.
    # Hooks keep the old palette mutation/restore ordering below; the LUT
    # is lazily revalidated at its next actual consumer.
    text = _once(text, "\tFCEU_BuildBgPairLUT();",
                 "\tif (!h1_skip_colour_output || sphitx != 0x100)\n"
                 "\t\tFCEU_BuildBgPairLUT();")

    helper = '''/* Colour conversion is after sprite hit/composition and before the
 * next CPU/event boundary. Merge the original grayscale, marker and
 * emphasis passes, processing four words per loop without changing bytes.
 * InputScanlineHook has already observed the original pre-colour pixels. */
static void H1_PPU_ColourLine(uint8_t *pixels, uint8_t *emphasis, uint8_t control) {
\ttypedef uint32_t h1_ppu_word __attribute__((may_alias));
\th1_ppu_word *p = (h1_ppu_word *)pixels;
\th1_ppu_word *d = (h1_ppu_word *)emphasis;
\tuint32_t mask = ((control & 0x18) && (control & 1)) ? 0x30303030u : ~0u;
\tuint32_t bits;
\tuint32_t colour = (uint32_t)(control >> 5) * 0x01010101u;
\tunsigned x;
\tif ((control >> 5) == 7) { mask &= 0x3f3f3f3fu; bits = 0xc0c0c0c0u; }
\telse if (control & 0xe0) bits = 0x40404040u;
\telse { mask &= 0x3f3f3f3fu; bits = 0x80808080u; }
\tfor (x = 0; x < 64; x += 4) {
\t\tp[x] = (p[x] & mask) | bits;
\t\tp[x + 1] = (p[x + 1] & mask) | bits;
\t\tp[x + 2] = (p[x + 2] & mask) | bits;
\t\tp[x + 3] = (p[x + 3] & mask) | bits;
\t\td[x] = colour; d[x + 1] = colour;
\t\td[x + 2] = colour; d[x + 3] = colour;
\t}
}

'''
    text = _once(text, "static void DoLine(void)\n{", helper + "static void DoLine(void)\n{")
    text = _once(text, "\tint x, colour_emphasis;", "")
    begin = text.index("\tif (!h1_skip_colour_output) {\n\tif (ScreenON || SpriteON)")
    end = text.index("\n    sphitx = 0x100;", begin)
    text = text[:begin] + "\tif (!h1_skip_colour_output)\n\t\tH1_PPU_ColourLine(target, dtarget, PPU[1]);\n" + text[end:]

    # Sprite compositing has no CPU or mapper callback in its interior. Keep
    # evaluation, fetched bytes, sprite-zero tests and the complete sprite row
    # unchanged, but visit only words touched by nontransparent sprites. The
    # per-byte high bits express transparency/priority independently, allowing
    # four exact decisions in ordinary MIPS32 integer operations. Bounds live
    # with sprlinebuf: the dropped-frame branch preserves both, and the existing
    # spork flag/pre-render reset still controls whether either is consumed.
    text = _once(text, 'static int spork = 0;',
                 'static int spork = 0;\n'
                 'static unsigned h1_sprite_first, h1_sprite_end;')
    text = _once(text, '\tFCEU_dwmemset(sprlinebuf, 0x80808080, 256);',
                 '\th1_sprite_first = 256;\n\th1_sprite_end = 0;\n'
                 '\tFCEU_dwmemset(sprlinebuf, 0x80808080, 256);')
    text = _once(text, '\t\tif (J) {\n\t\t\tif (n == 0',
                 '\t\tif (J) {\n'
                 '\t\t\tunsigned first = (unsigned)x & ~3u;\n'
                 '\t\t\tunsigned end = ((unsigned)x + 11u) & ~3u;\n'
                 '\t\t\tif (end > 256) end = 256;\n'
                 '\t\t\tif (first < h1_sprite_first) h1_sprite_first = first;\n'
                 '\t\t\tif (end > h1_sprite_end) h1_sprite_end = end;\n'
                 '\t\t\tif (n == 0')
    begin = text.index('static void CopySprites(uint8_t *target) {')
    end = text.index('\nvoid FCEUPPU_SetVideoSystem', begin)
    text = text[:begin] + '''static void CopySprites(uint8_t *target) {
    typedef uint32_t h1_ppu_word __attribute__((may_alias));
    unsigned first = ((PPU[1] & 4) ^ 4) << 1;
    unsigned n;
    if (!spork) return;
    spork = 0;
    if (rendis & 1) return;
    if (first < h1_sprite_first) first = h1_sprite_first;
    for (n = first; n < h1_sprite_end; n += 4) {
        uint32_t sprite = *(const h1_ppu_word *)(sprlinebuf + n);
        if (sprite != 0x80808080u) {
            uint32_t background = *(h1_ppu_word *)(target + n);
            uint32_t select = (~sprite & (~(sprite << 1) | (background << 1))) & 0x80808080u;
            select = (select >> 7) * 255u;
            *(h1_ppu_word *)(target + n) = background ^ ((background ^ sprite) & select);
        }
    }
}
''' + text[end:]
    text = _once(text, 'static void FetchSpriteData(void) {', r'''/* OAM bytes are validated after writes, restore and frame/observer boundaries.
 * An interval with no writer reuses this derived index without rereading OAM.
 * It includes all 64 sprite candidates in their original priority order.
 * CHR fetches and sprite overflow are still performed by the original body.
 * OAM writers and state restore revoke this derived cache's clean interval.
 * Unknown observers always use the original path and revoke that interval.
 * Changed content uses the original scan. Build an index only after a second
 * fetch sees the same snapshot, avoiding repeated work with dynamic OAM. */
static uint32_t h1_sprite_rows[240][2];
static uint8_t h1_sprite_oam[256];
static unsigned h1_sprite_height, h1_sprite_rows_valid;
static int H1_PPU_SpriteMask(uint32_t *lo, uint32_t *hi, unsigned height) {
    unsigned index;
    if ((unsigned)scanline >= 240
#ifdef HAVE_HDPACK
        || hdnes_active
#endif
    ) { h1_sprite_oam_dirty = 1; return 0; }
    if (InputScanlineHook || GameHBIRQHook || GameHBIRQHook2)
        { h1_sprite_oam_dirty = 1; return 0; }
    if (h1_sprite_oam_dirty || h1_sprite_height != height) {
        h1_sprite_oam_dirty = 0;
        if (h1_sprite_height != height || memcmp(h1_sprite_oam, SPRAM, 256)) {
            memcpy(h1_sprite_oam, SPRAM, 256);
            h1_sprite_height = height;
            h1_sprite_rows_valid = 0;
            return 0;
        }
    }
    if (!h1_sprite_rows_valid) {
        memset(h1_sprite_rows, 0, sizeof h1_sprite_rows);
        for (index = 0; index < 64; ++index) {
            unsigned row = SPRAM[index * 4], end = row + height;
            uint32_t bit = 1u << (index & 31);
            if (end > 240) end = 240;
            for (; row < end; ++row) h1_sprite_rows[row][index >> 5] |= bit;
        }
        h1_sprite_rows_valid = 1;
        h1_sprite_height = height;
    }
    *lo = h1_sprite_rows[scanline][0];
    *hi = h1_sprite_rows[scanline][1];
    return 1;
}
static void FetchSpriteData(void) {''')
    text = _once(text, '\tif (!PPU_hook)\n\t\tfor (n = 63; n >= 0; n--, spr++) {', r'''	if (!PPU_hook) {
        uint32_t lo = 0, hi = 0;
        unsigned base = 0;
        int indexed = H1_PPU_SpriteMask(&lo, &hi, H);
        for (n = 63; n >= 0; n--, spr++) {
            if (indexed) {
                unsigned index;
                if (!lo) {
                    if (!hi) break;
                    lo = hi; hi = 0; base = 32;
                }
                index = base + __builtin_ctz(lo);
                lo &= lo - 1;
                spr = (SPR *)SPRAM + index;
                n = 63 - index;
            }''')
    text = _once(text, '\t\t}\n\telse\n\t\tfor (n = 63; n >= 0; n--, spr++) {',
                 '\t\t}\n\t} else\n\t\tfor (n = 63; n >= 0; n--, spr++) {\n'
                 '\t\t\th1_sprite_oam_dirty = 1;')
    # Only the dropped line's pixel initialization is redundant. Keep the
    # original fill whenever sprite-zero logic or an external observer can
    # consume those bytes; all ResetRL state and callbacks remain intact.
    for declaration in ('static unsigned h1_skip_colour_output;',
                        'static int32_t sphitx;'):
        text = _once(text, declaration, '')
    text = _once(text,
                 'static void ResetRL(uint8_t *target) {\n\tmemset(target, 0xFF, 256);',
                 '''static unsigned h1_skip_colour_output;
static int32_t sphitx;

static void ResetRL(uint8_t *target) {
    /* An unobserved dropped line has no consumer of these pixel bytes.
     * Sprite-zero lines and every external observer keep the original fill.
     * All line state, timing, fetches and callbacks below remain unchanged. */
    if (!h1_skip_colour_output || sphitx != 0x100 || InputScanlineHook ||
        PPU_hook || MMC5Hack || PEC586Hack || GameHBIRQHook || GameHBIRQHook2
#ifdef HAVE_HDPACK
        || hdnes_active
#endif
       ) memset(target, 0xFF, 256);''')
    text = _protect_sprite_observer_bounds(text)
    path.write_text(text, encoding="utf-8", newline="\n")

    # A malformed state may partially replace OAM and return before the normal
    # successful-load callback. Revoke the interval before any chunks are read.
    path = core / "src/state.c"
    text = path.read_text(encoding="utf-8")
    text = _once(text, '   x = ReadStateChunks(mem, totalsize);',
                 '   { extern void H1_PPU_InvalidateOAM(void);\n'
                 '     H1_PPU_InvalidateOAM(); }\n'
                 '   x = ReadStateChunks(mem, totalsize);')
    path.write_text(text, encoding="utf-8", newline="\n")
