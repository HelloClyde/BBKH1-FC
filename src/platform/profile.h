#ifndef H1_PROFILE_H
#define H1_PROFILE_H
#include <stdint.h>
#ifndef H1_PROFILE
#define H1_PROFILE 0
#endif
#ifndef H1_DIAGNOSTIC
#define H1_DIAGNOSTIC 0
#endif
enum h1_profile_kind { HP_CORE, HP_RENDER, HP_MIX, HP_VIDEO, HP_AUDIO, HP_INPUT,
    HP_ROM_IO, HP_JIT, HP_SAVE, HP_WAIT, HP_OTHER,
    HP_CPU_SAMPLE, HP_NATIVE_SAMPLE, HP_EVENT_SAMPLE, HP_COMPLETE_SAMPLE,
    HP_APU_EVENT_SAMPLE, HP_APU_SYNTH_SAMPLE, HP_MAPPER_SAMPLE,
    HP_PPU_SAMPLE, HP_PPU_BG_SAMPLE, HP_PPU_SPRITE_SAMPLE, HP_PPU_PIXEL_SAMPLE,
    HP_COUNT };
enum h1_profile_detail_mask { HPD_CPU=1u, HPD_NATIVE=2u, HPD_EVENT=4u,
    HPD_PPU=8u, HPD_SOUND=16u };
#ifdef __cplusplus
extern "C" {
#endif
#if H1_PROFILE
/* Legacy preparation symbol; current diagnostic layouts keep it zero.
 * Detail modes rotate every 31 frames and stay fixed throughout each frame. */
extern unsigned h1_profile_ppu_sample;
extern unsigned h1_profile_detail_mask;
uint32_t h1_profile_now(void);
void h1_profile_begin(void);
void h1_profile_start(void);
void h1_profile_push(unsigned kind);
void h1_profile_pop(void);
void h1_profile_frame(uint32_t core_ticks, unsigned displayed);
void h1_profile_video(uint32_t buffer_ticks, uint32_t scale_ticks);
void h1_profile_dump(const char *reason);
#if H1_DIAGNOSTIC
enum h1_profile_diag_sampling { HPDS_LIGHT=0, HPDS_DETAIL=1, HPDS_RTC_ONLY=2 };
/* Begin the group before profile_begin/start. Record complete frames including
 * pacing; end must precede the legacy profile_frame call. These calls never
 * retire PCM, write files, or advance emulated time. Dump after audio stops. */
int h1_profile_diag_group_begin(unsigned group, unsigned sampling,
    unsigned expected_frames, unsigned warmup_frames);
void h1_profile_diag_frame_begin(unsigned frame, unsigned requested_skip,
    uint32_t jit_compiles, uint32_t jit_bytes);
void h1_profile_diag_frame_end(uint32_t core_ticks, unsigned displayed,
    uint32_t jit_compiles, uint32_t jit_bytes);
void h1_profile_diag_dump(const char *reason);
void h1_profile_diag_release(void);
#endif
/* The generated detail wrappers exist only in diagnostic builds. Cache the
 * token across the body: a clock fault may turn the mask off inside it. */
static inline unsigned h1_profile_detail_enter(unsigned mask, unsigned kind)
{
    if (!(h1_profile_detail_mask & mask)) return 0;
    h1_profile_push(kind);
    return 1;
}
static inline void h1_profile_detail_leave(unsigned token)
{ if (token) h1_profile_pop(); }
#else
#define h1_profile_ppu_sample 0u
#define h1_profile_detail_mask 0u
static inline uint32_t h1_profile_now(void) { return 0; }
static inline void h1_profile_begin(void) {}
static inline void h1_profile_start(void) {}
static inline void h1_profile_push(unsigned kind) { (void)kind; }
static inline void h1_profile_pop(void) {}
static inline void h1_profile_frame(uint32_t ticks, unsigned displayed) { (void)ticks; (void)displayed; }
static inline void h1_profile_video(uint32_t buffer_ticks, uint32_t scale_ticks) { (void)buffer_ticks;(void)scale_ticks; }
static inline void h1_profile_dump(const char *reason) { (void)reason; }
static inline unsigned h1_profile_detail_enter(unsigned mask, unsigned kind)
{ (void)mask; (void)kind; return 0; }
static inline void h1_profile_detail_leave(unsigned token) { (void)token; }
#endif
#ifdef __cplusplus
}
#endif
#endif
