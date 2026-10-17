#ifndef H1_NES_VIDEO_H
#define H1_NES_VIDEO_H
#include <stdint.h>
#include <stddef.h>
/* Synchronous view of the core's completed frame. Palette entries retain
 * the original RGB565 quantization; emphasis is constant within each row. */
typedef struct {
    const uint8_t *pixels, *emphasis;
    const uint32_t *palette;
    unsigned width, height, pitch, pixel_mask;
} h1_nes_video_frame;
typedef void (*h1_nes_indexed_callback)(const h1_nes_video_frame *);
void h1_nes_set_indexed_video(h1_nes_indexed_callback);
int h1_nes_present_indexed(const h1_nes_video_frame *);
void scale_nes_indexed_rgb32(uint32_t *, const h1_nes_video_frame *, int scale, int clear);
#endif
