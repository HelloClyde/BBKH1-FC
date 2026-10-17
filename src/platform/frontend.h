#ifndef H1_FRONTEND_H
#define H1_FRONTEND_H
#include <stdint.h>
#include <stddef.h>
#define FC_ROOT "A:\\FC\\"
#define H1_DEFAULT_FRAMESKIP 1
enum { H1_SCALE_NATIVE, H1_SCALE_ASPECT, H1_SCALE_STRETCH, H1_SCALE_LINEAR, H1_SCALE_COUNT };
typedef struct {
    uint8_t held[45], mapping[45];
    unsigned mapping_ready;
    int pause_requested;
    uint16_t mask;
    int exit_requested, menu_requested, scale, skip;
    unsigned scale_changed, skip_changed;
} input_state;
uint32_t crc32_bytes(const void *, size_t);
uint32_t crc32_update(uint32_t, const void *, size_t);
void input_mapping_defaults(input_state *);
int input_map_button(input_state *, unsigned logical_id, unsigned key);
int input_key_reserved(unsigned key);
void input_event(input_state *, int pressed, int key);
void input_frame(input_state *);
void input_poll(input_state *);
void input_discard_held(input_state *);
void scale_frame(uint16_t *, const void *, size_t pitch, int scale);
void scale_frame_dimensions(uint16_t *, const void *, size_t pitch, int scale, unsigned width, unsigned height);
void scale_frame_rgb32(uint32_t *, const void *, size_t pitch, int scale, unsigned width, unsigned height, int clear);
void h1_video_release(void);
#endif
