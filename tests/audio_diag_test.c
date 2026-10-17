#include <assert.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#define H1_PROFILE 0
#define H1_DIAGNOSTIC 1
#define H1_RUNTIME_SYS_TABLE_SLOT 0u
unsigned h1_raw_tick_80hz(void);
#include "../src/platform/audio.c"

static unsigned external_calls;
unsigned h1_raw_tick_80hz(void) { ++external_calls;return 0; }
void *h1_runtime_table(uint32_t slot) { (void)slot;++external_calls;return 0; }
void *h1_runtime_entry(void *table,uint32_t offset) { (void)table;(void)offset;++external_calls;return 0; }
void h1_diag(const char *format,...) { (void)format;++external_calls; }
int h1_diag_is_verbose(void) { ++external_calls;return 0; }

int main(void)
{
    h1_audio_snapshot first,second;
    /* A dangerous cursor address proves snapshot does not refresh firmware
     * state. active+playing would make any accidental retire dereference it. */
    queue=(volatile uint32_t *)(uintptr_t)1;
    active=playing=1;paused=storage_paused=catchup=waiting=0;
    queued=1234;used=2345;read_position=1111;batches=17;underruns=29;
    diag_retired=40000;diag_produced=41234;diag_retire_serial=71;diag_missing=63;
    storage_pauses=3;storage_ticks=120;diag_gap_queued=90;diag_gap_consumed=111;
    gba_audio_snapshot(&first);gba_audio_snapshot(&second);
    assert(!memcmp(&first,&second,sizeof first) && external_calls==0);
    assert(first.queued==1234 && first.flags==3 && first.retired==40000 && first.produced==41234);
    assert(first.retire_serial==71 && first.underruns==29 && first.missing==63);
    assert(first.storage_pauses==3 && first.storage_ticks==120 && first.gap_consumed==111);
    assert(queued==1234 && used==2345 && read_position==1111 && queue==(volatile uint32_t *)(uintptr_t)1);
    /* Exercise the real matching-rate producer without queue submissions.
     * The telemetry counts accepted samples including zero-valued PCM. */
    playing=0;queued=used=0;diag_produced=0;rate=32000;
    int16_t pcm[16]={100,-100,600,400,-5,1,0,0};
    assert(gba_audio_write(pcm,8)==8 && diag_produced==8 && queued==8);
    assert(samples[0][0]==0 && samples[0][1]==500 && samples[0][2]==-2);
    paused=1;assert(gba_audio_write(pcm,8)==8 && diag_produced==8 && queued==8);
    paused=0;playing=1;queue=0;active=0; /* No external calls during snapshot/write above. */
    assert(!external_calls);
    puts("audio diagnostic snapshot: cached-only reads, poisoned queue safety, no device/clock calls, accepted PCM counters passed");
    return 0;
}
