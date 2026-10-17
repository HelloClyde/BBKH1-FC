#ifndef H1_JIT_BACKOFF_H
#define H1_JIT_BACKOFF_H
#include <stdint.h>
#ifndef H1_NATIVE_JIT
#define H1_NATIVE_JIT 0
#endif
typedef struct {
    uint32_t initialized, mode, cooldown_left, next_delay;
    uint32_t probe_frames, stable_frames, active_frames;
    uint32_t last_epoch, last_wraps, last_compiles, last_emitted;
    uint32_t have_wrap, wrap_frame, wrap_compiles, wrap_emitted;
    uint32_t probe_compiles, probe_emitted, trips, probes;
    uint32_t last_invalidations, frame_active;
    uint32_t cheap_streak, recovery_left, budget_active, budget_hit;
    uint32_t frame_compiles, frame_emitted, frame_native_ops;
    uint32_t frame_wraps, frame_invalidations, frame_epoch;
} h1_jit_backoff_policy;
typedef struct {
    uint32_t dispatch_disabled, emitted_bytes;
    h1_jit_backoff_policy policy;
} h1_jit_backoff_context;
extern h1_jit_backoff_context h1_jit_backoff_state;
#define h1_jit_dispatch_disabled (h1_jit_backoff_state.dispatch_disabled)
#define h1_jit_emitted_bytes (h1_jit_backoff_state.emitted_bytes)
#endif
