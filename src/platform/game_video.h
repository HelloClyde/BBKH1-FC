#ifndef H1_GBA_GAME_VIDEO_H
#define H1_GBA_GAME_VIDEO_H
#include "h1_sdk.h"
#include "diagnostics.h"

/* V1.41 GUI+0x8F4 supplies the native game's current 32-bit LCD buffer.
 * Its six u16 fields include width[1], height[2], stride[3], bpp[5].
 * GUI+0x070 deliberately skips refresh while the native game window is open. */
static inline volatile uint32_t *gba_game_buffer(int trace)
{
    typedef volatile uint32_t *(*function_type)(uint16_t *, void *);
    function_type fn = (function_type)h1_runtime_entry(
        h1_runtime_table(H1_RUNTIME_GUI_TABLE_SLOT), 0x8f4u);
    uint16_t info[6] = {0};
    volatile uint32_t *buffer = fn ? fn(info, 0) : 0;
    if (trace) h1_diag("GAME_FRAMEBUFFER ptr=%p width=%u height=%u stride=%u bpp=%u",
                      (void *)buffer, info[1], info[2], info[3], info[5]);
    if (!buffer || info[1] != 480 || info[2] != 272 || info[3] != 1920 || info[5] != 32)
        return 0;
    return buffer;
}
static inline int gba_game_present(const uint16_t *pixels, int trace)
{
    volatile uint32_t *buffer = gba_game_buffer(trace);
    unsigned i;
    if (!buffer) return -1;
    for (i = 0; i < 480u * 272u; ++i) {
        uint32_t p = pixels[i];
        buffer[i] = ((p & 0xf800u) << 8) | ((p & 0x07e0u) << 5) | ((p & 0x001fu) << 3);
    }
    return 0;
}
#endif
