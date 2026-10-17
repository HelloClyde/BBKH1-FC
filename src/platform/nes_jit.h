#ifndef H1_NES_JIT_H
#define H1_NES_JIT_H

/* Core/event preparation is independent from native execution. */
#ifndef H1_NATIVE_JIT
#define H1_NATIVE_JIT 0
#endif
#include <stdint.h>
typedef struct {
    int32_t frame, dmc, size;
    char dma;
} h1_jit_apu_clock;
extern h1_jit_apu_clock h1_apu_clock;
typedef uint32_t (*h1_jit_fn)(void *cpu, uint8_t *ram);
typedef struct {
    h1_jit_fn run;
    uint32_t page, epoch;
    uint8_t opcode, heat, ops, pad;
} h1_jit_entry;
extern h1_jit_entry *h1_jit_entries;
extern unsigned h1_jit_epoch;
extern uint32_t h1_jit_stats[8];
extern uint32_t h1_jit_links;
/* Counts native O32 frames, including direct calls and recursive callbacks. */
extern uint32_t h1_jit_native_depth;
extern uint32_t h1_jit_run_stats[4];
extern uint32_t h1_jit_schedule_stats[4];
/* Exact-ROM semantic acceleration. Stats: batches, loop pairs, CPU cycles. */
extern unsigned h1_hle_sgz2_enabled;
extern uint32_t h1_hle_stats[3];
int h1_nes_hle_wait(void);
/* Credentials are confined to a proved PPU frame scope or one external CPU
 * scheduling call. Public native entry
 * points always validate; only the dispatcher and native linker may use the
 * second entry after an exact validation in the current nonzero generation. */
extern uint32_t *h1_jit_contract_stamps;
extern uint32_t h1_jit_contract_epoch, h1_jit_contract_serial;
extern uint32_t h1_jit_contract_frame;
extern uint32_t h1_jit_contract_dirty;
void h1_nes_jit_frame_begin(void);
void h1_nes_jit_frame_end(void);
void h1_nes_jit_contract_refresh(void);
void h1_nes_jit_contract_wrap(void);
static inline void h1_nes_jit_contract_begin(void)
{
    if (++h1_jit_contract_serial==0) h1_nes_jit_contract_wrap();
    h1_jit_contract_epoch=h1_jit_contract_serial;
}
static inline void h1_nes_jit_contract_revoke(void)
{ h1_jit_contract_epoch=0; }
static inline void h1_nes_jit_contract_unknown(void)
{ h1_jit_contract_dirty=1;h1_nes_jit_contract_revoke(); }
/* lookup, hit, miss, stored, eviction, native reuse, reject reuse, epoch miss */
extern uint32_t h1_jit_bank_stats[8];
/* Zero means interpreter only; other values encode operand length/mode. */
extern const uint8_t h1_jit_modes[256];
void h1_nes_jit_open(unsigned mapper);
void h1_nes_jit_close(void);
void h1_nes_jit_invalidate(void);
#if H1_DIAGNOSTIC
uint32_t h1_nes_diag_jit_bytes(void);
#endif
/* Cold path only: move a bank version between the direct table and L2. */
void h1_nes_jit_bind(uint16_t pc, uint8_t opcode, uint32_t page);
/* force is for backend differential verification, never normal admission. */
void h1_nes_jit_compile(uint16_t pc, uint8_t opcode, uint32_t page, int force);
uint32_t h1_cpu_jit_read(uint32_t address);
/* Execute the opcode body after its original clock hook has already run. */
uint32_t h1_cpu_jit_complete(uint32_t pc, uint32_t opcode);
#endif
