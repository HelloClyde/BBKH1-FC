#ifndef H1_SAVE_POLICY_H
#define H1_SAVE_POLICY_H
#include <stddef.h>
/* No emulator/JIT pointer is retained here. Query the currently loaded
 * exact cartridge and its registered battery mapping at each policy call. */
typedef struct { const void *prg, *wram, *iram; } h1_save_identity;
int h1_nes_sgz2_save_identity(const void *, size_t, h1_save_identity *);
#endif
