#include "profile.h"
#if H1_PROFILE
#include "h1_sdk.h"
#include "diagnostics.h"
#include <string.h>
#if H1_DIAGNOSTIC
#include "audio.h"
#include <stdio.h>
#include <stdlib.h>
#endif

/* TCU wall time includes IRQ/OS scheduling in the interrupted scope. */
#ifndef H1_PROFILE_HOST_TEST
#include "profile_clock.h"
#else
static int pc_start(void) { return 1; }
static void pc_stop(void) {}
static unsigned pc_fault;
#endif
typedef struct {
    uint64_t ticks[HP_COUNT], inclusive[HP_COUNT], core_sum;
    uint64_t detail_core_sum, light_core_sum;
    unsigned detail_frames, detail_displayed, light_frames, light_displayed;
    unsigned calls[HP_COUNT], histogram[64], frames, displayed, over_budget;
    uint32_t core_max;
    unsigned loop_under_2ms, loop_over_25ms;
    uint64_t video_buffer_ticks,video_scale_ticks;
    unsigned video_frames;
} profile_window;
static profile_window current, windows[12];
static unsigned stack[16], depth, enabled, window_count, omitted, frequency, fault;
static uint32_t stack_begin[16];
static unsigned stack_outer[16];
static unsigned clock_attempted, prepared;
/* Five rotating modes repeat every 155 frames, coprime with 2/3/6-frame
 * presentation. Each mode has its own denominator; no extrapolation. */
#define PPU_SAMPLE_PERIOD 31u
unsigned h1_profile_ppu_sample;
static unsigned ppu_sample_phase;
unsigned h1_profile_detail_mask;
static unsigned detail_mode, detail_cursor;
static uint32_t clock_reads, previous_reads;
static uint64_t previous_ticks[HP_COUNT],previous_inclusive[HP_COUNT];
static unsigned previous_calls[HP_COUNT];
typedef struct {
    uint64_t ticks[HP_COUNT], inclusive[HP_COUNT], core_sum[2], loop_sum[2];
    unsigned calls[HP_COUNT], frames[2], reads;
} detail_summary;
static detail_summary details[6];
static const char *detail_names[6]={"light","control","coarse","cpu","ppu","sound"};
static const unsigned detail_masks[6]={0,0,HPD_CPU,
    HPD_CPU|HPD_NATIVE|HPD_EVENT,HPD_CPU|HPD_PPU,HPD_CPU|HPD_SOUND|HPD_EVENT};
static uint32_t last, frame_rtc;
static uint32_t run_start_rtc, fault_previous_rtc, fault_current_rtc;
static unsigned fault_frame;
static uint32_t rtc_run_start,rtc_run_last;
static unsigned rtc_run_frames,rtc_run_displayed,rtc_run_invalid;
static uint64_t accounted;
#if H1_DIAGNOSTIC
#define DIAG_FRAME_CAPACITY 1800u
typedef struct {
    uint32_t frame,requested_skip,displayed,mode,fine_valid,fault;
    uint32_t rtc_begin,rtc_end,raw_begin,raw_end,tick_begin,loop_ticks,core_ticks;
    uint32_t scopes[HP_COUNT],jit_compiles,jit_bytes,jit_bytes_end,clock_reads;
    h1_audio_snapshot before,after;
} diagnostic_frame;
static diagnostic_frame *diag_frames;
static diagnostic_frame *diag_record;
static unsigned diag_group,diag_sampling=HPDS_DETAIL,diag_expected,diag_warmup;
static unsigned diag_count,diag_overflow,diag_active,diag_unbalanced;
#endif
static const char *names[HP_COUNT] = {"core_other_and_unsampled", "ppu_background_legacy", "apu_finalize",
    "screen_output", "pcm_resample", "input_poll", "rom_io",
#if H1_DIAGNOSTIC
    "jit_compile_and_cache_sync",
#else
    "jit_cache_sync",
#endif
    "save_checkpoint", "pacing_wait", "loop_other",
    "cpu_dispatch_fallback_sampled", "native_chain_sampled", "cpu_slow_event_sampled",
    "native_c_completion_sampled", "apu_event_sampled", "apu_synthesis_sampled",
    "mapper_callback_sampled", "ppu_loop_other_sampled", "ppu_background_sampled",
    "ppu_sprite_sampled", "ppu_pixel_sampled"};
uint32_t h1_profile_now(void)
{
#if H1_DIAGNOSTIC
    if (diag_sampling==HPDS_RTC_ONLY) return 0;
#endif
    ++clock_reads;
#ifdef H1_PROFILE_HOST_TEST
    extern uint32_t h1_profile_test_clock;
    return h1_profile_test_clock;
#else
    uint32_t value=pc_now();
    if (pc_fault) { enabled=h1_profile_ppu_sample=h1_profile_detail_mask=0;fault=3; }
    return value;
#endif
}
extern uint32_t h1_wall_clock(void);
static uint32_t raw_tick(void)
{
#ifdef H1_PROFILE_HOST_TEST
    extern uint32_t h1_profile_test_raw_tick(void);
    return h1_profile_test_raw_tick();
#else
    return h1_raw_tick_80hz();
#endif
}
typedef struct {
    uint32_t rtc_start,rtc_end,counter_start,counter_end,raw_start,raw_end;
    unsigned iterations;
    const char *outcome;
} clock_observation;
static void wait_rtc_edge(clock_observation *p)
{
    memset(p,0,sizeof *p);
    p->rtc_start=h1_wall_clock();p->counter_start=h1_profile_now();
    p->raw_start=p->raw_end=raw_tick();
    for (;;) {
        p->rtc_end=h1_wall_clock();p->counter_end=h1_profile_now();
        if (pc_fault) { p->outcome="counter_read_fault";break; }
        if (p->rtc_end!=p->rtc_start) { p->outcome="RTC_edge";break; }
        if (p->counter_end-p->counter_start>=65536u) { p->outcome="counter_timeout";break; }
        ++p->iterations;
        if (!(p->iterations&255u)) p->raw_end=raw_tick();
        if (p->raw_end-p->raw_start>=240u) { p->outcome="raw_tick_timeout";break; }
        /* Last resort when both hardware and firmware clocks are stopped.
         * This bound is deliberately much larger than the old 4M iterations. */
        if (p->iterations>=128000000u) { p->outcome="iteration_limit";break; }
    }
    p->raw_end=raw_tick();
}
static void log_observation(const char *phase, const clock_observation *p)
{
    h1_diag("PROFILE_CLOCK_PROBE phase=%s rtc_start=%u rtc_end=%u counter_start=%u counter_end=%u counter_delta=%u raw_ticks=%u iterations=%u outcome=%s",phase,p->rtc_start,p->rtc_end,p->counter_start,p->counter_end,p->counter_end-p->counter_start,p->raw_end-p->raw_start,p->iterations,p->outcome);
}
static void log_tcu_state(const char *stage)
{
#ifndef H1_PROFILE_HOST_TEST
    h1_diag("PROFILE_TCU stage=%s owned=%u enabled=%08X stopped=%08X flags=%08X mask=%08X",stage,pc_owned,pc_read(0x10,1),pc_read(0x1c,4),pc_read(0x20,4),pc_read(0x30,4));
    if (pc_owned) {
        /* Only read channel registers while its clock is supplied. */
        h1_diag("PROFILE_TCU_REG stage=%s full=%u half=%u counter=%u control=%04X saved_full=%u saved_half=%u saved_counter=%u saved_control=%04X",stage,pc_read(PC_BASE,2),pc_read(PC_BASE+4,2),pc_read(PC_BASE+8,2),pc_read(PC_BASE+12,2),pc_saved[0],pc_saved[1],pc_saved[2],pc_saved[3]);
    }
#else
    (void)stage;
#endif
}
static void log_tcu_reads(const char *stage)
{
#ifndef H1_PROFILE_HOST_TEST
    h1_diag("PROFILE_TCU_READS stage=%s fault=%u retries=%u backwards=%u wraps=%u deferred=%u terminal=%u period=%u previous=%u total=%u max_delta=%u bad_a=%u bad_b=%u bad_previous=%u",stage,pc_fault,pc_retries,pc_backwards,pc_wraps,pc_deferred,pc_terminal,PC_PERIOD,pc_previous,pc_total,pc_max_delta,pc_bad_a,pc_bad_b,pc_bad_previous);
#if H1_DIAGNOSTIC
    h1_diag("PROFILE_TCU_RTC stage=%s rtc_previous=%u rtc_anchor=%u phase_lo=%d phase_hi=%d missing_full=%u flag_only=%u rtc_retries=%u",stage,pc_rtc_previous,pc_rtc_anchor,pc_phase_lo,pc_phase_hi,pc_missing_full,pc_flag_only,pc_rtc_retries);
#endif
#else
    (void)stage;
#endif
}
void h1_profile_begin(void)
{
    uint32_t tick, elapsed, observed;
    clock_observation align,measure;
    enabled = depth = window_count = omitted = fault = prepared = 0;
    h1_profile_ppu_sample=ppu_sample_phase=0;
    h1_profile_detail_mask=detail_mode=detail_cursor=0;
    clock_reads=previous_reads=0;
    memset(details,0,sizeof details);
    memset(previous_ticks,0,sizeof previous_ticks);
    memset(previous_inclusive,0,sizeof previous_inclusive);
    memset(previous_calls,0,sizeof previous_calls);
    run_start_rtc=fault_previous_rtc=fault_current_rtc=fault_frame=0;
    rtc_run_start=rtc_run_last=rtc_run_frames=rtc_run_displayed=rtc_run_invalid=0;
    memset(&current, 0, sizeof current);
    accounted=0;
#if H1_DIAGNOSTIC
    if (diag_sampling==HPDS_RTC_ONLY) return;
#ifndef H1_PROFILE_HOST_TEST
    /* Revalidate every measured group. A successful first boot must not hide
     * a broken timer restart or make a later group's frequency an assumption. */
    frequency=clock_attempted=0;
#endif
#endif
    tick=h1_wall_clock();
    if (!tick) { h1_diag("PROFILE_CLOCK ready=0 reason=RTC_unavailable");return; }
    if (!frequency && clock_attempted) {
        h1_diag("PROFILE_CLOCK ready=0 reason=previous_clock_failure");return;
    }
    if (!pc_start()) {
        log_tcu_state("busy");h1_diag("PROFILE_CLOCK ready=0 reason=TCU5_busy");return;
    }
    frame_rtc=tick;
    if (frequency) {
        h1_diag("PROFILE_CLOCK source=TCU5_RTC hz=%u resolution_us=31 ready=1 reused=1",frequency);
        prepared=1;pc_stop();
        return;
    }
    clock_attempted=1;
    log_tcu_state("started");
    wait_rtc_edge(&align);
    if (pc_fault || align.rtc_end==align.rtc_start || !align.rtc_end) {
        log_observation("align",&align);log_tcu_state("align_failed");
        log_tcu_reads("align_failed");
        pc_stop();h1_diag("PROFILE_CLOCK ready=0 reason=%s",pc_fault ? "TCU_read_failed" : "RTC_alignment_failed");return;
    }
    /* Do not write logs between the two edges: storage latency would shorten
     * the reference second and falsely classify a healthy counter as slow. */
    wait_rtc_edge(&measure);
    elapsed=measure.rtc_end-measure.rtc_start;
    observed=elapsed && elapsed<3 ? (measure.counter_end-measure.counter_start)/elapsed : 0;
#if H1_DIAGNOSTIC
    /* Keep probe reads contiguous with calibration; logging between them can
     * block for seconds and is not timer-read overhead. Never subtract this
     * estimate from frame timings: whole-run RTC controls quantify disturbance. */
    uint32_t probe_raw_begin=raw_tick(),probe_rtc_begin=h1_wall_clock();
    uint32_t probe_begin=h1_profile_now(),probe_end=probe_begin,probe_reads=0;
    if (!pc_fault && observed>=32000u && observed<=33500u)
        for (;probe_reads<4096u && !pc_fault;++probe_reads) probe_end=h1_profile_now();
    uint32_t probe_raw_end=raw_tick(),probe_rtc_end=h1_wall_clock();
#endif
    log_observation("align",&align);log_observation("measure",&measure);
    frequency=!pc_fault && observed>=32000u && observed<=33500u ? 32768u : 0;
    prepared=frequency!=0;
    h1_diag("PROFILE_CLOCK source=TCU5_RTC hz=%u observed_hz=%u calibration_RTC_seconds=%u resolution_us=31 ready=%u",frequency,observed,elapsed,prepared);
#if H1_DIAGNOSTIC
    h1_diag("PROFILE_CLOCK_COST reads=%u ticks=%u raw_ticks=%u rtc_seconds=%u valid=%u subtract_from_frames=0",probe_reads,probe_end-probe_begin,probe_raw_end-probe_raw_begin,probe_rtc_end-probe_rtc_begin,prepared && probe_reads==4096u);
#endif
    if (!prepared) log_tcu_state("frequency_failed");
    log_tcu_reads(prepared ? "calibrated" : "frequency_failed");
    pc_stop();
}
void h1_profile_start(void)
{
    if (!prepared) return;
    prepared=0;
    /* Audio/loading/file logs can take several seconds after calibration.
     * Reacquire a fresh counter only at the game-loop boundary: a baseline
     * read alone cannot recover multiple unobserved 16-bit wraps. No IO here. */
    if (!pc_start()) { fault=4;return; }
    frame_rtc=run_start_rtc=h1_wall_clock();
    if (!frame_rtc) { fault=2;pc_stop();return; }
    enabled=1;last=h1_profile_now();previous_reads=clock_reads;
    detail_mode=detail_cursor=1;h1_profile_detail_mask=0;
#if H1_DIAGNOSTIC
    if (diag_sampling!=HPDS_DETAIL) detail_mode=detail_cursor=0;
#endif
}
static void charge(uint32_t now)
{
    if (depth) current.ticks[stack[depth-1]] += (uint32_t)(now-last);
#if H1_DIAGNOSTIC
    if (depth && diag_active) diag_record->scopes[stack[depth-1]]+=(uint32_t)(now-last);
#endif
    last = now;
}
void h1_profile_push(unsigned kind)
{
    if (!enabled) return;
    charge(h1_profile_now());
    if (!enabled) return;
    if (depth == 16 || kind >= HP_COUNT) { enabled=h1_profile_ppu_sample=h1_profile_detail_mask=0;fault=1;return; }
    stack_begin[depth]=last;stack_outer[depth]=1;
    /* Only outermost same-kind duration is inclusive; recursive CPU/APU
     * entry must not count that elapsed time twice in a category. */
    if (kind>=HP_CPU_SAMPLE)
        for (unsigned i=0;i<depth;++i) if (stack[i]==kind) stack_outer[depth]=0;
    stack[depth++] = kind; ++current.calls[kind];
}
void h1_profile_pop(void)
{
    if (!enabled) return;
    charge(h1_profile_now());
    if (!enabled) return;
    if (!depth) { enabled=h1_profile_ppu_sample=h1_profile_detail_mask=0;fault=1;return; }
    --depth;
    if (stack[depth]>=HP_CPU_SAMPLE && stack_outer[depth])
        current.inclusive[stack[depth]]+=(uint32_t)(last-stack_begin[depth]);
}
static void archive(void)
{
    if (!current.frames) return;
    if (window_count == 12) {
        memmove(windows, windows+1, 11*sizeof *windows);
        --window_count; ++omitted;
    }
    windows[window_count++] = current;
    memset(&current, 0, sizeof current);
    memset(previous_ticks,0,sizeof previous_ticks);
    memset(previous_inclusive,0,sizeof previous_inclusive);
    memset(previous_calls,0,sizeof previous_calls);
    accounted=0;
}
void h1_profile_frame(uint32_t core_ticks, unsigned displayed)
{
    unsigned ms;
    uint32_t rtc=h1_wall_clock();
    /* Independent coarse measurement survives a failed high-resolution clock.
     * Frame endpoints exclude loading, calibration, menu and log writes. */
    if (!rtc || (rtc_run_frames && rtc<rtc_run_last)) rtc_run_invalid=1;
    if (!rtc_run_frames) rtc_run_start=rtc;
    rtc_run_last=rtc;++rtc_run_frames;rtc_run_displayed+=displayed;
    if (!enabled) return;
    /* Reject possible multiple 16-bit wraps after a long unsampled stall. */
    if (frame_rtc && (!rtc || rtc-frame_rtc>=2u)) {
        fault_previous_rtc=frame_rtc;fault_current_rtc=rtc;
        fault_frame=rtc_run_frames;fault=2;enabled=h1_profile_ppu_sample=h1_profile_detail_mask=0;return;
    }
    frame_rtc=rtc;
    uint64_t sum=0;
    for (unsigned k=0;k<HP_COUNT;++k) sum+=current.ticks[k];
    uint64_t delta=sum-accounted;accounted=sum;
    if (depth) { fault=1;enabled=h1_profile_ppu_sample=h1_profile_detail_mask=0;return; }
    detail_summary *d=&details[detail_mode];
    unsigned shown=displayed!=0;
    ++d->frames[shown];d->core_sum[shown]+=core_ticks;d->loop_sum[shown]+=delta;
    d->reads+=clock_reads-previous_reads;previous_reads=clock_reads;
    for (unsigned k=0;k<HP_COUNT;++k) {
        d->ticks[k]+=current.ticks[k]-previous_ticks[k];previous_ticks[k]=current.ticks[k];
        d->inclusive[k]+=current.inclusive[k]-previous_inclusive[k];previous_inclusive[k]=current.inclusive[k];
        d->calls[k]+=current.calls[k]-previous_calls[k];previous_calls[k]=current.calls[k];
    }
    if (delta*1000u < (uint64_t)frequency*2u) ++current.loop_under_2ms;
    if (delta*1000u > (uint64_t)frequency*25u) ++current.loop_over_25ms;
    ++current.frames; current.displayed += displayed;
    current.core_sum += core_ticks;
    if (detail_mode) {
        ++current.detail_frames;current.detail_displayed+=displayed;
        current.detail_core_sum+=core_ticks;
    } else {
        ++current.light_frames;current.light_displayed+=displayed;
        current.light_core_sum+=core_ticks;
    }
    if (core_ticks > current.core_max) current.core_max = core_ticks;
    if ((uint64_t)core_ticks*1000000u > (uint64_t)frequency*16743u) ++current.over_budget;
    ms = (unsigned)((uint64_t)core_ticks*1000u/frequency);
    ++current.histogram[ms < 63 ? ms : 63];
    if (++ppu_sample_phase==PPU_SAMPLE_PERIOD) {
        ppu_sample_phase=0;
        if (++detail_cursor==6) detail_cursor=1;
        detail_mode=detail_cursor;
    } else detail_mode=0;
    h1_profile_detail_mask=detail_masks[detail_mode];
#if H1_DIAGNOSTIC
    if (diag_sampling!=HPDS_DETAIL) { detail_mode=detail_cursor=0;h1_profile_detail_mask=0; }
#endif
    if (current.frames == 300) archive();
}
void h1_profile_video(uint32_t buffer_ticks, uint32_t scale_ticks)
{
    if (!enabled) return;
    current.video_buffer_ticks+=buffer_ticks;current.video_scale_ticks+=scale_ticks;
    ++current.video_frames;
}
static unsigned microseconds(uint64_t ticks) { return frequency ? (unsigned)(ticks*1000000u/frequency) : 0; }
void h1_profile_dump(const char *reason)
{
    uint32_t dump_tick=raw_tick(),dump_rtc=h1_wall_clock();
    unsigned dumped;
    if (depth && !fault) { fault=1;enabled=0; }
    archive();
    pc_stop(); /* Release before menu/file IO and on every normal exit. */
    enabled=h1_profile_ppu_sample=h1_profile_detail_mask=0;
    dumped=window_count;
    h1_diag_batch_begin();
    log_tcu_reads(reason);
    h1_diag("PROFILE_RUN_BOUNDARY reason=%s start_rtc=%u fault_frame=%u fault_previous_rtc=%u fault_current_rtc=%u",reason,run_start_rtc,fault_frame,fault_previous_rtc,fault_current_rtc);
    h1_diag("PROFILE_RTC_RUN reason=%s frames=%u displayed=%u interval_frames=%u rtc_start=%u rtc_end=%u seconds=%u resolution_seconds=1 valid=%u",reason,rtc_run_frames,rtc_run_displayed,rtc_run_frames?rtc_run_frames-1:0,rtc_run_start,rtc_run_last,rtc_run_last>=rtc_run_start?rtc_run_last-rtc_run_start:0,rtc_run_frames>1 && !rtc_run_invalid);
    h1_diag("PROFILE_BEGIN reason=%s windows=%u omitted=%u hz=%u fault=%u exclusive=1",reason,window_count,omitted,frequency,fault);
    h1_diag("PROFILE_LAYOUT revision=core_detail_v1 period=%u modes=5 cycle_frames=155 sampled_cost_only=1 inclusive_not_additive=1 inclusive_detail_scopes_only=1",PPU_SAMPLE_PERIOD);
    for (unsigned w=0;w<window_count;++w) {
        profile_window *p=&windows[w]; uint64_t sum=0;
        unsigned cumulative=0, p95=0;
        for (unsigned k=0;k<HP_COUNT;++k) sum+=p->ticks[k];
        for (;p95<63;++p95) { cumulative+=p->histogram[p95]; if (cumulative*100u >= p->frames*95u) break; }
        h1_diag("PROFILE_WINDOW index=%u frames=%u displayed=%u elapsed_us=%u core_mean_us=%u core_max_us=%u core_p95_bin_ms=%u over_16_743ms=%u loop_under_2ms=%u loop_over_25ms=%u",w+omitted,p->frames,p->displayed,microseconds(sum),microseconds(p->core_sum/p->frames),microseconds(p->core_max),p95,p->over_budget,p->loop_under_2ms,p->loop_over_25ms);
        h1_diag("PROFILE_VIDEO window=%u frames=%u buffer_ticks=%llu scale_ticks=%llu",w+omitted,p->video_frames,(unsigned long long)p->video_buffer_ticks,(unsigned long long)p->video_scale_ticks);
        h1_diag("PROFILE_SAMPLING window=%u detail_frames=%u detail_displayed=%u detail_core_mean_us=%u light_frames=%u light_displayed=%u light_core_mean_us=%u",w+omitted,p->detail_frames,p->detail_displayed,p->detail_frames?microseconds(p->detail_core_sum/p->detail_frames):0,p->light_frames,p->light_displayed,p->light_frames?microseconds(p->light_core_sum/p->light_frames):0);
        for (unsigned k=0;k<HP_COUNT;++k)
            h1_diag("PROFILE_COST window=%u name=%s ticks=%llu us=%u calls=%u",w+omitted,names[k],(unsigned long long)p->ticks[k],microseconds(p->ticks[k]),p->calls[k]);
    }
    /* Whole-run detail totals survive the 12-window ring. Cost percentages
     * are meaningful only within one mode; different modes never share a
     * denominator. Every recorded frame has exactly one mode, including light. */
    for (unsigned m=0;m<6;++m) {
        detail_summary *d=&details[m];unsigned n=d->frames[0]+d->frames[1];
        if (!n) continue;
        h1_diag("PROFILE_DETAIL mode=%s frames=%u displayed=%u clock_reads=%u valid=%u whole_run=1 exclusive=1 core_mean_us=%u loop_mean_us=%u",detail_names[m],n,d->frames[1],d->reads,!fault,microseconds((d->core_sum[0]+d->core_sum[1])/n),microseconds((d->loop_sum[0]+d->loop_sum[1])/n));
        for (unsigned shown=0;shown<2;++shown) if (d->frames[shown])
            h1_diag("PROFILE_DETAIL_CLASS mode=%s displayed=%u frames=%u core_mean_us=%u loop_mean_us=%u",detail_names[m],shown,d->frames[shown],microseconds(d->core_sum[shown]/d->frames[shown]),microseconds(d->loop_sum[shown]/d->frames[shown]));
        for (unsigned k=0;k<HP_COUNT;++k) if (d->calls[k] || d->ticks[k])
            h1_diag("PROFILE_DETAIL_COST mode=%s name=%s ticks=%llu us=%u inclusive_ticks=%llu inclusive_us=%u calls=%u",detail_names[m],names[k],(unsigned long long)d->ticks[k],microseconds(d->ticks[k]),(unsigned long long)d->inclusive[k],microseconds(d->inclusive[k]),d->calls[k]);
    }
    memset(details,0,sizeof details);
    h1_diag("PROFILE_END"); window_count=0;
    h1_diag_batch_end();
    h1_diag("PROFILE_DUMP_IO reason=%s windows=%u raw_ticks=%u rtc_seconds=%u",reason,dumped,raw_tick()-dump_tick,h1_wall_clock()-dump_rtc);
}
#if H1_DIAGNOSTIC
int h1_profile_diag_group_begin(unsigned group,unsigned sampling,
    unsigned expected_frames,unsigned warmup_frames)
{
    diag_group=group;diag_sampling=sampling<=HPDS_RTC_ONLY?sampling:HPDS_LIGHT;
    diag_expected=expected_frames;diag_warmup=warmup_frames;
    diag_count=diag_overflow=diag_active=diag_unbalanced=0;diag_record=0;
    if (!diag_frames) diag_frames=malloc(DIAG_FRAME_CAPACITY*sizeof *diag_frames);
    return diag_frames!=0;
}
void h1_profile_diag_frame_begin(unsigned frame,unsigned requested_skip,
    uint32_t jit_compiles,uint32_t jit_bytes)
{
    if (diag_active) { ++diag_unbalanced;diag_active=0; }
    if (!diag_frames || diag_count>=DIAG_FRAME_CAPACITY) { ++diag_overflow;diag_record=0;return; }
    diagnostic_frame *p=&diag_frames[diag_count];memset(p,0,sizeof *p);
    p->frame=frame;p->requested_skip=requested_skip;p->mode=detail_mode;
    p->jit_compiles=jit_compiles;p->jit_bytes=jit_bytes;
    gba_audio_snapshot(&p->before);
    p->raw_begin=raw_tick();p->rtc_begin=h1_wall_clock();
    p->clock_reads=clock_reads;
    p->tick_begin=enabled?h1_profile_now():0;
    p->fine_valid=enabled && !fault && !pc_fault;
    diag_record=p;diag_active=1;
}
void h1_profile_diag_frame_end(uint32_t core_ticks,unsigned displayed,
    uint32_t jit_compiles,uint32_t jit_bytes)
{
    if (!diag_active) { if (diag_record) ++diag_unbalanced;return; }
    diagnostic_frame *p=diag_record;
    uint32_t now=enabled?h1_profile_now():0;
    p->rtc_end=h1_wall_clock();p->raw_end=raw_tick();
    p->fine_valid=p->fine_valid && enabled && !fault && !pc_fault && !depth;
    p->loop_ticks=p->fine_valid?now-p->tick_begin:0;
    p->core_ticks=p->fine_valid?core_ticks:0;p->displayed=displayed;
    p->clock_reads=clock_reads-p->clock_reads;
    p->fault=fault;p->jit_compiles=jit_compiles-p->jit_compiles;p->jit_bytes_end=jit_bytes;
    gba_audio_snapshot(&p->after);
    diag_active=0;++diag_count;
}
static void diag_phase_summary(unsigned begin,unsigned end,const char *phase)
{
    unsigned valid=1,shown=0,fine=0;
    uint64_t loops=0,cores=0;
    if (begin>=end) return;
    diagnostic_frame *first=&diag_frames[begin],*last_frame=&diag_frames[end-1];
    uint32_t rtc_previous=first->rtc_begin;
    for (unsigned i=begin;i<end;++i) {
        diagnostic_frame *p=&diag_frames[i];
        if (!p->rtc_begin || p->rtc_begin<rtc_previous || p->rtc_end<p->rtc_begin) valid=0;
        rtc_previous=p->rtc_end;shown+=p->displayed;
        fine+=p->fine_valid;loops+=p->loop_ticks;cores+=p->core_ticks;
    }
    h1_diag("DIAG_PHASE group=%u phase=%s frames=%u displayed=%u rtc_begin=%u rtc_end=%u seconds=%u raw_ticks=%u rtc_valid=%u fine_frames=%u loop_ticks=%llu core_ticks=%llu",diag_group,phase,end-begin,shown,first->rtc_begin,last_frame->rtc_end,last_frame->rtc_end-first->rtc_begin,last_frame->raw_end-first->raw_begin,valid,fine,(unsigned long long)loops,(unsigned long long)cores);
}
void h1_profile_diag_dump(const char *reason)
{
    static const char *sampling_names[]={"light","detail","rtc_only"};
    unsigned warm=diag_warmup<diag_count?diag_warmup:diag_count;
    if (diag_active) ++diag_unbalanced;
    h1_diag_batch_begin();
    h1_diag("DIAG_TRACE_BEGIN revision=1 group=%u reason=%s sampling=%s recorded=%u expected=%u warmup=%u capacity=%u overflow=%u unbalanced=%u hz=%u frame_bytes=%u allocated=%u audio_snapshot=cached_no_retire",diag_group,reason,sampling_names[diag_sampling],diag_count,diag_expected,diag_warmup,DIAG_FRAME_CAPACITY,diag_overflow,diag_unbalanced,frequency,(unsigned)sizeof(diagnostic_frame),diag_frames!=0);
    h1_diag("DIAG_TRACE_SCHEMA scopes=core,render,mix,video,audio,input,rom_io,jit,save,wait,other,cpu,native,event,complete,apu_event,apu_synth,mapper,ppu,ppu_bg,ppu_sprite,ppu_pixel scopes_exclusive=1 consumed=modulo_retire_observed gap_fields=last_gap_in_frame flags=active:1,playing:2,paused:4,storage:8,catchup:16,waiting:32");
    diag_phase_summary(0,warm,"warmup");diag_phase_summary(warm,diag_count,"measure");
    for (unsigned i=0;i<diag_count;++i) {
        diagnostic_frame *p=&diag_frames[i];h1_audio_snapshot *a=&p->before,*b=&p->after;
        char scopes[HP_COUNT*12u];size_t used=0;
        for (unsigned k=0;k<HP_COUNT;++k)
            used+=(size_t)snprintf(scopes+used,sizeof(scopes)-used,"%s%u",k?",":"",p->scopes[k]);
        h1_diag("DIAG_FRAME group=%u frame=%u warm=%u mode=%u skip=%u displayed=%u fine=%u fault=%u rtc_begin=%u rtc_end=%u raw_begin=%u raw_end=%u loop=%u core=%u reads=%u compile=%u code_begin=%u code_end=%u",diag_group,p->frame,i<diag_warmup,p->mode,p->requested_skip,p->displayed,p->fine_valid,p->fault,p->rtc_begin,p->rtc_end,p->raw_begin,p->raw_end,p->loop_ticks,p->core_ticks,p->clock_reads,p->jit_compiles,p->jit_bytes,p->jit_bytes_end);
        h1_diag("DIAG_SCOPES group=%u frame=%u ticks=%s",diag_group,p->frame,scopes);
        h1_diag("DIAG_AUDIO group=%u frame=%u queued_begin=%u queued_end=%u flags_begin=%u flags_end=%u read_begin=%u read_end=%u write_begin=%u write_end=%u retired=%u produced=%u retires=%u batches=%u submits=%u drops=%u failures=%u gaps=%u missing=%u storage=%u storage_raw_ticks=%u",diag_group,p->frame,a->queued,b->queued,a->flags,b->flags,a->read_position,b->read_position,a->write_position,b->write_position,b->retired-a->retired,b->produced-a->produced,b->retire_serial-a->retire_serial,b->batches-a->batches,b->submitted-a->submitted,b->dropped-a->dropped,b->failures-a->failures,b->underruns-a->underruns,b->missing-a->missing,b->storage_pauses-a->storage_pauses,b->storage_ticks-a->storage_ticks);
        if (b->underruns!=a->underruns)
            h1_diag("DIAG_GAP group=%u frame=%u count=%u missing=%u last_queued=%u last_consumed=%u last_old_position=%u last_new_position=%u",diag_group,p->frame,b->underruns-a->underruns,b->missing-a->missing,b->gap_queued,b->gap_consumed,b->gap_old_position,b->gap_new_position);
    }
    h1_diag("DIAG_TRACE_END group=%u recorded=%u complete=%u",diag_group,diag_count,diag_count==diag_expected && !diag_overflow && !diag_unbalanced);
    h1_diag_batch_end();
    diag_active=0;
}
void h1_profile_diag_release(void)
{
    free(diag_frames);diag_frames=0;diag_record=0;
    diag_active=diag_count=0;
}
#endif
#endif
