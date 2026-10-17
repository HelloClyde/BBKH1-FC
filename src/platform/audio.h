#ifndef H1_GBA_AUDIO_H
#define H1_GBA_AUDIO_H
#include <stddef.h>
#include <stdint.h>
int gba_audio_open(unsigned source_rate);
/* Native codec volume, 0..100; remembered while the device is closed. */
void gba_audio_set_volume(unsigned percent);
size_t gba_audio_write(const int16_t *stereo, size_t frames);
int gba_audio_pacing(void);
int gba_audio_wait(void);
/* Hysteretic low-water signal; skip video only, never CPU/APU emulation. */
int gba_audio_catchup(void);
unsigned gba_audio_render_skip(void);
/* Synchronous storage can outlast the entire looping ring. Cancel it before
 * blocking, then restart with fresh PCM. This is an audible storage pause,
 * not an underrun fix or a claim that the storage operation became faster. */
void gba_audio_storage_begin(void);
void gba_audio_storage_end(void);
void gba_audio_pause(void);
int gba_audio_resume(void);
void gba_audio_close(void);
#if defined(H1_DIAGNOSTIC) && H1_DIAGNOSTIC
/* Cached software state only. No firmware cursor read, retirement, resampling,
 * queue mutation, clock read or device call occurs in this snapshot. consumed
 * is modulo-ring retirement observed by existing audio paths, not DMA truth. */
typedef struct {
    uint32_t queued, read_position, write_position, flags;
    uint32_t batches, submitted, dropped, failures, underruns;
    uint32_t retired, produced, retire_serial, missing;
    uint32_t storage_pauses, storage_ticks;
    uint32_t gap_queued, gap_consumed, gap_old_position, gap_new_position;
} h1_audio_snapshot;
void gba_audio_snapshot(h1_audio_snapshot *out);
#endif
#endif
