#include <assert.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static void *test_malloc(size_t size);
#define malloc test_malloc
#define H1_PROFILE 1
#define H1_DIAGNOSTIC 1
#define H1_PROFILE_HOST_TEST 1
#include "../src/platform/profile.c"
#undef malloc

static unsigned fail_alloc,alloc_calls;
static void *test_malloc(size_t size) { ++alloc_calls;return fail_alloc?0:malloc(size); }

uint32_t h1_profile_test_clock;
static uint32_t test_rtc=100,test_raw;
static h1_audio_snapshot audio_state;
static unsigned snapshots,log_calls;
static char diagnostic_log[4u*1024u*1024u];
static size_t log_length;
uint32_t h1_wall_clock(void) { return test_rtc; }
uint32_t h1_profile_test_raw_tick(void) { return test_raw; }
void gba_audio_snapshot(h1_audio_snapshot *out) { *out=audio_state;++snapshots; }
void h1_diag_batch_begin(void) {}
void h1_diag_batch_end(void) {}
void h1_diag(const char *format,...)
{
    va_list args;va_start(args,format);
    int n=vsnprintf(diagnostic_log+log_length,sizeof diagnostic_log-log_length,format,args);
    va_end(args);assert(n>=0 && (size_t)n+1<sizeof diagnostic_log-log_length);
    /* Actual diagnostic writer truncates at 503 format bytes. Catch accidental
     * oversized trace records using the real formatter, not estimated widths. */
    assert(n<503);log_length+=(size_t)n;diagnostic_log[log_length++]='\n';
    diagnostic_log[log_length]=0;++log_calls;
}
static void begin(unsigned mode,unsigned count,unsigned warm)
{
    log_length=log_calls=snapshots=0;diagnostic_log[0]=0;
    memset(&audio_state,0,sizeof audio_state);
    frequency=32768;clock_attempted=0;pc_fault=0;
    h1_profile_test_clock=UINT32_MAX-200;test_raw=0;test_rtc=100;
    assert(h1_profile_diag_group_begin(7,mode,count,warm));
    assert(!log_calls);h1_profile_begin();h1_profile_start();
    assert((mode==HPDS_RTC_ONLY)==!enabled);
    log_length=log_calls=0;diagnostic_log[0]=0;
}
static void frame(unsigned index,unsigned gaps)
{
    unsigned old_logs=log_calls;
    h1_profile_diag_frame_begin(index,index%2,10+index,100+index*20);
    uint32_t start=h1_profile_now();
    h1_profile_push(HP_CORE);h1_profile_test_clock+=13;
    h1_profile_push(HP_AUDIO);h1_profile_test_clock+=7;h1_profile_pop();
    h1_profile_test_clock+=19;h1_profile_pop();
    uint32_t core=h1_profile_now()-start;
    h1_profile_push(HP_WAIT);h1_profile_test_clock+=5;h1_profile_pop();
    audio_state.underruns+=gaps;audio_state.missing+=gaps*90;
    audio_state.retired+=530;audio_state.produced+=534;audio_state.retire_serial+=3;
    audio_state.batches++;audio_state.queued=2200+index;
    audio_state.gap_queued=400;audio_state.gap_consumed=490;
    test_raw+=2;test_rtc=100+test_raw/80;
    h1_profile_diag_frame_end(core,index%2,11+index,140+index*20);
    h1_profile_frame(core,index%2);
    assert(old_logs==log_calls); /* No IO at any frame/PCM snapshot boundary. */
}
static void test_complete_and_independent_totals(void)
{
    begin(HPDS_LIGHT,1800,300);
    for (unsigned i=1;i<=1800;++i) {
        frame(i,i%7==0?2:0);
        diagnostic_frame *p=&diag_frames[i-1];
        assert(!h1_profile_detail_mask && p->mode==0 && p->fine_valid);
        assert(p->loop_ticks==44 && p->core_ticks==39);
        assert(p->scopes[HP_CORE]==32 && p->scopes[HP_AUDIO]==7 && p->scopes[HP_WAIT]==5);
        assert(p->after.retired-p->before.retired==530);
        assert(p->after.underruns-p->before.underruns==(i%7==0?2u:0u));
        assert(p->jit_compiles==1 && p->jit_bytes_end-p->jit_bytes==40);
    }
    assert(snapshots==3600 && diag_count==1800 && !diag_overflow);
    h1_profile_diag_dump("finished");
    assert(strstr(diagnostic_log,"phase=warmup frames=300 displayed=150"));
    assert(strstr(diagnostic_log,"phase=measure frames=1500 displayed=750"));
    assert(strstr(diagnostic_log,"frame=7 count=2 missing=180"));
    assert(strstr(diagnostic_log,"frame=1800 warm=0"));
    assert(strstr(diagnostic_log,"recorded=1800 complete=1"));
    assert(log_length>512u*1024u); /* Old fixed log cap cannot hold this trace. */
}
static void test_rtc_only_and_fault(void)
{
    begin(HPDS_RTC_ONLY,3,1);unsigned reads=clock_reads;
    for(unsigned i=1;i<=3;++i) frame(i,1);
    assert(clock_reads==reads && !h1_profile_now() && !h1_profile_detail_mask);
    assert(!diag_frames[2].fine_valid && diag_frames[2].loop_ticks==0);
    h1_profile_diag_dump("rtc");
    assert(strstr(diagnostic_log,"sampling=rtc_only"));
    assert(strstr(diagnostic_log,"rtc_valid=1 fine_frames=0"));
    begin(HPDS_LIGHT,3,1);frame(1,0);
    h1_profile_diag_frame_begin(2,1,0,0);
    h1_profile_push(HP_COUNT); /* Real scope failure disables fine timing. */
    ++test_raw;h1_profile_diag_frame_end(99,0,0,0);h1_profile_frame(99,0);
    frame(3,3);
    assert(diag_frames[0].fine_valid && !diag_frames[1].fine_valid && !diag_frames[2].fine_valid);
    assert(diag_frames[2].fault==1 && diag_frames[2].after.underruns==3);
    h1_profile_diag_dump("fault");
    assert(strstr(diagnostic_log,"phase=measure frames=2"));
    assert(strstr(diagnostic_log,"rtc_valid=1 fine_frames=0"));
}
static void test_limits_ordering_and_restart(void)
{
    begin(HPDS_LIGHT,1801,0);
    for(unsigned i=1;i<=1801;++i) frame(i,0);
    assert(diag_count==1800 && diag_overflow==1);
    h1_profile_diag_dump("overflow");
    assert(strstr(diagnostic_log,"recorded=1800 complete=0"));
    begin(HPDS_DETAIL,2,0);
    h1_profile_diag_frame_begin(1,1,0,0);
    h1_profile_diag_frame_begin(2,1,0,0);
    h1_profile_diag_frame_end(0,0,0,0);
    assert(diag_unbalanced==1 && diag_count==1);
    h1_profile_diag_dump("unbalanced");
    assert(strstr(diagnostic_log,"complete=0"));
    begin(HPDS_DETAIL,160,0);
    unsigned seen=0;
    for(unsigned i=1;i<=160;++i) { seen|=1u<<detail_mode;frame(i,0); }
    assert(seen==63u); /* All legacy rotating detail modes survive. */
    begin(HPDS_LIGHT,1,0);frame(1,0);h1_profile_diag_dump("restart");
    assert(!diag_overflow && !diag_unbalanced && diag_count==1 && diag_frames[0].fine_valid);
    assert(strstr(diagnostic_log,"recorded=1 complete=1"));
    /* Buffer is reused across every group, and allocation failure is explicit
     * rather than silently producing an incomplete performance comparison. */
    assert(alloc_calls==1);h1_profile_diag_release();assert(!diag_frames);
    fail_alloc=1;assert(!h1_profile_diag_group_begin(9,HPDS_LIGHT,1800,300));
    assert(!diag_frames && diag_count==0);fail_alloc=0;
    begin(HPDS_LIGHT,1,0);frame(1,0);
    assert(alloc_calls==3 && diag_count==1);h1_profile_diag_release();
}
int main(void)
{
    test_complete_and_independent_totals();test_rtc_only_and_fault();test_limits_ordering_and_restart();
    puts("diagnostic telemetry: complete 1800 frames, exact exclusive scopes, all deficits, no frame IO, RTC-only, fault survival, overflow and restart passed");
    return 0;
}
