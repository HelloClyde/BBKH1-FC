/* H1 cartridge adapter for the pinned GPL FCEUmm libretro core. */
#include "platform/core.h"
#include "platform/diagnostics.h"
#include <stdlib.h>
#include <string.h>
#include <streams/file_stream.h>
#include "fceu.h"
#include "cart.h"
#include "ines.h"
#include "unif.h"

extern CartInfo iNESCart, UNIFCart;

#define H1_NES_MAX_ROM_BYTES (4u * 1024u * 1024u + 528u)
#define H1_NES_MAX_SAVE_BYTES (1024u * 1024u)

static uint8_t *save_buffer;
static size_t save_bytes;
static CartInfo *save_cart;
static unsigned skip_count, skip_phase;
static bool loaded;

/* Validate sizes before the upstream loader makes mapper allocations. The
 * NES 2.0 exponent form can otherwise describe gigabytes in a tiny file. */
static uint64_t rom_bytes(unsigned low, unsigned high, unsigned unit)
{
    if (high == 15) {
        unsigned exponent = low >> 2;
        if (exponent > 22) return H1_NES_MAX_ROM_BYTES + 1u;
        return ((uint64_t)1 << exponent) * ((low & 3u) * 2u + 1u);
    }
    return (uint64_t)(low | (high << 8)) * unit;
}

static bool valid_header(const uint8_t header[16], uint64_t length)
{
    uint64_t prg, chr, required;
    unsigned ram = 0, i;
    bool nes2 = (header[7] & 12u) == 8u;
    if (memcmp(header, "NES\032", 4)) return false;
    prg = rom_bytes(header[4], nes2 ? header[9] & 15u : 0u, 16384);
    chr = rom_bytes(header[5], nes2 ? header[9] >> 4 : 0u, 8192);
    required = 16u + ((header[6] & 4u) ? 512u : 0u) + prg + chr;
    if (!prg || required > length || required > H1_NES_MAX_ROM_BYTES) return false;
    if (nes2) {
        for (i = 0; i < 4; ++i) {
            unsigned shift = (header[10u + i / 2u] >> ((i & 1u) * 4u)) & 15u;
            if (shift) ram += 64u << shift;
        }
        if (ram > 2u * 1024u * 1024u) return false;
    }
    return true;
}

void h1_nes_set_frameskip(unsigned count)
{
    if (count > 5) count = 5;
    if (skip_count != count) skip_phase = 0;
    skip_count = count;
}

/* Called by the prepared upstream retro_run. Skipped frames still execute
 * the CPU, PPU timing/mapper effects and APU; only the pixel output is skipped. */
int h1_nes_skip_frame(void)
{
    int skip = skip_phase != 0;
    if (++skip_phase > skip_count) skip_phase = 0;
    return skip;
}

static void sync_save(void)
{
    unsigned i;
    size_t offset = 0;
    if (!save_cart || !save_buffer) return;
    for (i = 0; i < 4; ++i) {
        size_t n = save_cart->SaveGameLen[i];
        if (!save_cart->SaveGame[i] || !n) continue;
        if (n > save_bytes - offset) return;
        memcpy(save_buffer + offset, save_cart->SaveGame[i], n);
        offset += n;
    }
}

static void save_loaded(void)
{
    unsigned i;
    size_t offset = 0;
    if (!save_cart || !save_buffer) return;
    for (i = 0; i < 4; ++i) {
        size_t n = save_cart->SaveGameLen[i];
        if (!save_cart->SaveGame[i] || !n) continue;
        if (n > save_bytes - offset) return;
        memcpy(save_cart->SaveGame[i], save_buffer + offset, n);
        offset += n;
    }
}

static void unload(void)
{
    if (loaded) retro_unload_game();
    loaded = false;
    free(save_buffer);
    save_buffer = NULL; save_cart = NULL; save_bytes = 0;
    skip_phase = 0;
}

static void deinit(void)
{
    unload();
    retro_deinit();
}

static bool load(const struct retro_game_info *game)
{
    RFILE *file;
    uint8_t header[16];
    int64_t length, got;
    unsigned i;
    unload();
    if (!game || !game->path || !*game->path) return false;
    file = filestream_open(game->path, RETRO_VFS_FILE_ACCESS_READ, 0);
    if (!file) return false;
    length = filestream_get_size(file);
    got = filestream_read(file, header, sizeof(header));
    filestream_close(file);
    if (length < 16 || length > H1_NES_MAX_ROM_BYTES || got != sizeof(header) ||
            !valid_header(header, (uint64_t)length)) {
        h1_diag("FC_INVALID_ROM bytes=%d (expected complete iNES/NES2, max 4 MiB ROM)", (int)length);
        return false;
    }
    if (!retro_load_game(game)) return false;
    loaded = true;
    /* Lock the local input to a standard pad rather than selecting a Zapper
     * or keyboard based on the database. The H1 has one physical controller. */
    retro_set_controller_port_device(0, RETRO_DEVICE_SUBCLASS(RETRO_DEVICE_JOYPAD, 1));
    retro_set_controller_port_device(1, RETRO_DEVICE_NONE);
    if (iNESCart.battery) save_cart = &iNESCart;
    else if (UNIFCart.battery) save_cart = &UNIFCart;
    if (save_cart) {
        for (i = 0; i < 4; ++i) {
            size_t n = save_cart->SaveGameLen[i];
            if (!save_cart->SaveGame[i] || !n) continue;
            if (n > H1_NES_MAX_SAVE_BYTES - save_bytes) {
                h1_diag("FC_SAVE_TOO_LARGE"); unload(); return false;
            }
            save_bytes += n;
        }
        if (save_bytes) {
            save_buffer = (uint8_t *)malloc(save_bytes);
            if (!save_buffer) { unload(); return false; }
            sync_save();
        }
    }
    h1_diag("FC_CORE_READY mapper=%d save_bytes=%u", iNESCart.mapper, (unsigned)save_bytes);
    return true;
}

static void *memory(unsigned id)
{
    return id == RETRO_MEMORY_SAVE_RAM ? save_buffer : retro_get_memory_data(id);
}

static size_t memory_size(unsigned id)
{
    return id == RETRO_MEMORY_SAVE_RAM ? save_bytes : retro_get_memory_size(id);
}

const h1_core h1_nes_core = {
    .name = "FCEUmm",
    .environment = retro_set_environment,
    .video = retro_set_video_refresh,
    .audio = retro_set_audio_sample_batch,
    .poll = retro_set_input_poll,
    .input = retro_set_input_state,
    .init = retro_init,
    .deinit = deinit,
    .load = load,
    .unload = unload,
    .run = retro_run,
    .av = retro_get_system_av_info,
    .memory = memory,
    .memory_size = memory_size,
    .save_loaded = save_loaded,
    .save_sync = sync_save,
    .state_size = retro_serialize_size,
    .state_save = retro_serialize,
    .state_load = retro_unserialize,
};
