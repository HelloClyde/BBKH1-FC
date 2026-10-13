#ifndef H1_PROFILE_H
#define H1_PROFILE_H
#include <stdint.h>
#ifndef H1_PROFILE
#define H1_PROFILE 0
#endif
enum h1_profile_kind { HP_CORE, HP_RENDER, HP_MIX, HP_VIDEO, HP_AUDIO, HP_INPUT,
    HP_ROM_IO, HP_JIT, HP_SAVE, HP_WAIT, HP_OTHER, HP_COUNT };
#ifdef __cplusplus
extern "C" {
#endif
#if H1_PROFILE
uint32_t h1_profile_now(void);
void h1_profile_begin(void);
void h1_profile_start(void);
void h1_profile_push(unsigned kind);
void h1_profile_pop(void);
void h1_profile_frame(uint32_t core_ticks, unsigned displayed);
void h1_profile_video(uint32_t buffer_ticks, uint32_t scale_ticks);
void h1_profile_dump(const char *reason);
#else
static inline uint32_t h1_profile_now(void) { return 0; }
static inline void h1_profile_begin(void) {}
static inline void h1_profile_start(void) {}
static inline void h1_profile_push(unsigned kind) { (void)kind; }
static inline void h1_profile_pop(void) {}
static inline void h1_profile_frame(uint32_t ticks, unsigned displayed) { (void)ticks; (void)displayed; }
static inline void h1_profile_video(uint32_t buffer_ticks, uint32_t scale_ticks) { (void)buffer_ticks;(void)scale_ticks; }
static inline void h1_profile_dump(const char *reason) { (void)reason; }
#endif
#ifdef __cplusplus
}
#endif
#endif
