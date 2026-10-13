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
void gba_audio_pause(void);
int gba_audio_resume(void);
void gba_audio_close(void);
#endif
