#ifndef H1_DIAGNOSTIC_REPLAY_H
#define H1_DIAGNOSTIC_REPLAY_H
#include <stdint.h>

#ifndef H1_DIAGNOSTIC
#define H1_DIAGNOSTIC 0
#endif

/* This controller owns only the test schedule. The application owns ROM
 * identity, state serialization, CPU mode changes, sound, clocks and logging.
 * A normal build cannot start a diagnostic replay. */
enum h1_replay_stage {
    H1_REPLAY_DISABLED, H1_REPLAY_UNSUPPORTED, H1_REPLAY_BOOT,
    H1_REPLAY_SAVE, H1_REPLAY_RESTORE, H1_REPLAY_RUN,
    H1_REPLAY_DONE, H1_REPLAY_ABORTED, H1_REPLAY_ERROR
};
enum h1_replay_control {
    H1_REPLAY_CONTROL_NONE, H1_REPLAY_CONTROL_SAVE,
    H1_REPLAY_CONTROL_RESTORE
};
enum h1_replay_mode {
    H1_REPLAY_INTERPRETER, H1_REPLAY_JIT, H1_REPLAY_JIT_HLE
};
enum h1_replay_sampling {
    H1_REPLAY_LIGHT, H1_REPLAY_DETAIL, H1_REPLAY_RTC_ONLY
};
enum {
    H1_REPLAY_BOOT_FRAMES = 1500,
    H1_REPLAY_WARMUP_FRAMES = 300,
    H1_REPLAY_RUN_FRAMES = 1800,
    H1_REPLAY_GROUPS = 15,
    H1_REPLAY_TOTAL_FRAMES = H1_REPLAY_BOOT_FRAMES +
                             H1_REPLAY_GROUPS * H1_REPLAY_RUN_FRAMES,
    /* Libretro joypad bit positions, independent of physical key mappings. */
    H1_REPLAY_BUTTON_START = 1u << 3,
    H1_REPLAY_BUTTON_A = 1u << 8
};

typedef struct {
    unsigned stage, group_index, completed, total_completed;
} h1_replay;

typedef struct {
    unsigned stage, control, mode;
    unsigned group_id;    /* 1..15; 0 during boot and snapshot save. */
    unsigned group_total;
    unsigned repeat;      /* 1..3; detail/RTC controls accompany repeat 1. */
    unsigned frame;       /* Next frame, 1-based; 0 at a control boundary. */
    unsigned completed;   /* Completed frames in the current boot/run. */
    unsigned total_completed;
    unsigned measurement; /* Only run frames 301..1800. */
    unsigned sampling;    /* LIGHT / DETAIL / RTC_ONLY; see enum above. */
    uint16_t buttons;     /* The complete input mask, never OR with real keys. */
} h1_replay_view;

/* exact_rom is a verified whole-ROM identity, not merely mapper == 19.
 * Call before any battery/state load; BOOT must start from a fresh ROM. */
void h1_replay_init(h1_replay *replay, int exact_rom);
void h1_replay_read(const h1_replay *replay, h1_replay_view *view);

/* Called once after a successfully completed core frame. Returns 0 if no
 * frame was permitted. SAVE/RESTORE boundaries never advance implicitly. */
int h1_replay_frame_done(h1_replay *replay);

/* SAVE: serialize frame 1500 into RAM. RESTORE: restore that same snapshot,
 * choose view.mode, reset app frame/display phase and group counters, and
 * restore the same audio/synchronization starting conditions. Do not ack
 * until every step succeeds. success == 0 ends the experiment as ERROR. */
int h1_replay_control_done(h1_replay *replay, int success);

/* The app routes P/Escape to this function before emulating another frame.
 * Other physical input must never alter the deterministic replay. */
void h1_replay_abort(h1_replay *replay);
const char *h1_replay_stage_name(unsigned stage);
const char *h1_replay_mode_name(unsigned mode);

#endif
