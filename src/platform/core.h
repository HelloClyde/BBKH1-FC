#ifndef H1_CORE_H
#define H1_CORE_H
#include <libretro.h>
typedef struct {
    const char *name;
    void (*environment)(retro_environment_t);
    void (*video)(retro_video_refresh_t);
    void (*audio)(retro_audio_sample_batch_t);
    void (*poll)(retro_input_poll_t);
    void (*input)(retro_input_state_t);
    void (*init)(void), (*deinit)(void);
    bool (*load)(const struct retro_game_info *);
    void (*unload)(void), (*run)(void);
    void (*av)(struct retro_system_av_info *);
    void *(*memory)(unsigned);
    size_t (*memory_size)(unsigned);
    void (*save_loaded)(void), (*save_sync)(void);
    size_t (*state_size)(void);
    bool (*state_save)(void *, size_t), (*state_load)(const void *, size_t);
} h1_core;
extern const h1_core h1_nes_core;
void h1_nes_set_frameskip(unsigned);
#endif
