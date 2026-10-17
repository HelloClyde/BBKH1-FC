#include "diagnostic_replay.h"

#if H1_DIAGNOSTIC
typedef struct { uint8_t mode, repeat, sampling; } replay_group;

/* Light-timer repeats rotate CPU mode order. Each mode also has adjacent
 * detailed and RTC-only runs from the identical snapshot and input sequence.
 * Their order is reversed for mode 1 to expose order sensitivity. Throughput
 * comparisons use light runs only; detail/RTC runs measure instrumentation.
 * Total: 3 modes x (3 light + 1 detail + 1 RTC-only) x 1800 frames. */
static const replay_group groups[H1_REPLAY_GROUPS] = {
    {0,1,2}, {0,1,0}, {0,1,1},
    {1,1,1}, {1,1,0}, {1,1,2},
    {2,1,2}, {2,1,0}, {2,1,1},
    {2,2,0}, {0,2,0}, {1,2,0},
    {1,3,0}, {2,3,0}, {0,3,0}
};

static uint16_t boot_buttons(unsigned frame)
{
    /* tests/scenarios/sgz2-newgame-city.json, frame 1 through 1500. Each
     * release frame is excluded: press 301, release 305 means 301..304. */
    if ((frame >= 301 && frame < 305) ||
        (frame >= 421 && frame < 425) ||
        (frame >= 541 && frame < 545)) return H1_REPLAY_BUTTON_START;
    if ((frame >= 721 && frame < 725) ||
        (frame >= 841 && frame < 845) ||
        (frame >= 1201 && frame < 1205) ||
        (frame >= 1321 && frame < 1325)) return H1_REPLAY_BUTTON_A;
    return 0;
}

static uint16_t run_buttons(unsigned frame)
{
    /* Resume the same script's 1501/1621 A presses from the frame-1500
     * snapshot. Leave the resulting city interface stationary afterwards;
     * blindly looping A could advance the date or execute a purchase. */
    return (frame < 5 || (frame >= 121 && frame < 125)) ?
           H1_REPLAY_BUTTON_A : 0;
}
#endif

void h1_replay_init(h1_replay *replay, int exact_rom)
{
    replay->group_index = H1_REPLAY_GROUPS;
    replay->completed = replay->total_completed = 0;
#if H1_DIAGNOSTIC
    replay->stage = exact_rom ? H1_REPLAY_BOOT : H1_REPLAY_UNSUPPORTED;
#else
    (void)exact_rom;
    replay->stage = H1_REPLAY_DISABLED;
#endif
}

void h1_replay_read(const h1_replay *replay, h1_replay_view *view)
{
    view->stage = replay->stage;
    view->control = H1_REPLAY_CONTROL_NONE;
    view->mode = H1_REPLAY_INTERPRETER;
    view->group_id = view->repeat = view->frame = 0;
    view->group_total = H1_REPLAY_GROUPS;
    view->measurement = view->sampling = 0;
    view->buttons = 0;
    view->completed = replay->completed;
    view->total_completed = replay->total_completed;
#if H1_DIAGNOSTIC
    if (replay->stage == H1_REPLAY_BOOT) {
        view->frame = replay->completed + 1;
        view->buttons = boot_buttons(view->frame);
    } else if (replay->stage == H1_REPLAY_SAVE) {
        view->control = H1_REPLAY_CONTROL_SAVE;
    } else if (replay->stage == H1_REPLAY_RESTORE ||
               replay->stage == H1_REPLAY_RUN ||
               ((replay->stage == H1_REPLAY_ABORTED ||
                 replay->stage == H1_REPLAY_ERROR ||
                 replay->stage == H1_REPLAY_DONE) &&
                replay->group_index < H1_REPLAY_GROUPS)) {
        const replay_group *group = &groups[replay->group_index];
        view->group_id = replay->group_index + 1;
        view->mode = group->mode;
        view->repeat = group->repeat;
        view->sampling = group->sampling;
        if (replay->stage == H1_REPLAY_RESTORE) {
            view->control = H1_REPLAY_CONTROL_RESTORE;
        } else if (replay->stage == H1_REPLAY_RUN) {
            view->frame = replay->completed + 1;
            view->buttons = run_buttons(view->frame);
            view->measurement = view->frame > H1_REPLAY_WARMUP_FRAMES;
        }
    }
#endif
}

int h1_replay_frame_done(h1_replay *replay)
{
#if H1_DIAGNOSTIC
    if (replay->stage != H1_REPLAY_BOOT &&
        replay->stage != H1_REPLAY_RUN) return 0;
    ++replay->completed;
    ++replay->total_completed;
    if (replay->stage == H1_REPLAY_BOOT) {
        if (replay->completed == H1_REPLAY_BOOT_FRAMES)
            replay->stage = H1_REPLAY_SAVE;
    } else if (replay->completed == H1_REPLAY_RUN_FRAMES) {
        if (replay->group_index + 1 == H1_REPLAY_GROUPS) {
            replay->stage = H1_REPLAY_DONE;
        } else {
            ++replay->group_index;
            replay->completed = 0;
            replay->stage = H1_REPLAY_RESTORE;
        }
    }
    return 1;
#else
    (void)replay;
    return 0;
#endif
}

int h1_replay_control_done(h1_replay *replay, int success)
{
#if H1_DIAGNOSTIC
    if (replay->stage != H1_REPLAY_SAVE &&
        replay->stage != H1_REPLAY_RESTORE) return 0;
    if (!success) {
        replay->stage = H1_REPLAY_ERROR;
    } else {
        if (replay->stage == H1_REPLAY_SAVE) replay->group_index = 0;
        replay->stage = replay->stage == H1_REPLAY_SAVE ?
                        H1_REPLAY_RESTORE : H1_REPLAY_RUN;
        replay->completed = 0;
    }
    return 1;
#else
    (void)replay; (void)success;
    return 0;
#endif
}

void h1_replay_abort(h1_replay *replay)
{
#if H1_DIAGNOSTIC
    if (replay->stage == H1_REPLAY_BOOT || replay->stage == H1_REPLAY_SAVE ||
        replay->stage == H1_REPLAY_RESTORE || replay->stage == H1_REPLAY_RUN)
        replay->stage = H1_REPLAY_ABORTED;
#else
    (void)replay;
#endif
}

const char *h1_replay_stage_name(unsigned stage)
{
    static const char *const names[] = {
        "disabled", "unsupported", "boot", "save", "restore", "run",
        "done", "aborted", "error"
    };
    return stage < sizeof(names) / sizeof(names[0]) ? names[stage] : "invalid";
}

const char *h1_replay_mode_name(unsigned mode)
{
    static const char *const names[] = {"interpreter", "jit", "jit_hle"};
    return mode < sizeof(names) / sizeof(names[0]) ? names[mode] : "invalid";
}
