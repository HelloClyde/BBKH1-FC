#include "h1_sdk.h"
#include "audio.h"
#include "diagnostics.h"
#include "profile.h"
#include <string.h>

#define OUTPUT_RATE 32000u
#define BLOCK_SAMPLES 8192u
#define PREFILL 4096u
#define SLOTS 1u
typedef struct {
    int16_t *pcm;
    uint32_t bytes;
    uint32_t private_words[6];
} pcm_descriptor;
typedef int (*descriptor_fn)(pcm_descriptor *);
typedef int (*device_fn)(void);
typedef int (*open_fn)(uint32_t *);
typedef int (*submit_fn)(int, pcm_descriptor *, int, int);
typedef void (*volume_set_fn)(int);
static volume_set_fn set_volume;
static device_fn get_volume;
static unsigned volume=100, original_volume, volume_owned;
static pcm_descriptor descriptors[SLOTS] __attribute__((aligned(32)));
static int16_t samples[SLOTS][BLOCK_SAMPLES] __attribute__((aligned(32)));
static descriptor_fn initialize, destroy;
static device_fn start, stop, close_device;
static submit_fn submit;
static volatile uint32_t *queue;
static unsigned initialized, active, rate, phase, used, read_position, queued, playing;
static int32_t accumulator;
static unsigned batches, submitted, dropped, failures, nonzero, underruns;
static unsigned waiting, progress_tick, progress_position;
static unsigned paused;
#if H1_PROFILE
extern uint32_t h1_wall_clock(void);
typedef struct {
    unsigned batch,raw_tick,rtc,queued,consumed,old_position,new_position,reset_ticks;
} audio_gap;
static audio_gap gaps[16];
static unsigned gap_count,gap_total;
static void record_gap(unsigned consumed,unsigned position)
{
    audio_gap *p=&gaps[gap_count%16];
    p->batch=batches;p->raw_tick=h1_raw_tick_80hz();p->rtc=h1_wall_clock();
    p->queued=queued;p->consumed=consumed;p->old_position=read_position;p->new_position=position;
    p->reset_ticks=0;
    ++gap_count;++gap_total;
}
static void finish_gap(void)
{
    audio_gap *p=&gaps[(gap_count-1)%16];
    p->reset_ticks=h1_raw_tick_80hz()-p->raw_tick;
}
static void dump_gaps(const char *reason)
{
    unsigned start=gap_count>16 ? gap_count-16 : 0;
    h1_diag_batch_begin();
    h1_diag("AUDIO_GAPS reason=%s count=%u omitted=%u total=%u",reason,gap_count-start,start,gap_total);
    for (unsigned i=start;i<gap_count;++i) {
        audio_gap *p=&gaps[i%16];
        h1_diag("AUDIO_GAP batch=%u rtc=%u raw_tick=%u queued=%u consumed=%u missing=%u old_position=%u new_position=%u reset_ticks=%u",p->batch,p->rtc,p->raw_tick,p->queued,p->consumed,p->consumed-p->queued,p->old_position,p->new_position,p->reset_ticks);
    }
    gap_count=0;
    h1_diag_batch_end();
}
#else
static void record_gap(unsigned consumed,unsigned position) { (void)consumed;(void)position; }
static void finish_gap(void) {}
static void dump_gaps(const char *reason) { (void)reason; }
#endif

static void *service(unsigned offset)
{
    return h1_runtime_entry(h1_runtime_table(H1_RUNTIME_SYS_TABLE_SLOT), offset);
}

static void resolve_volume(void)
{
    const uint32_t *setter=service(0x40u), *getter=service(0x44u);
    /* V1.41 setter clamps to 100; getter loads a global in its return slot.
     * Reject unknown implementations rather than calling an unverified ABI. */
    set_volume=0;get_volume=0;
    if (!setter || !getter || setter[0]!=0x28850065u || setter[1]!=0x24030064u ||
        (getter[0]&0xffff0000u)!=0x3c020000u || getter[1]!=0x03e00008u ||
        (getter[2]&0xffff0000u)!=0x8c420000u) return;
    set_volume=(volume_set_fn)setter;get_volume=(device_fn)getter;
}
void gba_audio_set_volume(unsigned percent)
{
    volume=percent>100u?100u:percent;
    if (active && volume_owned) {
        set_volume((int)volume);
        h1_diag("AUDIO_VOLUME percent=%u native=1",volume);
    }
}

/* V1.41 has no public completion callback. Resolve the queue's address from
 * the supplied submit implementation, accepting only its verified instruction
 * sequence. Queue nodes contain next, descriptor, cursor, end, repeats, flags.
 * This is read-only; never assume a fixed firmware RAM address. */
static volatile uint32_t *resolve_queue(submit_fn function)
{
    const uint32_t *code = (const uint32_t *)function;
    uint32_t address;
    if (!code || (code[0x70 / 4] & 0xffff0000u) != 0x3c060000u ||
        (code[0x74 / 4] & 0xffff0000u) != 0x24c60000u ||
        code[0x78 / 4] != 0x00c43021u)
        return 0;
    address = ((code[0x70 / 4] & 65535u) << 16) + (int16_t)code[0x74 / 4];
    if ((address & 3u) || address < 0x80000000u || address >= 0x84000000u) return 0;
    return (volatile uint32_t *)address;
}
static void reset_ring(void)
{
    /* Cancellation is serialized with the firmware mixer. Only then clear
     * samples or restart the ring, so no active source can be overwritten. */
    submit(0, 0, 0, 0);
    playing = queued = used = read_position = 0;
    memset(samples, 0, sizeof(samples));
}
static void retire(void)
{
    volatile uint32_t *node;
    uint32_t address, cursor, base = (uint32_t)samples[0];
    unsigned position, consumed, i;
    if (!playing) return;
    node = (volatile uint32_t *)queue[0]; address = (uint32_t)node;
    if (!node || (address & 3u) || address < 0x80000000u || address >= 0x84000000u ||
        node[1] != (uint32_t)&descriptors[0]) { ++failures; reset_ring(); return; }
    cursor = node[2];
    if (cursor < base || cursor > base + sizeof(samples) || (cursor & 1u)) {
        ++failures; reset_ring(); return;
    }
    position = ((cursor - base) / 2) % BLOCK_SAMPLES;
    consumed = (position + BLOCK_SAMPLES - read_position) % BLOCK_SAMPLES;
    if (consumed > queued) { record_gap(consumed,position);++underruns; reset_ring(); finish_gap();return; }
    /* Erase retired data; a slow producer then yields silence, not old audio.
     * Unread samples remain owned by the mixer until cursor passes them. */
    for (i = 0; i < consumed; ++i) samples[0][(read_position + i) % BLOCK_SAMPLES] = 0;
    read_position = position; queued -= consumed;
}
int gba_audio_open(unsigned source_rate)
{
    uint32_t config[9] = {OUTPUT_RATE, 1, 4096, 0, 0, 0, 0, 0, 0};
    open_fn open_device = (open_fn)service(0x58u);
    int result, previous=-1;
    unsigned i;
    gba_audio_close();
    resolve_volume();
    if (get_volume) previous=get_volume();
    initialize = (descriptor_fn)service(0x50u); destroy = (descriptor_fn)service(0x54u);
    submit = (submit_fn)service(0x5cu); start = (device_fn)service(0x60u);
    stop = (device_fn)service(0x64u); close_device = (device_fn)service(0x68u);
    queue = resolve_queue(submit);
    h1_diag("AUDIO_OPEN_BEGIN source=%u output=%u mono=1 queue=%p", source_rate, OUTPUT_RATE, (void *)queue);
    if (!source_rate || !queue || !initialize || !destroy || !open_device || !start || !stop || !close_device) {
        h1_diag("AUDIO_OPEN_END ready=0 reason=unsupported_services"); return 0;
    }
    memset(descriptors, 0, sizeof(descriptors)); memset(samples, 0, sizeof(samples));
    initialized = batches = submitted = dropped = failures = nonzero = underruns = 0;
#if H1_PROFILE
    gap_count=gap_total=0;
#endif
    rate = source_rate; phase = used = read_position = queued = playing = 0; accumulator = 0;
    waiting = paused = 0;
    for (i = 0; i < SLOTS; ++i) {
        descriptors[i].pcm = samples[i]; descriptors[i].bytes = BLOCK_SAMPLES * 2;
        h1_diag("AUDIO_DESCRIPTOR_BEGIN slot=%u", i);
        result = initialize(&descriptors[i]);
        h1_diag("AUDIO_DESCRIPTOR_END slot=%u rc=%d", i, result);
        if (!result) { gba_audio_close(); return 0; }
        ++initialized;
    }
    h1_diag("AUDIO_DEVICE_BEGIN"); result = open_device(config);
    h1_diag("AUDIO_DEVICE_END rc=%d", result);
    if (!result) { gba_audio_close(); return 0; }
    active = 1;
    if (set_volume && get_volume) {
        if (previous>=0 && previous<=100) {
            original_volume=(unsigned)previous;volume_owned=1;
            gba_audio_set_volume(volume);
        }
    }
    if (!volume_owned) h1_diag("AUDIO_VOLUME unavailable=1");
    h1_diag("AUDIO_START_BEGIN"); result = start(); h1_diag("AUDIO_START_END rc=%d", result);
    if (!result) { gba_audio_close(); return 0; }
    h1_diag("AUDIO_OPEN_END ready=1"); return 1;
}
static void output(int16_t sample)
{
    if (queued == BLOCK_SAMPLES) { ++dropped; return; }
    samples[0][used] = sample;
    used = (used + 1) % BLOCK_SAMPLES; ++queued;
    if (sample) ++nonzero;
    if (!playing && queued >= PREFILL) {
        int result, trace = submitted < 4 && h1_diag_is_verbose();
        if (trace) h1_diag("AUDIO_SUBMIT_BEGIN ring_bytes=%u prefill=%u", BLOCK_SAMPLES * 2u, PREFILL);
        /* One persistent ring avoids V1.41's short-block queue counter growth.
         * repeats=0 loops; cursor retirement above controls buffer ownership. */
        result = submit(0, &descriptors[0], 0, 0);
        if (trace) h1_diag("AUDIO_SUBMIT_END rc=%d", result);
        if (!result) { ++failures; queued = used = 0; }
        else playing = 1;
        ++submitted;
    }
}
size_t gba_audio_write(const int16_t *stereo, size_t frames)
{
    size_t i;
    if (!active || paused || !stereo) return frames;
    ++batches;
    retire();
    /* Weighted box resampling, preserving fractional position across batches.
     * Fold stereo to mono before filtering; accumulation stays within int32. */
    for (i = 0; i < frames; ++i) {
        int32_t sample = ((int32_t)stereo[i * 2] + stereo[i * 2 + 1]) / 2;
        unsigned remaining = OUTPUT_RATE;
        while (remaining) {
            unsigned weight = rate - phase;
            if (weight > remaining) weight = remaining;
            accumulator += sample * (int32_t)weight;
            phase += weight; remaining -= weight;
            if (phase == rate) {
                int32_t average = rate == 32768u ? accumulator / 32768 :
                                  rate == 65536u ? accumulator / 65536 : accumulator / (int32_t)rate;
                output((int16_t)average); phase = 0; accumulator = 0;
            }
        }
    }
    return frames;
}
int gba_audio_pacing(void) { return active && !paused; }
int gba_audio_wait(void)
{
    unsigned now;
    if (!active || paused) return 0;
    retire();
    if (!playing || queued <= PREFILL) { waiting = 0; return 0; }
    now = h1_raw_tick_80hz();
    if (!waiting || progress_position != read_position) {
        waiting = 1; progress_tick = now; progress_position = read_position;
    } else if ((unsigned)(now - progress_tick) > 80u) {
        h1_diag("AUDIO_STALL disabling_output=1");
        gba_audio_close(); return 0;
    }
    return 1;
}
void gba_audio_pause(void)
{
    if (!active || paused) return;
    stop(); reset_ring();
    phase=0; accumulator=0; waiting=0; paused=1;
    h1_diag("AUDIO_PAUSE batches=%u underruns=%u failures=%u",batches,underruns,failures);
    dump_gaps("pause");
}
int gba_audio_resume(void)
{
    if (!active) return 0;
    if (!paused) return 1;
    /* Keep the initialized device and descriptor. Logging and storage work
     * must finish before restarting DMA, otherwise the empty ring can starve. */
    if (!start()) { gba_audio_close(); return 0; }
    paused=0; waiting=0;
    return 1;
}
void gba_audio_close(void)
{
    unsigned i;
    if (active) {
        h1_diag("AUDIO_CLOSE_BEGIN batches=%u submitted=%u nonzero=%u dropped=%u failures=%u underruns=%u", batches, submitted, nonzero, dropped, failures, underruns);
        stop(); submit(0, 0, 0, 0); close_device(); active = 0;
        if (volume_owned) {
            set_volume((int)original_volume);volume_owned=0;
            h1_diag("AUDIO_VOLUME_RESTORE percent=%u",original_volume);
        }
        dump_gaps("close");
    }
    for (i = 0; i < initialized; ++i) destroy(&descriptors[i]);
    if (initialized) h1_diag("AUDIO_CLOSE_END descriptors=%u", initialized);
    initialized = 0; queue = 0; used = playing = queued = paused = 0;
}
