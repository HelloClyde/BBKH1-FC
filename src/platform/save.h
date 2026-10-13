#ifndef H1_SAVE_H
#define H1_SAVE_H
#include <stdint.h>
#include <stddef.h>
#include "file_selector.h"
typedef struct {
    char path[H1_ROM_PATH_MAX + 4];
    uint32_t rom_id, generation, crc;
    int slot, loaded;
} save_store;
/* 1 = restored/written, 0 = new/unchanged, -1 = failure/corrupt existing save. */
int save_load(save_store *, const char *rom_path, void *data, size_t n);
int save_load_rtc(save_store *, const char *rom_path, void *data, size_t n);
int save_load_aux(save_store *, const char *, void *, size_t, const char *suffix);
int save_checkpoint(save_store *, const void *data, size_t n);
#endif
