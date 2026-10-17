#include <assert.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#ifdef H1_PROFILE_DISABLED_TEST
#define H1_PROFILE 0
#include "../src/platform/profile.h"
int main(void)
{
    assert(h1_profile_ppu_sample==0 && h1_profile_detail_mask==0);
    assert(h1_profile_detail_enter(HPD_CPU|HPD_NATIVE|HPD_EVENT|HPD_PPU|HPD_SOUND,
                                   HP_CPU_SAMPLE)==0);
    h1_profile_detail_leave(0);h1_profile_detail_leave(1);
    h1_profile_begin();h1_profile_start();h1_profile_push(HP_CORE);
    h1_profile_pop();h1_profile_frame(123,1);h1_profile_video(4,5);
    h1_profile_dump("disabled");
    assert(h1_profile_now()==0);
    puts("profile disabled: no timer, token or sampling dependency");
    return 0;
}
#else
#define H1_PROFILE 1
#define H1_PROFILE_HOST_TEST 1
#include "../src/platform/profile.c"

uint32_t h1_profile_test_clock;
static uint32_t test_rtc=100;
static char diagnostic_log[262144];
static size_t log_length;
uint32_t h1_wall_clock(void) { return test_rtc; }
uint32_t h1_profile_test_raw_tick(void) { return test_rtc*40u; }
void h1_diag_batch_begin(void) {}
void h1_diag_batch_end(void) {}
void h1_diag(const char *format, ...)
{
    va_list args;va_start(args,format);
    int n=vsnprintf(diagnostic_log+log_length,sizeof diagnostic_log-log_length,format,args);
    va_end(args);
    assert(n>=0 && (size_t)n+1<sizeof diagnostic_log-log_length);
    log_length+=(size_t)n;
    diagnostic_log[log_length++]='\n';diagnostic_log[log_length]=0;
}

/* This completed call tree is the independent accounting oracle. Its durations
 * are input facts, not values recovered from the profiler's clock or stack.
 * Disabled instrumentation still executes the entire body. Recursive CPU,
 * APU and PPU entries must count calls but not duplicate inclusive duration. */
#define ALWAYS (~0u)
#define COUNT(a) (sizeof(a)/sizeof((a)[0]))
typedef struct scope {
    unsigned kind,mask,before,after;
    const struct scope *children;
    unsigned count;
} scope;
#define LEAF(k,m,t) {k,m,t,0,NULL,0}
#define BRANCH(k,m,b,a,c) {k,m,b,a,c,COUNT(c)}
static const scope native_children[]={
    LEAF(HP_CPU_SAMPLE,HPD_CPU,11),
    LEAF(HP_COMPLETE_SAMPLE,HPD_NATIVE,13)
};
static const scope apu_recursion[]={LEAF(HP_APU_EVENT_SAMPLE,HPD_EVENT,29)};
static const scope event_children[]={
    BRANCH(HP_APU_EVENT_SAMPLE,HPD_EVENT,23,31,apu_recursion),
    LEAF(HP_MAPPER_SAMPLE,HPD_EVENT,37)
};
static const scope cpu_children[]={
    BRANCH(HP_NATIVE_SAMPLE,HPD_NATIVE,7,17,native_children),
    BRANCH(HP_EVENT_SAMPLE,HPD_EVENT,19,41,event_children)
};
static const scope background_children[]={LEAF(HP_PPU_PIXEL_SAMPLE,HPD_PPU,61)};
static const scope ppu_children[]={
    BRANCH(HP_PPU_BG_SAMPLE,HPD_PPU,53,67,background_children),
    LEAF(HP_PPU_SPRITE_SAMPLE,HPD_PPU,59),
    LEAF(HP_PPU_SAMPLE,HPD_PPU,71)
};
static const scope synthesis_children[]={LEAF(HP_APU_EVENT_SAMPLE,HPD_EVENT,83)};
static const scope mix_children[]={
    BRANCH(HP_APU_SYNTH_SAMPLE,HPD_SOUND,79,89,synthesis_children)
};
static const scope core_children[]={
    BRANCH(HP_CPU_SAMPLE,HPD_CPU,5,43,cpu_children),
    BRANCH(HP_PPU_SAMPLE,HPD_PPU,47,73,ppu_children),
    BRANCH(HP_MIX,ALWAYS,9,0,mix_children)
};
static const scope input_scope=LEAF(HP_INPUT,ALWAYS,2);
static const scope core_scope=BRANCH(HP_CORE,ALWAYS,3,97,core_children);
static const scope video_scope=LEAF(HP_VIDEO,ALWAYS,101);
static const scope audio_scope=LEAF(HP_AUDIO,ALWAYS,107);
static const scope wait_scope=LEAF(HP_WAIT,ALWAYS,109);
static const scope other_scope=LEAF(HP_OTHER,ALWAYS,113);

/* Independent specification: never reuse production detail_masks/cursor. */
static const unsigned expected_masks[]={0,0,HPD_CPU,
    HPD_CPU|HPD_NATIVE|HPD_EVENT,HPD_CPU|HPD_PPU,HPD_CPU|HPD_SOUND|HPD_EVENT};
static const char *expected_modes[]={"light","control","coarse","cpu","ppu","sound"};
typedef struct {
    uint64_t ticks[HP_COUNT],inclusive[HP_COUNT],core[2],loop[2];
    unsigned calls[HP_COUNT],frames[2],reads;
} expected_detail;
static expected_detail expected[6];

static unsigned body_ticks(const scope *s)
{
    unsigned total=s->before+s->after;
    for (unsigned i=0;i<s->count;++i) total+=body_ticks(&s->children[i]);
    return total;
}
static void oracle_scope(expected_detail *e,const scope *s,unsigned mask,
                         unsigned parent,uint64_t ancestors)
{
    unsigned active=s->mask==ALWAYS || (s->mask&mask);
    unsigned owner=active?s->kind:parent;
    assert(owner<HP_COUNT);
    if (active) {
        ++e->calls[s->kind];e->reads+=2;
        if (s->kind>=HP_CPU_SAMPLE && !(ancestors&(UINT64_C(1)<<s->kind)))
            e->inclusive[s->kind]+=body_ticks(s);
        ancestors|=UINT64_C(1)<<s->kind;
    }
    e->ticks[owner]+=s->before+s->after;
    for (unsigned i=0;i<s->count;++i)
        oracle_scope(e,&s->children[i],mask,owner,ancestors);
}
static void play_scope(const scope *s,unsigned mask)
{
    unsigned reads=clock_reads;
    unsigned active=s->mask==ALWAYS || (s->mask&mask);
    unsigned token;
    if (s->mask==ALWAYS) { h1_profile_push(s->kind);token=1; }
    else token=h1_profile_detail_enter(s->mask,s->kind);
    assert(token==!!active && !h1_profile_ppu_sample);
    if (!active) assert(clock_reads==reads);
    h1_profile_test_clock+=s->before;
    for (unsigned i=0;i<s->count;++i) play_scope(&s->children[i],mask);
    h1_profile_test_clock+=s->after;
    reads=clock_reads;
    h1_profile_detail_leave(token);
    if (!active) assert(clock_reads==reads);
    assert(!fault && !h1_profile_ppu_sample);
}
static void play_and_account(expected_detail *e,const scope *s,unsigned mask)
{
    oracle_scope(e,s,mask,HP_COUNT,0);
    play_scope(s,mask);
}
static uint64_t sum_ticks(const uint64_t *ticks)
{
    uint64_t sum=0;
    for (unsigned k=0;k<HP_COUNT;++k) sum+=ticks[k];
    return sum;
}
static void compare_details(void)
{
    for (unsigned m=0;m<6;++m) {
        const expected_detail *e=&expected[m];
        const detail_summary *d=&details[m];
        for (unsigned shown=0;shown<2;++shown) {
            assert(d->frames[shown]==e->frames[shown]);
            assert(d->core_sum[shown]==e->core[shown]);
            assert(d->loop_sum[shown]==e->loop[shown]);
        }
        assert(d->reads==e->reads);
        for (unsigned k=0;k<HP_COUNT;++k) {
            assert(d->ticks[k]==e->ticks[k]);
            assert(d->inclusive[k]==e->inclusive[k]);
            assert(d->calls[k]==e->calls[k]);
        }
        assert(sum_ticks(d->ticks)==e->loop[0]+e->loop[1]);
    }
}
static void fresh_run(uint32_t initial_clock)
{
    frequency=32768;pc_fault=0;test_rtc=100;
    h1_profile_test_clock=initial_clock;
    log_length=0;diagnostic_log[0]=0;
    memset(expected,0,sizeof expected);
    h1_profile_begin();
    assert(!enabled && prepared && !fault && !depth && !window_count && !omitted);
    assert(!h1_profile_ppu_sample && !h1_profile_detail_mask);
    compare_details();
    /* Startup/loading time is excluded by start(), including a long delay. */
    test_rtc+=5;h1_profile_test_clock+=200000;
    h1_profile_start();
    assert(enabled && !prepared && !fault && !depth);
    assert(detail_mode==1 && detail_cursor==1 && ppu_sample_phase==0);
    assert(!h1_profile_ppu_sample && !h1_profile_detail_mask);
    assert(previous_reads==clock_reads && frame_rtc==105);
}
static void fixture_frame(unsigned f,unsigned displayed)
{
    unsigned mode=f%31u==0 ? (f/31u)%5u+1u : 0u;
    unsigned mask=expected_masks[mode],shown=displayed!=0;
    expected_detail *e=&expected[mode];
    scope varied_core=core_scope;
    /* Different displayed/non-displayed CPU work proves class denominators
     * are not interchangeable; nonzero display counts still form one class. */
    varied_core.before+=shown?11:0;
    unsigned core=body_ticks(&varied_core),loop=core+331+(shown?101:0);
    assert(core==1067+(shown?11:0));
    assert(detail_mode==mode && h1_profile_detail_mask==mask && !h1_profile_ppu_sample);
    uint32_t before=h1_profile_test_clock;
    play_and_account(e,&input_scope,mask);
    play_and_account(e,&varied_core,mask);
    if (shown) { play_and_account(e,&video_scope,mask);h1_profile_video(4,97); }
    play_and_account(e,&audio_scope,mask);
    play_and_account(e,&wait_scope,mask);
    play_and_account(e,&other_scope,mask);
    assert(h1_profile_test_clock-before==loop && depth==0);
    ++e->frames[shown];e->core[shown]+=core;e->loop[shown]+=loop;
    test_rtc=105+f/60;
    h1_profile_frame(core,displayed);
    assert(!fault && !depth && enabled && !h1_profile_ppu_sample);
    compare_details();
}
static void check_fixed_durations(unsigned samples)
{
    /* Hand-computed checks independently validate the tree oracle itself. */
    assert(expected[2].ticks[HP_CPU_SAMPLE]==276u*samples);
    assert(expected[2].inclusive[HP_CPU_SAMPLE]==276u*samples);
    assert(expected[2].calls[HP_CPU_SAMPLE]==2u*samples);
    assert(expected[3].ticks[HP_CPU_SAMPLE]==59u*samples);
    assert(expected[3].inclusive[HP_CPU_SAMPLE]==276u*samples);
    assert(expected[3].ticks[HP_NATIVE_SAMPLE]==24u*samples);
    assert(expected[3].inclusive[HP_NATIVE_SAMPLE]==48u*samples);
    assert(expected[3].ticks[HP_EVENT_SAMPLE]==60u*samples);
    assert(expected[3].inclusive[HP_EVENT_SAMPLE]==180u*samples);
    assert(expected[3].inclusive[HP_APU_EVENT_SAMPLE]==166u*samples);
    assert(expected[3].calls[HP_APU_EVENT_SAMPLE]==3u*samples);
    assert(expected[4].ticks[HP_PPU_SAMPLE]==191u*samples);
    assert(expected[4].inclusive[HP_PPU_SAMPLE]==431u*samples);
    assert(expected[4].ticks[HP_PPU_BG_SAMPLE]==120u*samples);
    assert(expected[4].inclusive[HP_PPU_BG_SAMPLE]==181u*samples);
    assert(expected[4].ticks[HP_PPU_PIXEL_SAMPLE]==61u*samples);
    assert(expected[5].ticks[HP_APU_SYNTH_SAMPLE]==168u*samples);
    assert(expected[5].inclusive[HP_APU_SYNTH_SAMPLE]==251u*samples);
    assert(expected[5].inclusive[HP_APU_EVENT_SAMPLE]==166u*samples);
    for (unsigned mode=0;mode<2;++mode)
        for (unsigned k=HP_CPU_SAMPLE;k<HP_COUNT;++k)
            assert(!expected[mode].calls[k] && !expected[mode].ticks[k]);
}
static void validate_windows(unsigned frames,unsigned display_period)
{
    unsigned retained=0,displayed=0,detail=0,detail_displayed=0,first=omitted*300u;
    uint64_t loops=0,cores=0;
    for (unsigned w=0;w<window_count;++w) {
        profile_window *p=&windows[w];
        unsigned expect_frames=frames-first<300 ? frames-first : 300;
        unsigned shown=0,sampled=0,sampled_shown=0;
        for (unsigned f=first;f<first+expect_frames;++f) {
            shown+=f%display_period==0;
            sampled+=f%31u==0;
            sampled_shown+=f%31u==0 && f%display_period==0;
        }
        assert(p->frames==expect_frames && p->displayed==shown);
        assert(p->detail_frames==sampled && p->detail_displayed==sampled_shown);
        assert(p->light_frames+p->detail_frames==p->frames);
        assert(p->light_displayed+p->detail_displayed==p->displayed);
        assert(p->light_core_sum+p->detail_core_sum==p->core_sum);
        assert(p->core_sum==UINT64_C(1067)*expect_frames+11u*shown);
        assert(sum_ticks(p->ticks)==UINT64_C(1398)*expect_frames+112u*shown);
        assert(p->video_frames==shown && p->video_buffer_ticks==4u*shown);
        assert(p->video_scale_ticks==97u*shown);
        assert(p->over_budget==expect_frames && p->loop_over_25ms==expect_frames);
        assert(p->loop_under_2ms==0 && p->histogram[32]==expect_frames);
        assert(p->core_max==1078);
        retained+=p->frames;displayed+=shown;detail+=sampled;detail_displayed+=sampled_shown;
        loops+=sum_ticks(p->ticks);cores+=p->core_sum;first+=expect_frames;
    }
    assert(retained==frames-omitted*300u && first==frames);
    assert(loops==UINT64_C(1398)*retained+112u*displayed);
    assert(cores==UINT64_C(1067)*retained+11u*displayed);
    assert(detail && detail_displayed && detail<retained);
}
static void check_dump(unsigned frame_count,unsigned samples,unsigned display_period)
{
    char line[512];
    h1_profile_dump("oracle");
    assert(!enabled && !h1_profile_ppu_sample && !h1_profile_detail_mask && !window_count);
    assert(strstr(diagnostic_log,"PROFILE_LAYOUT revision=core_detail_v1 period=31 modes=5 cycle_frames=155 sampled_cost_only=1 inclusive_not_additive=1"));
    assert(strstr(diagnostic_log,"resolution_us=31"));
    assert(!strstr(diagnostic_log,"PROFILE_PPU_SAMPLING"));
    for (unsigned mode=0;mode<6;++mode) {
        expected_detail *e=&expected[mode];
        unsigned n=e->frames[0]+e->frames[1];
        assert(n==(mode?samples:frame_count-5*samples));
        snprintf(line,sizeof line,"PROFILE_DETAIL mode=%s frames=%u displayed=%u clock_reads=%u valid=1 whole_run=1 exclusive=1",
                 expected_modes[mode],n,e->frames[1],e->reads);
        assert(strstr(diagnostic_log,line));
        for (unsigned shown=0;shown<2;++shown) {
            assert(e->frames[shown]);
            snprintf(line,sizeof line,"PROFILE_DETAIL_CLASS mode=%s displayed=%u frames=%u core_mean_us=%u loop_mean_us=%u",
                     expected_modes[mode],shown,e->frames[shown],
                     (unsigned)((e->core[shown]/e->frames[shown])*1000000/32768),
                     (unsigned)((e->loop[shown]/e->frames[shown])*1000000/32768));
            assert(strstr(diagnostic_log,line));
        }
        if (mode) assert(e->frames[1]==samples/display_period);
    }
    snprintf(line,sizeof line,"PROFILE_DETAIL_COST mode=cpu name=cpu_dispatch_fallback_sampled ticks=%llu us=%u inclusive_ticks=%llu inclusive_us=%u calls=%u",
             (unsigned long long)(59u*samples),(unsigned)(UINT64_C(59)*samples*1000000u/32768u),
             (unsigned long long)(276u*samples),
             (unsigned)(UINT64_C(276)*samples*1000000u/32768u),2u*samples);
    assert(strstr(diagnostic_log,line));
    memset(expected,0,sizeof expected);compare_details();
}
static void test_cadence_and_accounting(unsigned display_period,unsigned frames)
{
    /* A 32-bit counter wrap is deliberately inside this run. */
    fresh_run(UINT32_MAX-200100u);
    for (unsigned f=0;f<frames;++f) fixture_frame(f,f%display_period==0);
    unsigned samples=frames/155;
    check_fixed_durations(samples);
    assert(ppu_sample_phase==0 && detail_mode==1 && detail_cursor==1);
    assert(details[1].reads==details[0].reads*samples/(frames-5*samples));
    archive();compare_details();validate_windows(frames,display_period);
    if (frames>3600) assert(window_count==12 && omitted==4);
    else assert(omitted==0);
    check_dump(frames,samples,display_period);
}
static void advance_to_coarse(void)
{
    for (unsigned f=0;f<31;++f) {
        h1_profile_push(HP_CORE);++h1_profile_test_clock;h1_profile_pop();
        h1_profile_frame(1,0);
    }
    assert(detail_mode==2 && h1_profile_detail_mask==HPD_CPU && !h1_profile_ppu_sample);
}
static int early_return(int leave_early)
{
    unsigned token=h1_profile_detail_enter(HPD_CPU,HP_CPU_SAMPLE);
    ++h1_profile_test_clock;
    if (leave_early) { h1_profile_detail_leave(token);return 13; }
    h1_profile_test_clock+=3;
    h1_profile_detail_leave(token);return 17;
}
static void test_tokens_early_return_and_quantization(void)
{
    fresh_run(0);advance_to_coarse();
    uint32_t reads=clock_reads;
    unsigned off=h1_profile_detail_enter(HPD_PPU,HP_PPU_SAMPLE);
    assert(!off && reads==clock_reads);h1_profile_detail_leave(off);
    assert(reads==clock_reads && depth==0);
    h1_profile_push(HP_CORE);
    assert(early_return(1)==13 && early_return(0)==17);
    /* Three calls within one timer tick remain three calls and zero duration.
     * The profiler cannot manufacture sub-tick CPU/APU latency estimates. */
    for (unsigned i=0;i<3;++i) {
        unsigned token=h1_profile_detail_enter(HPD_CPU,HP_NATIVE_SAMPLE);
        h1_profile_detail_leave(token);
    }
    h1_profile_pop();h1_profile_frame(5,2);
    assert(!depth && !fault && details[2].frames[1]==1 && details[2].frames[0]==0);
    assert(details[2].core_sum[1]==5 && details[2].loop_sum[1]==5);
    assert(details[2].ticks[HP_CPU_SAMPLE]==5 && details[2].inclusive[HP_CPU_SAMPLE]==5);
    assert(details[2].calls[HP_CPU_SAMPLE]==2 && details[2].calls[HP_NATIVE_SAMPLE]==3);
    assert(details[2].ticks[HP_NATIVE_SAMPLE]==0 && details[2].inclusive[HP_NATIVE_SAMPLE]==0);
    assert(microseconds(0)==0 && microseconds(1)==30 && microseconds(2)==61);
    assert(microseconds(32768)==1000000);
    h1_profile_dump("quantized");
    assert(strstr(diagnostic_log,"mode=coarse name=native_chain_sampled ticks=0 us=0 inclusive_ticks=0 inclusive_us=0 calls=3"));
    assert(strstr(diagnostic_log,"PROFILE_DETAIL_CLASS mode=coarse displayed=1 frames=1 core_mean_us=152 loop_mean_us=152"));
}
static void assert_disabled_token(unsigned token,unsigned expected_fault)
{
    unsigned reads=clock_reads,old_depth=depth;
    assert(token && !enabled && fault==expected_fault && !h1_profile_detail_mask);
    assert(!h1_profile_ppu_sample);
    h1_profile_detail_leave(token);
    assert(clock_reads==reads && depth==old_depth && fault==expected_fault);
    assert(h1_profile_detail_enter(HPD_CPU,HP_CPU_SAMPLE)==0);
    h1_profile_detail_leave(0);
    assert(clock_reads==reads);
    h1_profile_dump("fault_inside_token");
    assert(fault==expected_fault && !enabled && !h1_profile_detail_mask);
    assert(strstr(diagnostic_log,"valid=0 whole_run=1 exclusive=1"));
}
static void test_faults_and_restart(void)
{
    fresh_run(0);advance_to_coarse();
    unsigned token=h1_profile_detail_enter(HPD_CPU,HP_CPU_SAMPLE);
    test_rtc+=2;h1_profile_frame(1,0);
    assert_disabled_token(token,2);
    fresh_run(0);advance_to_coarse();
    token=h1_profile_detail_enter(HPD_CPU,HP_CPU_SAMPLE);
    h1_profile_push(HP_COUNT);assert_disabled_token(token,1);
    fresh_run(0);advance_to_coarse();
    token=h1_profile_detail_enter(HPD_CPU,HP_CPU_SAMPLE);
    h1_profile_frame(0,0); /* Unbalanced successful frame is never summarized. */
    assert(details[2].frames[0]==0 && details[2].frames[1]==0);
    assert_disabled_token(token,1);
    fresh_run(0);h1_profile_pop();
    assert(fault==1 && !enabled && !h1_profile_detail_mask && !h1_profile_ppu_sample);
    fresh_run(0);advance_to_coarse();
    for (unsigned i=0;i<17;++i) h1_profile_push(HP_CORE);
    assert(depth==16 && fault==1 && !enabled && !h1_profile_detail_mask && !h1_profile_ppu_sample);
    fresh_run(0);h1_profile_push(HP_CORE);h1_profile_dump("unfinished_scope");
    assert(fault==1 && !enabled && !h1_profile_detail_mask && !h1_profile_ppu_sample);
    /* A final healthy run proves fault state, phases, per-mode totals and
     * previous-window deltas do not leak across a pause/resume boundary. */
    fresh_run(0);fixture_frame(0,1);compare_details();
    h1_profile_dump("restart");
    assert(!fault && strstr(diagnostic_log,"mode=control frames=1 displayed=1"));
}
int main(void)
{
    test_cadence_and_accounting(2,930);
    test_cadence_and_accounting(3,930);
    test_cadence_and_accounting(6,930);
    test_cadence_and_accounting(6,4650);
    test_tokens_early_return_and_quantization();
    test_faults_and_restart();
    puts("profile sampling: independent nested accounting, mode/class cadence, whole-run archive, tokens, restart, faults and 31us quantization passed");
    return 0;
}
#endif
