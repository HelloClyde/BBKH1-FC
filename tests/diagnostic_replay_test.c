#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "../src/platform/diagnostic_replay.h"

static h1_replay_view read_view(const h1_replay *replay)
{
    h1_replay_view view;
    h1_replay_read(replay, &view);
    assert(view.group_total == H1_REPLAY_GROUPS);
    return view;
}

static void assert_terminal(h1_replay *replay, unsigned stage)
{
    h1_replay saved = *replay;
    h1_replay_view view = read_view(replay);
    assert(view.stage == stage && !view.frame && !view.buttons);
    assert(!view.control && !view.measurement);
    assert(!h1_replay_frame_done(replay));
    assert(!h1_replay_control_done(replay, 1));
    assert(!h1_replay_control_done(replay, 0));
    h1_replay_abort(replay);
    assert(!memcmp(&saved, replay, sizeof(saved)));
}

#if H1_DIAGNOSTIC
static void assert_abort_and_failure(const h1_replay *replay)
{
    h1_replay copy = *replay;
    h1_replay_view before = read_view(&copy), after;
    h1_replay_abort(&copy);
    assert_terminal(&copy, H1_REPLAY_ABORTED);
    after = read_view(&copy);
    assert(after.total_completed == before.total_completed);
    assert(after.completed == before.completed);
    if (before.control) {
        copy = *replay;
        assert(!h1_replay_frame_done(&copy));
        assert(!memcmp(&copy, replay, sizeof(copy)));
        assert(h1_replay_control_done(&copy, 0));
        assert_terminal(&copy, H1_REPLAY_ERROR);
        after = read_view(&copy);
        assert(after.group_id == before.group_id);
        assert(after.total_completed == before.total_completed);
    } else {
        copy = *replay;
        assert(!h1_replay_control_done(&copy, 1));
        assert(!h1_replay_control_done(&copy, 0));
        assert(!memcmp(&copy, replay, sizeof(copy)));
    }
}

static void test_full_replay(int trace)
{
    h1_replay replay;
    h1_replay_view view;
    unsigned boot_frames = 0, saves = 0, restores = 0, primary_index = 0;
    unsigned run_frames[H1_REPLAY_GROUPS] = {0};
    unsigned measures[3][3] = {{0}}, warmups[3][3] = {{0}};
    unsigned modes[3][3] = {{0}}, all_frames = 0;
    const unsigned primary_modes[] = {0,1,2,2,0,1,1,2,0};
    h1_replay_init(&replay, 1);
    for (;;) {
        view = read_view(&replay);
        assert(view.total_completed == all_frames);
        if (view.stage == H1_REPLAY_DONE) break;
        /* Read-only observation never advances or consumes an input edge. */
        h1_replay saved = replay;
        h1_replay_view twice = read_view(&replay);
        assert(!memcmp(&saved, &replay, sizeof(saved)));
        assert(twice.frame == view.frame && twice.buttons == view.buttons);
        assert_abort_and_failure(&replay);
        if (view.control) {
            assert(!view.frame && !view.buttons && !view.measurement);
            if (view.control == H1_REPLAY_CONTROL_SAVE) {
                assert(view.stage == H1_REPLAY_SAVE && !view.group_id);
                assert(boot_frames == 1500 && !saves && !restores);
                assert(view.completed == 1500);
                ++saves;
            } else {
                assert(view.stage == H1_REPLAY_RESTORE && saves == 1);
                assert(view.group_id == restores + 1 && !view.completed);
                assert(view.mode < 3 && view.sampling < 3);
                assert(view.repeat >= 1 && view.repeat <= 3);
                ++modes[view.mode][view.sampling];
                if (view.sampling == H1_REPLAY_LIGHT) {
                    assert(primary_index < sizeof(primary_modes) /
                                           sizeof(primary_modes[0]));
                    assert(view.mode == primary_modes[primary_index]);
                    assert(view.repeat == primary_index / 3 + 1);
                    ++primary_index;
                } else assert(view.repeat == 1);
                ++restores;
            }
            assert(h1_replay_control_done(&replay, 1));
            continue;
        }
        assert(view.frame == view.completed + 1);
        assert(view.buttons == 0 || view.buttons == H1_REPLAY_BUTTON_START ||
               view.buttons == H1_REPLAY_BUTTON_A);
        if (view.stage == H1_REPLAY_BOOT) {
            assert(view.frame == ++boot_frames && !view.group_id);
            assert(view.mode == H1_REPLAY_INTERPRETER && !view.measurement);
            assert(!view.sampling && view.frame <= 1500);
            if (trace) printf("boot,%u,%u\n", view.frame, view.buttons);
        } else {
            assert(view.stage == H1_REPLAY_RUN && view.group_id);
            assert(view.frame == ++run_frames[view.group_id - 1]);
            assert(view.frame <= 1800);
            assert(view.measurement == (view.frame > 300));
            if (view.measurement) ++measures[view.mode][view.sampling];
            else ++warmups[view.mode][view.sampling];
            if (view.frame > 124) assert(!view.buttons);
            if (trace && view.group_id == 1)
                printf("run,%u,%u\n", view.frame, view.buttons);
        }
        assert(h1_replay_frame_done(&replay));
        ++all_frames;
    }
    assert(saves == 1 && restores == 15 && primary_index == 9);
    assert(all_frames == 28500 && all_frames == H1_REPLAY_TOTAL_FRAMES);
    assert(view.group_id == 15 && view.completed == 1800);
    assert_terminal(&replay, H1_REPLAY_DONE);
    for (unsigned group = 0; group < 15; ++group) assert(run_frames[group] == 1800);
    for (unsigned mode = 0; mode < 3; ++mode) {
        assert(modes[mode][0] == 3 && modes[mode][1] == 1 && modes[mode][2] == 1);
        for (unsigned sampling = 0; sampling < 3; ++sampling) {
            assert(measures[mode][sampling] == modes[mode][sampling] * 1500);
            assert(warmups[mode][sampling] == modes[mode][sampling] * 300);
        }
    }
    h1_replay_init(&replay, 0);
    assert_terminal(&replay, H1_REPLAY_UNSUPPORTED);
    assert(!read_view(&replay).group_id);
    h1_replay_init(&replay, 1);
    assert(read_view(&replay).stage == H1_REPLAY_BOOT);
    assert(read_view(&replay).frame == 1 && !read_view(&replay).total_completed);
}
#endif

int main(int argc, char **argv)
{
    (void)argc; (void)argv;
#if H1_DIAGNOSTIC
    test_full_replay(argc > 1 && !strcmp(argv[1], "--trace"));
    puts("PASS: replay schedule, frame boundaries, failure/abort at every frame");
#else
    h1_replay replay;
    h1_replay_init(&replay, 0);
    assert_terminal(&replay, H1_REPLAY_DISABLED);
    h1_replay_init(&replay, 1);
    assert_terminal(&replay, H1_REPLAY_DISABLED);
    puts("PASS: ordinary builds cannot start a replay");
#endif
    assert(!strcmp(h1_replay_mode_name(H1_REPLAY_INTERPRETER), "interpreter"));
    assert(!strcmp(h1_replay_mode_name(H1_REPLAY_JIT), "jit"));
    assert(!strcmp(h1_replay_mode_name(H1_REPLAY_JIT_HLE), "jit_hle"));
    assert(!strcmp(h1_replay_mode_name(999), "invalid"));
    assert(!strcmp(h1_replay_stage_name(999), "invalid"));
    return 0;
}
