"""Add sampled, nested PPU scopes after every functional core preparation.

This pass is only for diagnostic builds.  It preserves each original entry
point and moves its body behind a wrapper, so early returns and recursive
PPU updates cannot leave a profiling scope open.  The profiler owns sampling,
exclusive nested attribution, and invalid-clock handling.
"""
from pathlib import Path


def _once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise RuntimeError(f"Pinned profile PPU context mismatch: {old[:100]!r}")
    return text.replace(old, new, 1)


def _wrap_void(text: str, name: str, parameters: str, arguments: str,
               kind: str) -> str:
    body = f"H1_Profile_{name}Body"
    signature = f"static void {name}({parameters})"
    replacement = (
        f"static void {body}({parameters});\n"
        f"{signature} {{\n"
        f"\tunsigned token = h1_profile_detail_enter(HPD_PPU, {kind});\n"
        f"\t{body}({arguments});\n"
        "\th1_profile_detail_leave(token);\n"
        "}\n"
        f"static void {body}({parameters}) {{"
    )
    return _once(text, signature + " {", replacement)


def prepare_profile_ppu(core: Path) -> None:
    """Instrument the already prepared core, once, for an H1_PROFILE build."""
    path = Path(core) / "src/ppu.c"
    text = path.read_text(encoding="utf-8")
    if text.count('#include "platform/profile.h"') != 1:
        raise RuntimeError("Profile PPU pass requires the prepared profile header")

    # The old wrapper counts only background work and calls it HP_RENDER.
    # Replace it rather than nesting a second timer around the same body.
    text = _once(text, '''static void FASTAPASS(1) RefreshLine(int lastpixel) {
\tif (h1_profile_ppu_sample) {
\t\th1_profile_push(HP_RENDER);
\t\tRefreshLineBody(lastpixel);
\t\th1_profile_pop();
\t} else RefreshLineBody(lastpixel);
}''', '''static void FASTAPASS(1) RefreshLine(int lastpixel) {
\tunsigned token = h1_profile_detail_enter(HPD_PPU, HP_PPU_BG_SAMPLE);
\tRefreshLineBody(lastpixel);
\th1_profile_detail_leave(token);
}''')

    # CPU is present in every detail mode.  Its nested scopes subtract CPU
    # runs from this outer frame scope, including non-displayed frames.
    # Unwrapped mapper/input hooks and FCEU_PutImage remain in their caller's
    # PPU category; this scope must not be described as pure pixel rendering.
    text = _once(text, "int FCEUPPU_Loop(int skip) {", '''static int H1_Profile_FCEUPPU_LoopBody(int skip);
int FCEUPPU_Loop(int skip) {
\tunsigned token = h1_profile_detail_enter(HPD_CPU, HP_PPU_SAMPLE);
\tint result = H1_Profile_FCEUPPU_LoopBody(skip);
\th1_profile_detail_leave(token);
\treturn result;
}
static int H1_Profile_FCEUPPU_LoopBody(int skip) {''')

    # Whole sprite stages only.  In particular, do not instrument individual
    # sprite pixels, tile reads, or PPU_hook calls made during fetches.
    for name, parameters, arguments in (
        ("FetchSpriteData", "void", ""),
        ("RefreshSprites", "void", ""),
        ("CopySprites", "uint8_t *target", "target"),
        ("CheckSpriteHit", "int p", "p"),
    ):
        text = _wrap_void(text, name, parameters, arguments,
                          "HP_PPU_SPRITE_SAMPLE")

    # ResetRL includes row clearing/setup; any nested background update gets
    # its own scope.  ColourLine includes the complete final row conversion.
    for name, parameters, arguments in (
        ("ResetRL", "uint8_t *target", "target"),
        ("H1_PPU_ColourLine",
         "uint8_t *pixels, uint8_t *emphasis, uint8_t control",
         "pixels, emphasis, control"),
    ):
        text = _wrap_void(text, name, parameters, arguments,
                          "HP_PPU_PIXEL_SAMPLE")

    path.write_text(text, encoding="utf-8", newline="\n")
