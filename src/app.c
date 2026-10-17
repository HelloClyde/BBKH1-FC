#include "h1_sdk.h"
#include "platform/frontend.h"
#include "platform/core.h"
#include "platform/save.h"
#include "platform/file_selector.h"
#include "platform/diagnostics.h"
#include "platform/gui_context.h"
#include "platform/game_video.h"
#include "platform/audio.h"
#include "platform/loading.h"
#include "platform/menu.h"
#include "platform/profile.h"
#include "platform/nes_video.h"
#if H1_DIAGNOSTIC
#include "platform/diagnostic_replay.h"
#include "platform/nes_jit.h"
#include "platform/game_input.h"
#endif
#include <libretro.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const h1_core *core;
static input_state input;
static uint16_t *screen;
static unsigned frames, total_frames, videos, video_errors;
#if !H1_DIAGNOSTIC
static unsigned audio_catchup_frames;
#endif
static unsigned video_callbacks, input_callbacks;
static int variable_changed, profile_waiting;
static volatile uint32_t *last_video_buffer;
#if H1_DIAGNOSTIC
static unsigned diagnostic_running, diagnostic_abort, diagnostic_pcm_error;
static unsigned diagnostic_suppressed_logs;
static uint16_t diagnostic_buttons;
/* Event identity is used only for safe cancellation. Neither the queue nor
 * native held keys is allowed to alter the replay's complete joypad mask. */
static void diagnostic_poll(int held_keys)
{
    for (unsigned n=0;n<128;++n) {
        int code=-1,key=-1;
        h1_event_fetch(&code,&key);
        if (code==-1 && key==-1) break;
        if (code==11 || (code==H1_EVENT_KEY_DOWN &&
            (key==H1_KEY_P || key==H1_KEY_ESCAPE || key==H1_KEY_BACK)))
            diagnostic_abort=1;
    }
    if (held_keys && (h1_game_code_down(h1_game_key_code(H1_KEY_P)) ||
                     h1_game_code_down(h1_game_key_code(H1_KEY_ESCAPE))))
        diagnostic_abort=1;
}
#else
typedef struct { unsigned frame,video,errors,ms; } heartbeat_record;
static heartbeat_record heartbeats[8];
static unsigned heartbeat_count,heartbeat_omitted;
typedef struct { unsigned frame,raw_ticks; int status; } checkpoint_record;
static checkpoint_record checkpoints[8];
static unsigned checkpoint_count,checkpoint_omitted,checkpoint_unchanged;
static void remember_checkpoint(int status,unsigned raw_ticks)
{
    if (checkpoint_count==8) {
        memmove(checkpoints,checkpoints+1,7*sizeof *checkpoints);
        --checkpoint_count;++checkpoint_omitted;
    }
    checkpoints[checkpoint_count++]=(checkpoint_record){total_frames,raw_ticks,status};
}
static void dump_checkpoints(void)
{
    if (!checkpoint_count && !checkpoint_unchanged) return;
    h1_diag_batch_begin();
    h1_diag("SAVE_CHECKPOINT_DUMP count=%u omitted=%u unchanged=%u",checkpoint_count,checkpoint_omitted,checkpoint_unchanged);
    for (unsigned i=0;i<checkpoint_count;++i) {
        checkpoint_record *p=&checkpoints[i];
        h1_diag("SAVE_CHECKPOINT_END status=%d frame=%u raw_ticks=%u deferred=1",p->status,p->frame,p->raw_ticks);
    }
    h1_diag_batch_end();checkpoint_count=checkpoint_omitted=checkpoint_unchanged=0;
}
static void remember_heartbeat(unsigned ms)
{
    if (heartbeat_count==8) {
        memmove(heartbeats,heartbeats+1,7*sizeof *heartbeats);
        --heartbeat_count;++heartbeat_omitted;
    }
    heartbeats[heartbeat_count++]=(heartbeat_record){total_frames,videos,video_errors,ms};
}
static void dump_heartbeats(void)
{
    if (!heartbeat_count) return;
    h1_diag_batch_begin();
    h1_diag("HEARTBEAT_DUMP count=%u omitted=%u",heartbeat_count,heartbeat_omitted);
    for (unsigned i=0;i<heartbeat_count;++i) {
        heartbeat_record *p=&heartbeats[i];
        h1_diag("FRAME_HEARTBEAT n=%u video=%u errors=%u ms=%u deferred=1",p->frame,p->video,p->errors,p->ms);
    }
    h1_diag_batch_end();heartbeat_count=heartbeat_omitted=0;
}
#endif
#ifndef H1_TEST_FRAMES
#define H1_TEST_FRAMES 0
#endif

static void log_line(const char *line)
{
    h1_diag("%s", line);
}
static void core_log(enum retro_log_level level, const char *fmt, ...)
{
    char line[192]; va_list args;
#if H1_DIAGNOSTIC
    if (diagnostic_running) { ++diagnostic_suppressed_logs;return; }
#endif
    if (level < RETRO_LOG_WARN && !h1_diag_is_verbose()) return;
    va_start(args, fmt); vsnprintf(line, sizeof(line), fmt, args); va_end(args);
    log_line(line);
}
static const char *option(const char *key)
{
    if (!strcmp(key, "fceumm_region")) return "Auto";
    if (!strcmp(key, "fceumm_sndrate_hint")) return "22KHz";
    if (!strcmp(key, "fceumm_aspect")) return "PP";
    if (!strcmp(key, "fceumm_sndquality")) return "Low";
    if (!strcmp(key, "fceumm_sndvolume")) return "10";
    if (!strcmp(key, "fceumm_sndstereodelay")) return "disabled";
    if (!strcmp(key, "fceumm_ntsc_filter")) return "disabled";
    if (!strcmp(key, "fceumm_overscan") || !strcmp(key, "fceumm_game_genie") ||
        !strcmp(key, "fceumm_overclocking")) return "disabled";
    if (!strncmp(key, "fceumm_overscan_", 16)) return "0";
    if (!strcmp(key, "fceumm_turbo_enable")) return "None";
    return 0;
}
static bool environment(unsigned cmd, void *data)
{
    switch (cmd) {
    case RETRO_ENVIRONMENT_GET_LOG_INTERFACE: ((struct retro_log_callback *)data)->log = core_log; return true;
    case RETRO_ENVIRONMENT_GET_CORE_OPTIONS_VERSION: *(unsigned *)data = 1; return true;
    case RETRO_ENVIRONMENT_GET_LANGUAGE: *(unsigned *)data = RETRO_LANGUAGE_ENGLISH; return true;
    case RETRO_ENVIRONMENT_GET_INPUT_BITMASKS: return true;
    case RETRO_ENVIRONMENT_SET_PIXEL_FORMAT: return *(enum retro_pixel_format *)data == RETRO_PIXEL_FORMAT_RGB565;
    case RETRO_ENVIRONMENT_SET_CORE_OPTIONS:
    case RETRO_ENVIRONMENT_SET_CORE_OPTIONS_INTL:
    case RETRO_ENVIRONMENT_SET_INPUT_DESCRIPTORS:
    case RETRO_ENVIRONMENT_SET_MEMORY_MAPS:
    case RETRO_ENVIRONMENT_SET_MINIMUM_AUDIO_LATENCY: return true;
    case RETRO_ENVIRONMENT_GET_SYSTEM_DIRECTORY:
    case RETRO_ENVIRONMENT_GET_SAVE_DIRECTORY: *(const char **)data = FC_ROOT; return true;
    case RETRO_ENVIRONMENT_GET_VARIABLE: {
        struct retro_variable *v = (struct retro_variable *)data;
        v->value = option(v->key); return v->value != 0;
    }
    case RETRO_ENVIRONMENT_GET_VARIABLE_UPDATE: *(bool *)data = variable_changed; variable_changed = 0; return true;
    case RETRO_ENVIRONMENT_SHUTDOWN: input.exit_requested = 1; return true;
    default: return false;
    }
}
static void video_impl(const void *data, unsigned w, unsigned h, size_t pitch)
{
    int trace = video_callbacks++ < 6 && h1_diag_is_verbose();
    int blit_rc, present_rc;
    if (trace) h1_diag("VIDEO_CB_BEGIN frame=%u data=%p width=%u height=%u pitch=%u", total_frames + 1, data, w, h, (unsigned)pitch);
    if (!data) { if (trace) h1_diag("VIDEO_CB_DUPLICATE"); return; }
    if (!(w == 256 && h >= 224 && h <= 240) || pitch < w * 2) { ++video_errors; return; }
    if (trace) h1_diag("VIDEO_SCALE_BEGIN");
    uint32_t buffer_start=h1_profile_now();
    volatile uint32_t *buffer=gba_game_buffer(trace);
    if (!buffer) { ++video_errors; return; }
    uint32_t scale_start=h1_profile_now();
    scale_frame_rgb32((uint32_t *)buffer,data,pitch,input.scale,w,h,input.scale_changed||!videos||last_video_buffer!=buffer);
    h1_profile_video(scale_start-buffer_start,h1_profile_now()-scale_start);
    last_video_buffer=buffer;
    input.scale_changed=0;
    if (trace) h1_diag("VIDEO_SCALE_END");
    /* Full packed framebuffer, including black borders. */
    if (trace) h1_diag("VIDEO_BLIT_BEGIN");
    blit_rc = 0;
    if (trace) h1_diag("VIDEO_BLIT_END rc=%d", blit_rc);
    if (trace) h1_diag("VIDEO_PRESENT_BEGIN");
    present_rc = blit_rc;
    if (trace) h1_diag("VIDEO_PRESENT_END rc=%d", present_rc);
    if (blit_rc < 0 || present_rc < 0) ++video_errors;
    ++videos;
}
static void video(const void *data, unsigned w, unsigned h, size_t pitch)
{ h1_profile_push(HP_VIDEO); video_impl(data,w,h,pitch); h1_profile_pop(); }
static void indexed_video(const h1_nes_video_frame *frame)
{
    h1_profile_push(HP_VIDEO);
    int trace=video_callbacks++<6 && h1_diag_is_verbose();
    if (frame->width!=256 || frame->height<224 || frame->height>240 || frame->pitch<256) {
        ++video_errors;h1_profile_pop();return;
    }
    uint32_t begin=h1_profile_now();
    volatile uint32_t *buffer=gba_game_buffer(trace);
    if (!buffer) { ++video_errors;h1_profile_pop();return; }
    uint32_t scale_start=h1_profile_now();
    scale_nes_indexed_rgb32((uint32_t *)buffer,frame,input.scale,
        input.scale_changed||!videos||last_video_buffer!=buffer);
    h1_profile_video(scale_start-begin,h1_profile_now()-scale_start);
    last_video_buffer=buffer;input.scale_changed=0;++videos;
    if (trace) h1_diag("VIDEO_INDEXED width=%u height=%u scale=%u",frame->width,frame->height,(unsigned)input.scale);
    h1_profile_pop();
}
static size_t audio(const int16_t *data, size_t n)
{
    h1_profile_push(HP_AUDIO); size_t rc=gba_audio_write(data,n); h1_profile_pop();
#if H1_DIAGNOSTIC
    if (diagnostic_running && rc!=n) diagnostic_pcm_error=1;
#endif
    return rc;
}
static void poll(void)
{
#if H1_DIAGNOSTIC
    if (diagnostic_running) {
        h1_profile_push(profile_waiting ? HP_WAIT : HP_INPUT);
        diagnostic_poll(0);h1_profile_pop();return;
    }
#endif
    int trace = input_callbacks++ < 6 && h1_diag_is_verbose();
    if (trace) h1_diag("INPUT_POLL_BEGIN frame=%u", total_frames + 1);
    h1_profile_push(profile_waiting ? HP_WAIT : HP_INPUT); input_poll(&input); h1_profile_pop();
    if (trace) h1_diag("INPUT_POLL_END mask=%u exit=%d menu=%d", input.mask, input.exit_requested, input.menu_requested);
    if (input.skip_changed) { variable_changed = 1; input.skip_changed = 0; }
}
static int16_t buttons(unsigned port, unsigned device, unsigned index, unsigned id)
{
#if H1_DIAGNOSTIC
    if (diagnostic_running) {
        if (port || index || device!=RETRO_DEVICE_JOYPAD) return 0;
        if (id==RETRO_DEVICE_ID_JOYPAD_MASK) return diagnostic_buttons;
        return id<16 ? ((diagnostic_buttons>>id)&1u) : 0;
    }
#endif
    if (input.pause_requested || port || index || device != RETRO_DEVICE_JOYPAD) return 0;
    if (id == RETRO_DEVICE_ID_JOYPAD_MASK) return input.mask;
    return id < 16 ? ((input.mask >> id) & 1u) : 0;
}
static void clear_input(int trace)
{
    /* Consume selector confirmation so it cannot become an in-game held key. */
    input_state discard;
    if (trace) h1_diag("INPUT_CLEAR_BEGIN");
    memset(&discard, 0, sizeof(discard)); input_poll(&discard);
    if (trace) h1_diag("INPUT_CLEAR_END");
    input_discard_held(&input);
    input.exit_requested = input.menu_requested = input.pause_requested = 0;
}
#if !H1_DIAGNOSTIC
int h1_app_main(void)
{
    char directory[H1_ROM_PATH_MAX] = FC_ROOT;
    int result = 0;
    int logging;
    logging = h1_diag_init();
    h1_diag("APP_BEGIN version=0.1.16 core=FCEUmm audio=PCM input=GUI_9D8 profile=%u",(unsigned)H1_PROFILE);
    if (!logging) h1_message_box(0, "Cannot create A:\\FC\\h1fc.log. Create the FC directory and check writable storage.", "H1 FC log", 0);
    extern int h1_linear_init(void);
    h1_diag("LINEAR_BACKEND name=MXU1_Q8 selftest=%d irq_scope=cached_row state=preserved",h1_linear_init());
    memset(&input, 0, sizeof(input)); input.scale = 1; input.skip = H1_DEFAULT_FRAMESKIP;
    h1_diag("SCREEN_ALLOC_BEGIN bytes=%u", 480u * 272u * 2u);
    screen = (uint16_t *)malloc(480 * 272 * 2);
    h1_diag("SCREEN_ALLOC_END ptr=%p", screen);
    if (!screen) { h1_message_box(0, "Not enough memory", "H1 FC", 0); return 1; }
    for (;;) {
        char path[H1_ROM_PATH_MAX], line[160];
        struct retro_game_info game;
        save_store store;
        h1_config config;
        void *save;
        size_t save_size;
        uint32_t start, saved_at, load_tick, heartbeat_tick, frame_us;
        int pick, load_status, save_enabled;
        h1_diag_verbose(1);
        h1_diag("ROM_SELECT_BEGIN dir=%s", directory);
        pick = h1_select_rom(directory, path, sizeof(path));
        h1_diag("ROM_SELECT_END status=%d", pick);
        if (pick == 0) {
            log_line("ROM selection cancelled");
            /* V1.41's selector sets game display mode even on cancellation.
             * Closing without an open context is a no-op, so pair the native
             * window APIs to restore desktop redraw before returning. */
            h1_diag("SELECTOR_CANCEL_RESTORE_BEGIN");
            if (gba_gui_open()) gba_gui_close();
            else result = 4;
            h1_diag("SELECTOR_CANCEL_RESTORE_END restored=%d", result == 0);
            break;
        }
        if (pick < 0) {
            h1_message_box(0, "File selection failed or path is too long. Please select a .nes ROM.", "H1 FC", 0);
            clear_input(1); continue;
        }
        core = &h1_nes_core;
        h1_config_load(&config,&input,path);
        gba_audio_set_volume(config.volume);
        h1_diag("CORE_SELECTED name=%s", core->name);
        core->environment(environment); core->video(video); core->audio(audio); core->poll(poll); core->input(buttons);
        h1_nes_set_indexed_video(indexed_video);
        h1_rom_directory(path, directory, sizeof(directory));
        h1_diag("ROM_PATH length=%u path=%s", (unsigned)strlen(path), path);
        h1_diag_hex("ROM_PATH_HEX", path, strlen(path));
        if (!gba_gui_open()) {
            h1_message_box(0, "Cannot initialize the native graphics window.", "H1 FC", 0);
            result = 4; break;
        }
        clear_input(1);load_tick=h1_raw_tick_80hz();
        h1_diag("LOADING_BEGIN");h1_loading_begin(screen);
        frames = total_frames = videos = video_errors = video_callbacks = input_callbacks = 0; variable_changed = 0;
        audio_catchup_frames=0;
        h1_diag("CORE_INIT_BEGIN");
        core->init();
        h1_diag("CORE_INIT_END");
        h1_loading_step(20,"READING ROM");
        memset(&game, 0, sizeof(game)); game.path = path;
        log_line(path);
        h1_diag("ROM_LOAD_BEGIN");
        pick = core->load(&game);
        h1_diag("ROM_LOAD_END loaded=%d", pick);
        if (!pick) {
            h1_loading_end();h1_diag("LOADING_DONE failed=1");
            core->deinit(); gba_gui_close();
            h1_message_box(0, "ROM load failed. Check file and free memory.", "H1 FC", 0);
            clear_input(1); continue;
        }
        h1_loading_step(75,"RESTORING SAVE");
        save = core->memory(RETRO_MEMORY_SAVE_RAM); save_size = core->memory_size(RETRO_MEMORY_SAVE_RAM);
        h1_diag("SAVE_LOAD_BEGIN ptr=%p bytes=%u", save, (unsigned)save_size);
        memset(&store, 0, sizeof store); store.slot = -1;
        load_status = save && save_size ? save_load(&store, path, save, save_size) : 0;
        if (core->save_loaded && load_status == 1) core->save_loaded();
        h1_diag("SAVE_LOAD_END status=%d slot=%d generation=%u", load_status, store.slot, store.generation);
        save_enabled = save && save_size && load_status >= 0;
        if (load_status < 0) h1_message_box(0, "Save cannot be restored. Existing files are preserved; saving is disabled for this session.", "H1 FC", 0);
        log_line(load_status == 1 ? "SAVE restored" : load_status == 0 ? "SAVE new" : "SAVE restore failed");
        heartbeat_count=heartbeat_omitted=0;
        checkpoint_count=checkpoint_omitted=checkpoint_unchanged=0;
#if H1_PROFILE
        h1_diag("PROFILE_SETTINGS scale=%d skip=%d sound=%d",input.scale,input.skip,config.sound);
#endif
        h1_profile_begin();
        h1_loading_step(95,"STARTING AUDIO");
        {
            struct retro_system_av_info av;core->av(&av);
            frame_us=(uint32_t)(1000000.0 / av.timing.fps + 0.5);
            if (!frame_us || frame_us>25000u) frame_us=16639u;
            h1_diag("NES_TIMING frame_us=%u rate=%u",frame_us,(unsigned)av.timing.sample_rate);
            if (!H1_BENCHMARK && config.sound) gba_audio_open((unsigned)av.timing.sample_rate);
        }
        h1_loading_step(100,"READY");h1_loading_end();
        h1_diag("LOADING_DONE ticks=%u",(unsigned)(h1_raw_tick_80hz()-load_tick));
        h1_diag("TIMER_START_BEGIN"); h1_timer_start(); h1_diag("TIMER_START_END");
        h1_diag("TIMER_READ_BEGIN"); start = h1_timer_read_ms(); h1_diag("TIMER_READ_END ms=%u", start); saved_at = start;
        heartbeat_tick=h1_raw_tick_80hz();
        h1_profile_start();
        while (!input.exit_requested && !input.menu_requested) {
            uint32_t now;
            if (input.pause_requested) {
                gba_audio_pause();h1_timer_stop();h1_profile_dump("pause");
                dump_heartbeats();dump_checkpoints();
                int action=h1_pause_menu(&input,&config,core,path,screen);
                clear_input(0);
                if (action) {
                    input.menu_requested=action==1;input.exit_requested=action==2;break;
                }
                if (input.skip_changed) { variable_changed=1;input.skip_changed=0; }
#if H1_PROFILE
                h1_diag("PROFILE_SETTINGS scale=%d skip=%d sound=%d",input.scale,input.skip,config.sound);
#endif
                h1_profile_begin();
                if (!H1_BENCHMARK && config.sound) {
                    if (!gba_audio_resume()) {
                        struct retro_system_av_info av;core->av(&av);gba_audio_open((unsigned)av.timing.sample_rate);
                    }
                } else gba_audio_close();
                h1_timer_start();start=saved_at=h1_timer_read_ms();frames=0;
                heartbeat_tick=h1_raw_tick_80hz();
                h1_profile_start();
            }
            int trace = total_frames < 10 && h1_diag_is_verbose();
            if (trace) h1_diag("FRAME_BEGIN n=%u video=%u", total_frames + 1, videos);
            unsigned profile_videos=videos;
            h1_profile_push(HP_INPUT);input_frame(&input);h1_profile_pop();
            uint32_t profile_start=h1_profile_now();
            h1_profile_push(HP_CORE);
            unsigned render_skip=(unsigned)input.skip;
            unsigned audio_skip=gba_audio_render_skip();
            if (render_skip<audio_skip) {
                render_skip=audio_skip;++audio_catchup_frames;
            }
            h1_nes_set_frameskip(render_skip);
            core->run();
            h1_profile_pop();
            uint32_t profile_core=h1_profile_now()-profile_start;
            h1_profile_push(HP_OTHER);
            ++frames; ++total_frames;
            if (trace) h1_diag("FRAME_END n=%u video=%u errors=%u", total_frames, videos, video_errors);
            if (total_frames == 10) h1_diag_verbose(0);
#if H1_TEST_FRAMES > 0
            if (total_frames >= H1_TEST_FRAMES) input.exit_requested = 1;
#endif
            if (trace) h1_diag("FRAME_CLOCK_BEGIN n=%u", total_frames);
            now = h1_timer_read_ms();
            if ((uint32_t)(h1_raw_tick_80hz()-heartbeat_tick)>=2400u) {
                /* File commit can exceed the PCM prefill duration on H1. */
                remember_heartbeat(now);
                heartbeat_tick=h1_raw_tick_80hz();
            }
            if (trace) h1_diag("FRAME_CLOCK_END n=%u ms=%u", total_frames, now);
            if (save_enabled && (uint32_t)(now-saved_at)>=10000u) {
                h1_profile_push(HP_SAVE);
                if (core->save_sync) core->save_sync();
                if (save_dirty(&store,save,save_size)) {
                    uint32_t checkpoint_tick=h1_raw_tick_80hz();
                    gba_audio_storage_begin();
                    int checkpoint=save_checkpoint(&store,save,save_size);
                    remember_checkpoint(checkpoint,h1_raw_tick_80hz()-checkpoint_tick);
                    if (checkpoint < 0) {
                        log_line("SAVE checkpoint failed");
                        h1_message_box(0, "Save write failed. Check storage. Exit will retry.", "H1 FC", 0);
                        clear_input(0);
                    }
                    gba_audio_storage_end();
                } else ++checkpoint_unchanged;
                saved_at = h1_timer_read_ms();
                h1_profile_pop();
            }
            /* Respect the cartridge region (NTSC/PAL); discard excessive lag. */
            if ((uint32_t)(now - start) > 1000u) { start = now; frames = 0; }
            if (trace) h1_diag("FRAME_WAIT_BEGIN n=%u", total_frames);
            profile_waiting=1;h1_profile_push(HP_WAIT);
            if (H1_BENCHMARK) {
                /* Bounded comparisons measure the core without waiting. */
            } else if (gba_audio_pacing()) {
                /* PCM playback is the clock: H1's software millisecond timer
                 * loses ticks under audio IRQ load and would starve the mixer. */
                while (gba_audio_wait() && !input.exit_requested && !input.menu_requested && !input.pause_requested) poll();
            } else {
                while ((uint32_t)(h1_timer_read_ms() - start) < frames * frame_us / 1000u &&
                       !input.exit_requested && !input.menu_requested && !input.pause_requested) poll();
            }
            h1_profile_pop();profile_waiting=0;h1_profile_pop();
            h1_profile_frame(profile_core,videos-profile_videos);
            if (trace) h1_diag("FRAME_WAIT_END n=%u", total_frames);
        }
        /* Stop/cancel PCM before any teardown log can block on storage. */
        gba_audio_close();
        h1_diag("TIMER_STOP_BEGIN"); h1_timer_stop(); h1_diag("TIMER_STOP_END");
        h1_profile_dump("exit");
        h1_diag("AUDIO_CATCHUP frames=%u max_skip=5 config_skip=%u",audio_catchup_frames,(unsigned)input.skip);
        dump_heartbeats();dump_checkpoints();
        h1_config_save(&config,&input);
        snprintf(line, sizeof(line), "STOP video=%u errors=%u", videos, video_errors); log_line(line);
        h1_diag("SAVE_EXIT_BEGIN enabled=%d", save_enabled);
        if (core->save_sync) core->save_sync();
        pick = save_enabled ? save_checkpoint(&store, save, save_size) : 0;
        h1_diag("SAVE_EXIT_END status=%d", pick);
        if (pick < 0) {
            h1_message_box(0, "Save failed; the previous verified slot is retained.", "H1 FC", 0); result = 3;
        }
        h1_diag("CORE_UNLOAD_BEGIN"); core->unload(); h1_diag("CORE_UNLOAD_END");
        h1_diag("CORE_DEINIT_BEGIN"); core->deinit(); h1_diag("CORE_DEINIT_END");
        gba_gui_close();
        if (input.exit_requested) break;
        clear_input(1);
    }
    h1_video_release();
    free(screen); screen = 0;
    h1_diag("APP_END result=%d", result);
    return result;
}
#else
static const char diagnostic_version[]="v0.1.16";
static const char *diagnostic_reason;
static const char *diagnostic_sampling(unsigned sampling)
{
    static const char *const names[]={"light","detail","rtc_only"};
    return sampling<3 ? names[sampling] : "invalid";
}
/* Return 1 after an emulated frame, 0 for an input-phase cancellation. PCM
 * failure is latched independently: even its last completed frame is retained.
 * A cancellation during core/pacing finishes the in-flight frame only. */
static int diagnostic_frame(const h1_replay_view *view,int record)
{
    unsigned shown_before=videos;
    uint32_t core_start,core_ticks;
    if (record) h1_profile_diag_frame_begin(view->frame,1,h1_jit_stats[0],h1_nes_diag_jit_bytes());
    h1_profile_push(HP_INPUT);
    diagnostic_poll(1);diagnostic_buttons=view->buttons;
    h1_profile_pop();
    if (diagnostic_abort) {
        if (record) h1_profile_diag_frame_end(0,0,h1_jit_stats[0],h1_nes_diag_jit_bytes());
        return 0;
    }
    core_start=h1_profile_now();h1_profile_push(HP_CORE);
    h1_nes_set_frameskip(1);core->run();
    h1_profile_pop();core_ticks=h1_profile_now()-core_start;
    h1_profile_push(HP_OTHER);++frames;++total_frames;
    profile_waiting=1;h1_profile_push(HP_WAIT);
    while (gba_audio_wait() && !diagnostic_abort && !input.exit_requested) poll();
    h1_profile_pop();profile_waiting=0;h1_profile_pop();
    if (record) {
        h1_profile_diag_frame_end(core_ticks,videos-shown_before,h1_jit_stats[0],h1_nes_diag_jit_bytes());
        h1_profile_frame(core_ticks,videos-shown_before);
    }
    h1_audio_snapshot audio_state;gba_audio_snapshot(&audio_state);
    if (!(audio_state.flags&1u) || audio_state.failures || !gba_audio_pacing())
        diagnostic_pcm_error=1;
    return 1;
}
static void diagnostic_stop_group(const h1_replay_view *group,unsigned completed,
                                  unsigned aborted,unsigned partial,const char *reason)
{
    diagnostic_running=0;diagnostic_buttons=0;profile_waiting=0;
    h1_timer_stop();gba_audio_close();
    /* Each dump releases its clock before any log write. The frame trace was
     * completed before this point, so close/log latency is never a frame cost. */
    h1_profile_dump(reason);h1_profile_diag_dump(reason);
    h1_diag("DIAG_CPU group=%u compiled=%u hits=%u misses=%u native_ops=%u deopts=%u rejected=%u invalidations=%u code_bytes=%u hle_batches=%u hle_pairs=%u hle_cycles=%u",group->group_id,h1_jit_stats[0],h1_jit_stats[1],h1_jit_stats[2],h1_jit_stats[3],h1_jit_stats[4],h1_jit_stats[5],h1_jit_stats[6],h1_nes_diag_jit_bytes(),h1_hle_stats[0],h1_hle_stats[1],h1_hle_stats[2]);
    h1_diag("DIAG_GROUP_END group=%u mode=%s repeat=%u sampling=%s completed=%u aborted=%u partial=%u reason=%s video_errors=%u suppressed_core_logs=%u",group->group_id,h1_replay_mode_name(group->mode),group->repeat,diagnostic_sampling(group->sampling),completed,aborted,partial,reason,video_errors,diagnostic_suppressed_logs);
}
static unsigned diagnostic_session(void)
{
    h1_replay replay;
    h1_replay_view view,group;
    void *snapshot=0,*verify=0;
    size_t snapshot_bytes=0;
    uint32_t snapshot_crc=0,frame_us;
    unsigned group_open=0,group_completed=0,group_partial=0;
    unsigned boot_audio=0,session_frames=0;
    struct retro_system_av_info av;
    diagnostic_reason="none";
    diagnostic_running=diagnostic_abort=diagnostic_pcm_error=diagnostic_suppressed_logs=0;
    diagnostic_buttons=0;
    h1_replay_init(&replay,h1_nes_diag_identity());
    if (replay.stage==H1_REPLAY_UNSUPPORTED) {
        diagnostic_reason="ROM_identity";h1_loading_end();return replay.stage;
    }
    core->av(&av);
    frame_us=(uint32_t)(1000000.0/av.timing.fps+0.5);
    if (!frame_us || frame_us>25000u || !av.timing.sample_rate) {
        diagnostic_reason="invalid_AV_timing";replay.stage=H1_REPLAY_ERROR;goto finish;
    }
    input.scale=1;input.skip=1;input.scale_changed=1;
    input.exit_requested=input.menu_requested=input.pause_requested=0;
    variable_changed=0;last_video_buffer=0;
    h1_diag_verbose(0);
    h1_diag("DIAG_BEGIN revision=1 version=%s group_total=%u frames=%u warmup=%u boot_frames=%u scale=1 skip=1 sound=1 volume=100 adaptive_skip=0 storage=disabled input=replay abort=P_Escape_touch log=A:\\FC\\nesdiag.log",diagnostic_version,H1_REPLAY_GROUPS,H1_REPLAY_RUN_FRAMES,H1_REPLAY_WARMUP_FRAMES,H1_REPLAY_BOOT_FRAMES);
    h1_diag("DIAG_IDENTITY mapper=19 prg_bytes=262144 chr_bytes=262144 prg_chr_md5=de319a7a8099c388810c59cb038e8c9d frame_us=%u sample_rate=%u",frame_us,(unsigned)av.timing.sample_rate);
    if (!h1_nes_diag_mode(H1_REPLAY_INTERPRETER)) {
        diagnostic_reason="boot_CPU_mode";replay.stage=H1_REPLAY_ERROR;goto finish;
    }
    h1_nes_diag_video_reset();
    h1_loading_step(100,"PREPARING CITY");h1_loading_end();
    gba_audio_set_volume(100);
    if (!gba_audio_open((unsigned)av.timing.sample_rate)) {
        diagnostic_reason="boot_PCM_open";replay.stage=H1_REPLAY_ERROR;goto finish;
    }
    boot_audio=1;
    if (!h1_diag_active()) {
        diagnostic_reason="log_unavailable";replay.stage=H1_REPLAY_ERROR;goto finish;
    }
    h1_timer_start();diagnostic_running=1;
    for (;;) {
        h1_replay_read(&replay,&view);
        if (view.stage==H1_REPLAY_BOOT || view.stage==H1_REPLAY_RUN) {
            if (diagnostic_abort) { h1_replay_abort(&replay);break; }
            /* Preserve this pre-frame view. frame_done may immediately move
             * to the next group's RESTORE and reset its completed counter. */
            int completed=diagnostic_frame(&view,view.stage==H1_REPLAY_RUN);
            if (completed) {
                ++session_frames;
                if (!h1_replay_frame_done(&replay)) {
                    diagnostic_reason="replay_frame_contract";replay.stage=H1_REPLAY_ERROR;break;
                }
                if (view.stage==H1_REPLAY_RUN) group_completed=view.frame;
            } else if (view.stage==H1_REPLAY_RUN) group_partial=1;
            if (diagnostic_pcm_error || input.exit_requested || video_errors) {
                diagnostic_reason=diagnostic_pcm_error ? "PCM_failure" :
                    input.exit_requested ? "core_shutdown" : "video_failure";
                replay.stage=H1_REPLAY_ERROR;break;
            }
            if (diagnostic_abort) { h1_replay_abort(&replay);break; }
            if (view.stage==H1_REPLAY_BOOT && view.frame==H1_REPLAY_BOOT_FRAMES) {
                diagnostic_running=0;diagnostic_buttons=0;
                h1_timer_stop();gba_audio_close();boot_audio=0;
                h1_diag("DIAG_BOOT_END completed=%u mode=interpreter storage=disabled",view.frame);
            } else if (view.stage==H1_REPLAY_RUN && view.frame==H1_REPLAY_RUN_FRAMES) {
                diagnostic_stop_group(&group,group_completed,0,0,"complete");group_open=0;
                if (!h1_diag_active()) {
                    diagnostic_reason="log_unavailable";replay.stage=H1_REPLAY_ERROR;break;
                }
            }
            continue;
        }
        if (view.control==H1_REPLAY_CONTROL_SAVE) {
            snapshot_bytes=core->state_size();
            if (snapshot_bytes) { snapshot=malloc(snapshot_bytes);verify=malloc(snapshot_bytes); }
            int ok=snapshot && verify;
            if (ok) { memset(snapshot,0,snapshot_bytes);ok=core->state_save(snapshot,snapshot_bytes); }
            if (ok) snapshot_crc=crc32_bytes(snapshot,snapshot_bytes);
            h1_diag("DIAG_SNAPSHOT bytes=%u crc=%08X saved=%u frame=%u storage=RAM",(unsigned)snapshot_bytes,snapshot_crc,ok,H1_REPLAY_BOOT_FRAMES);
            h1_replay_control_done(&replay,ok);
            if (!ok) { diagnostic_reason="snapshot_save";break; }
            continue;
        }
        if (view.control==H1_REPLAY_CONTROL_RESTORE) {
            char label[40];
            h1_loading_begin(screen);
            snprintf(label,sizeof label,"GROUP %u OF %u",view.group_id,view.group_total);
            h1_loading_step(100u*view.group_id/view.group_total,label);
            int ok=snapshot && verify && crc32_bytes(snapshot,snapshot_bytes)==snapshot_crc;
            if (ok) ok=core->state_load(snapshot,snapshot_bytes);
            uint32_t restored_crc=0;
            if (ok) {
                memset(verify,0,snapshot_bytes);ok=core->state_save(verify,snapshot_bytes);
                if (ok) {
                    restored_crc=crc32_bytes(verify,snapshot_bytes);
                    ok=restored_crc==snapshot_crc && !memcmp(snapshot,verify,snapshot_bytes);
                }
            }
            h1_diag("DIAG_RESTORE group=%u bytes=%u snapshot_crc=%08X restored_crc=%08X equal=%u",view.group_id,(unsigned)snapshot_bytes,snapshot_crc,restored_crc,ok);
            if (ok) ok=h1_nes_diag_mode(view.mode);
            if (ok) ok=h1_profile_diag_group_begin(view.group_id,view.sampling,H1_REPLAY_RUN_FRAMES,H1_REPLAY_WARMUP_FRAMES);
            if (!ok) {
                h1_replay_control_done(&replay,0);diagnostic_reason="restore_mode_or_trace";break;
            }
            h1_nes_diag_video_reset();
            frames=videos=video_errors=video_callbacks=input_callbacks=0;
            input.scale=1;input.skip=1;input.scale_changed=1;last_video_buffer=0;
            diagnostic_pcm_error=diagnostic_suppressed_logs=0;diagnostic_buttons=0;
            group=view;group_completed=group_partial=0;group_open=1;
            h1_diag("DIAG_GROUP_BEGIN group=%u mode=%s repeat=%u sampling=%s snapshot_crc=%08X group_total=%u frames=%u warmup=%u frame_us=%u scale=1 skip=1 sound=1 adaptive_skip=0 storage=disabled",group.group_id,h1_replay_mode_name(group.mode),group.repeat,diagnostic_sampling(group.sampling),snapshot_crc,group.group_total,H1_REPLAY_RUN_FRAMES,H1_REPLAY_WARMUP_FRAMES,frame_us);
            h1_profile_begin();
            h1_loading_end();gba_audio_set_volume(100);
            if (!gba_audio_open((unsigned)av.timing.sample_rate) || !h1_diag_active()) {
                h1_replay_control_done(&replay,0);diagnostic_reason="group_PCM_or_log_open";break;
            }
            if (!h1_replay_control_done(&replay,1)) {
                diagnostic_reason="replay_restore_contract";replay.stage=H1_REPLAY_ERROR;break;
            }
            h1_timer_start();h1_profile_start();diagnostic_running=1;
            continue;
        }
        break;
    }
finish:
    diagnostic_running=0;diagnostic_buttons=0;profile_waiting=0;
    if (group_open) diagnostic_stop_group(&group,group_completed,replay.stage==H1_REPLAY_ABORTED,group_partial,
        replay.stage==H1_REPLAY_ABORTED ? "user_abort" : diagnostic_reason);
    else { h1_timer_stop();gba_audio_close(); }
    if (boot_audio) h1_diag("DIAG_BOOT_END completed=%u mode=interpreter status=%s",replay.completed,h1_replay_stage_name(replay.stage));
    h1_loading_end();h1_profile_diag_release();free(verify);free(snapshot);
    h1_diag("DIAG_SESSION frames=%u expected=%u",session_frames,H1_REPLAY_TOTAL_FRAMES);
    if (!h1_diag_active()) { diagnostic_reason="log_unavailable";return H1_REPLAY_ERROR; }
    return replay.stage;
}
int h1_app_main(void)
{
    char directory[H1_ROM_PATH_MAX]=FC_ROOT,path[H1_ROM_PATH_MAX];
    struct retro_game_info game;
    unsigned status=H1_REPLAY_ERROR;
    int gui_open=0,core_initialized=0,core_loaded=0,result=1;
    if (!h1_diag_init()) {
        h1_message_box(0,"Cannot create A:\\FC\\nesdiag.log. Check writable storage.","NES diagnostic",0);
        return 1;
    }
    h1_diag("APP_BEGIN version=0.1.16 core=FCEUmm audio=PCM input=GUI_9D8 profile=%u",(unsigned)H1_PROFILE);
    h1_diag("DIAG_BUILD version=%s ordinary_app=unchanged",diagnostic_version);
    diagnostic_reason="setup";
    extern int h1_linear_init(void);
    h1_diag("LINEAR_BACKEND name=MXU1_Q8 selftest=%d irq_scope=cached_row state=preserved",h1_linear_init());
    memset(&input,0,sizeof input);input.scale=input.skip=1;
    frames=total_frames=videos=video_errors=video_callbacks=input_callbacks=0;
    variable_changed=profile_waiting=0;
    screen=(uint16_t *)malloc(480u*272u*2u);
    if (!screen) { diagnostic_reason="screen_allocation";goto done; }
    h1_diag("ROM_SELECT_BEGIN dir=%s",directory);
    int pick=h1_select_rom(directory,path,sizeof path);
    h1_diag("ROM_SELECT_END status=%d",pick);
    if (pick<=0) {
        if (gba_gui_open()) {
            gba_gui_close();
            status=pick==0 ? H1_REPLAY_ABORTED : H1_REPLAY_ERROR;
            diagnostic_reason=pick==0 ? "selector_cancel" : "selector_failure";
        } else {
            status=H1_REPLAY_ERROR;diagnostic_reason="selector_restore";
        }
        goto done;
    }
    if (!h1_diag_active()) { diagnostic_reason="log_unavailable";goto done; }
    core=&h1_nes_core;
    core->environment(environment);core->video(video);core->audio(audio);core->poll(poll);core->input(buttons);
    h1_nes_set_indexed_video(indexed_video);
    h1_diag("ROM_PATH length=%u path=%s",(unsigned)strlen(path),path);
    if (!gba_gui_open()) { diagnostic_reason="GUI_open";goto done; }
    gui_open=1;clear_input(0);
    h1_loading_begin(screen);core->init();core_initialized=1;
    memset(&game,0,sizeof game);game.path=path;
    h1_diag("ROM_LOAD_BEGIN");core_loaded=core->load(&game);
    h1_diag("ROM_LOAD_END loaded=%d",core_loaded);
    if (!core_loaded) { diagnostic_reason="ROM_load";goto done; }
    /* Deliberately enter before config/SRAM/state file load. The full initial
     * state and every subsequent restore live only in private RAM. */
    status=diagnostic_session();
done:
    diagnostic_running=0;diagnostic_buttons=0;
    h1_loading_end();h1_profile_diag_release();
    if (core_loaded) core->unload();
    if (core_initialized) core->deinit();
    if (gui_open) gba_gui_close();
    h1_video_release();free(screen);screen=0;
    if (!h1_diag_active()) { status=H1_REPLAY_ERROR;diagnostic_reason="log_unavailable"; }
    h1_diag("DIAG_END status=%s reason=%s storage=disabled",h1_replay_stage_name(status),diagnostic_reason);
    result=status==H1_REPLAY_ERROR ? 1 : 0;
    h1_diag("APP_END result=%d",result);
    /* The terminal records themselves can exhaust storage or fail a close.
     * Do not show a successful completion after the durable log was lost. */
    if (!h1_diag_active()) { status=H1_REPLAY_ERROR;diagnostic_reason="log_unavailable";result=1; }
    if (status==H1_REPLAY_DONE)
        h1_message_box(0,"Diagnosis complete. Please send A:\\FC\\nesdiag.log.","NES diagnostic",0);
    else if (status==H1_REPLAY_UNSUPPORTED)
        h1_message_box(0,"This diagnosis needs the matching SGZ2-CN ROM. No save was loaded or written.","NES diagnostic",0);
    else if (status==H1_REPLAY_ABORTED)
        h1_message_box(0,"Diagnosis stopped. Partial results are in A:\\FC\\nesdiag.log.","NES diagnostic",0);
    else
        h1_message_box(0,"Diagnosis failed. Please send A:\\FC\\nesdiag.log if it was created.","NES diagnostic",0);
    return result;
}
#endif
