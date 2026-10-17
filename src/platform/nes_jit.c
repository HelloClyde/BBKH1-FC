/* Original 6502 -> MIPS32 block emitter. GPL-2.0-or-later.
 * Interrupt entry and unsupported operations retain FCEUmm's interpreter.
 * IO writes call the original handler as the final instruction of a block.
 * Decoded pure runs commit clocks together only within a proved event-free
 * interval. Other instructions retain exact boundaries and original hooks.
 * Native edges share one O32 frame. Exact entry validation grants a credential
 * confined to the current CPU slice and revoked around opaque callbacks. */
#include "nes_jit.h"
#include "diagnostics.h"
#include "fceu.h"
#include "cart.h"
#include "x6502.h"
#include "sound.h"
#include "ppu.h"
#include "input.h"
#include <stddef.h>
#include <stdlib.h>
#include <string.h>

#ifndef H1_JIT_CACHE_MIB
#define H1_JIT_CACHE_MIB 8
#endif
#define WORDS 2048u
#define MAX_OPS 16u
#define PROLOGUE_WORDS 19u
#define CHAIN_LIMIT (32768u-MAX_OPS) /* packet count has only 15 bits */
#define BANK_SETS 2048u
#define BANK_WAYS 4u
extern uint8_t PRGIsRAM[32];
extern uint8_t ZNTable[256];
extern void NamcoIRQHook(int cycles);
extern DECLFR(h1_cpu_jit_namco_wram);
extern DECLFR(h1_cpu_jit_raml);
extern DECLFR(h1_cpu_jit_ramh);
extern DECLFW(h1_cpu_jit_ram_write_l);
extern DECLFW(h1_cpu_jit_ram_write_h);
extern uint16_t h1_namco_irq_count;
extern uint8_t h1_namco_irq_enabled;
extern writefunc const h1_ppu_code_safe_writes[7];
extern uint32_t h1_cpu_apu_deadline;
static int stable_policy;
static uintptr_t known_irq;
static readfunc known_status_read;
extern CartInfo iNESCart;
unsigned h1_hle_sgz2_enabled;
uint32_t h1_hle_stats[3];
static uint8_t *hle_sgz2_page;

/* SGZ2-CN: LDA $62; BNE $FB28. The flag is cleared by Namco IRQ code.
 * Replace only complete nonzero iterations within the existing CPU slice
 * and strictly before any hardware event. No PPU boundary is moved. */
int h1_nes_hle_wait(void)
{
    if (!h1_hle_sgz2_enabled || X.count <= 432 || !RAM[0x62] ||
        X.IRQlow || X.jammed || encryptOpcodes || overclock_enabled || overclocked ||
        MapIRQHook != NamcoIRQHook || PPU_hook || GameHBIRQHook || GameHBIRQHook2 ||
        InputScanlineHook || MMC5Hack || PRGIsRAM[31] || Page[31] != hle_sgz2_page ||
        ARead[0xfb28] != CartBR || ARead[0xfb29] != CartBR ||
        ARead[0xfb2a] != CartBR || ARead[0xfb2b] != CartBR)
        return 0;
    uint32_t code;
    __builtin_memcpy(&code, Page[31] + 0xfb28, sizeof code);
    if (code != 0xfcd062a5u || X.tcount < 0 || X.tcount > 255 ||
        h1_apu_clock.frame <= 0 || h1_apu_clock.dmc <= 0 ||
        (h1_apu_clock.size && !h1_apu_clock.dma)) return 0;
    /* Exact unsigned constant quotients. Spell out multiply-high so the
     * size-oriented MIPS build does not emit three hardware DIVU per batch.
     * floor(x/48)=floor((x>>4)/3); floor(x/288)=floor((x>>5)/9). */
    unsigned limit = (unsigned)(((uint64_t)(((unsigned)h1_apu_clock.frame - 1u) >> 4)
                                  * 0xaaaaaaabu) >> 33);
    unsigned dmc = (unsigned)h1_apu_clock.dmc - 1u;
    if (limit > dmc) limit = dmc;
    if (h1_namco_irq_enabled) {
        if (h1_namco_irq_count >= 0x7fff) return 0;
        unsigned irq = 0x7ffeu - h1_namco_irq_count;
        if (limit > irq) limit = irq;
    }
    unsigned carry = (unsigned)X.tcount;
    if (limit < carry + 11u) return 0;
    unsigned pairs = (unsigned)(((uint64_t)(((unsigned)X.count + 143u) >> 5)
                                  * 0x38e38e39u) >> 33);
    unsigned event_pairs = (unsigned)(((uint64_t)((limit - carry + 1u) >> 1)
                                        * 0xaaaaaaabu) >> 33);
    if (pairs > event_pairs) pairs = event_pairs;
    if (pairs < 2u) return 0;
    unsigned cycles = pairs * 6u;
    unsigned hardware = carry + cycles - 1u;
    X.count -= (int32_t)(cycles * 48u);
    timestamp += cycles;
    sound_timestamp += cycles;
    h1_apu_clock.frame -= (int32_t)(hardware * 48u);
    h1_apu_clock.dmc -= (int32_t)hardware;
    if (h1_namco_irq_enabled)
        h1_namco_irq_count = (uint16_t)(h1_namco_irq_count + hardware);
    /* Each last BNE leaves its extra taken cycle for the next hook. */
    X.tcount = 1;
    X.A = RAM[0x62];
    X.P = (X.P & ~(N_FLAG | Z_FLAG)) | (X.A & N_FLAG);
    X.mooPI = X.P;
    X.DB = 0xfc;
    /* The dispatcher keeps its next PC locally. Before the next clock hook,
     * X.PC still names the last executed BNE; the run epilogue writes FB28. */
    X.PC = 0xfb2a;
    h1_cpu_apu_deadline = timestamp;
    ++h1_hle_stats[0]; h1_hle_stats[1] += pairs; h1_hle_stats[2] += cycles;
    return 1;
}
_Static_assert(offsetof(X6502, A) == 6 && offsetof(X6502, P) == 10 &&
               offsetof(X6502, count) == 16 && offsetof(X6502, DB) == 24,
               "JIT requires the pinned X6502 ABI");

const uint8_t h1_jit_modes[256] = {
    [0xea]=1, [0x18]=1, [0x38]=1, [0x58]=1, [0x78]=1, [0xb8]=1,
    [0xd8]=1, [0xf8]=1,
    [0x0a]=1, [0x4a]=1, [0x2a]=1, [0x6a]=1,
    [0xaa]=1, [0xa8]=1, [0x8a]=1, [0x98]=1, [0xba]=1, [0x9a]=1,
    [0xe8]=1, [0xc8]=1, [0xca]=1, [0x88]=1,
    [0x48]=1, [0x68]=1, [0x60]=1,
    [0xa9]=2, [0xa2]=2, [0xa0]=2, [0xa5]=2, [0xa6]=2, [0xa4]=2,
    [0xb5]=2, [0xb6]=2, [0xb4]=2,
    [0x85]=2, [0x86]=2, [0x84]=2, [0x95]=2, [0x96]=2, [0x94]=2,
    [0x29]=2, [0x25]=2, [0x35]=2, [0x49]=2, [0x45]=2, [0x55]=2,
    [0x09]=2, [0x05]=2, [0x15]=2,
    [0xc9]=2, [0xc5]=2, [0xd5]=2, [0xe0]=2, [0xe4]=2, [0xc0]=2, [0xc4]=2,
    [0xe6]=2, [0xf6]=2, [0xc6]=2, [0xd6]=2,
    [0x24]=2, [0x2c]=4, [0xad]=4, [0xae]=4, [0xac]=4,
    [0x69]=2, [0x65]=2, [0x75]=2, [0xe9]=2, [0xe5]=2, [0xf5]=2,
    [0x8d]=6, [0x8e]=6, [0x8c]=6,
    [0xbd]=7, [0xb9]=7, [0xbe]=7, [0xbc]=7,
    [0xee]=8, [0xce]=8, [0x6c]=9,
    [0x4c]=5, [0x20]=5,
    [0x10]=3, [0x30]=3, [0x50]=3, [0x70]=3,
    [0x90]=3, [0xb0]=3, [0xd0]=3, [0xf0]=3,
};

typedef struct {
    uint32_t epoch, page;
    uint16_t pc, words;
    uint8_t opcode, ops;
    uint8_t padding[18];
    uint32_t code[WORDS];
} block;
_Static_assert(offsetof(block, code) == 32 && sizeof(block) % 32 == 0,
               "Cache entries and generated code must be aligned");
static void *allocation;
static uint8_t *cache;
static size_t cache_bytes, used_bytes;
#if H1_DIAGNOSTIC
uint32_t h1_nes_diag_jit_bytes(void) { return (uint32_t)used_bytes; }
#endif
/* Each version has exactly one owner: either its CPU-PC L1 slot or one L2
 * slot. Moving, rather than copying, makes the existing native stale store
 * (L1 epoch=0) permanently discard that version, including chained targets.
 * The Page bias plus CPU PC identifies both physical code and CPU semantics;
 * the emitted content/handler/hook guards still run after every restoration. */
typedef struct {
    h1_jit_entry entry;
    uint16_t pc, reserved;
} bank_entry;
_Static_assert(sizeof(bank_entry)==20, "Bounded ROM-bank metadata size");
static bank_entry *bank_entries;
static uint8_t *bank_victims;
h1_jit_entry *h1_jit_entries;
unsigned h1_jit_epoch;
uint32_t h1_jit_wraps;
uint32_t h1_jit_native_depth;
const uint32_t h1_jit_prologue_words=PROLOGUE_WORDS;
uint32_t h1_jit_links;
uint32_t h1_jit_run_stats[4]; /* compiled runs/ops, executed/rejected runs */
uint32_t h1_jit_schedule_stats[4]; /* full runs, CPU prefixes, event prefixes, reserved */
uint32_t *h1_jit_contract_stamps;
uint32_t h1_jit_contract_epoch, h1_jit_contract_serial;
uint32_t h1_jit_contract_frame;
uint32_t h1_jit_contract_dirty;
/* Readable in differential tests, and printed before releasing the arena. */
uint32_t h1_jit_stats[8]; /* compile, hit, miss, native op, deopt, reject, clear, max words */
uint32_t h1_jit_bank_stats[8];

#include "jit_backoff.h"
h1_jit_backoff_context h1_jit_backoff_state;
_Static_assert(sizeof(h1_jit_backoff_context)==132,"JIT backoff context ABI");
#if H1_NATIVE_JIT
#include "jit_backoff.inc"
#else
/* Default C keeps the existing readiness/allocation contract. It never needs
 * native performance sampling or an adaptive mode transition. */
#define backoff_reset() ((void)0)
#define backoff_frame_begin() ((void)0)
#define backoff_frame_end() ((void)0)
#define backoff_compile_end(force) ((void)(force))
#endif

static unsigned bank_set(uint16_t pc, uint32_t page)
{
    unsigned hash=(page>>11)^((unsigned)pc*0x9e37u);
    return (hash^(hash>>11))&(BANK_SETS-1u);
}
static void bank_store(uint16_t pc, const h1_jit_entry *entry)
{
    if (!bank_entries || entry->epoch!=h1_jit_epoch ||
        (!entry->run && !entry->heat)) return;
    unsigned set=bank_set(pc,entry->page), way;
    bank_entry *slots=bank_entries+set*BANK_WAYS;
    for(way=0;way<BANK_WAYS;++way)
        if(slots[way].entry.epoch!=h1_jit_epoch)break;
    if(way==BANK_WAYS) {
        way=bank_victims[set];++h1_jit_bank_stats[4];
    }
    bank_victims[set]=(uint8_t)((way+1u)%BANK_WAYS);
    slots[way].pc=pc;slots[way].entry=*entry;++h1_jit_bank_stats[3];
}
void h1_nes_jit_bind(uint16_t pc, uint8_t opcode, uint32_t page)
{
    if (!h1_jit_entries) return;
    h1_jit_entry *entry=&h1_jit_entries[pc], replacement={0};
    if(entry->epoch==h1_jit_epoch && entry->page==page && entry->opcode==opcode)return;
    h1_jit_contract_stamps[pc]=0;
    ++h1_jit_bank_stats[0];
    if(entry->epoch!=h1_jit_epoch)++h1_jit_bank_stats[7];
    if(bank_entries) {
        bank_entry *slots=bank_entries+bank_set(pc,page)*BANK_WAYS;
        for(unsigned way=0;way<BANK_WAYS;++way) {
            bank_entry *candidate=&slots[way];
            if(candidate->entry.epoch==h1_jit_epoch && candidate->pc==pc &&
               candidate->entry.page==page && candidate->entry.opcode==opcode) {
                replacement=candidate->entry;
                /* Remove before storing the displaced L1: both may hash to
                 * the same set, and no executable version may have two owners. */
                candidate->entry.epoch=0;
                ++h1_jit_bank_stats[1];
                if(replacement.run)++h1_jit_bank_stats[5];
                else if(replacement.heat==255)++h1_jit_bank_stats[6];
                break;
            }
        }
    }
    bank_store(pc,entry);
    if(!replacement.epoch) {
        replacement.page=page;replacement.opcode=opcode;
        replacement.epoch=h1_jit_epoch;++h1_jit_bank_stats[2];
    }
    *entry=replacement;
}

enum { ZERO=0, V0=2, A0=4, A1=5, A2=6, A3=7, T0=8, T1=9,
       T2=10, T3=11, T4=12, T5=13, T6=14, T7=15,
       S0=16, S1=17, S2=18, S3=19, S4=20, S5=21, S6=22, S7=23,
       T8=24, T9=25, SP=29, RA=31 };
typedef struct {
    block *b;
    unsigned n, exits[MAX_OPS*10+16], exits_n;
    unsigned stale[MAX_OPS*10+16], stale_n;
    unsigned page_miss;
    unsigned ends[MAX_OPS], ends_n;
    unsigned run_exits[MAX_OPS], run_exits_n;
    uint16_t pcs[MAX_OPS]; unsigned labels[MAX_OPS], emitted;
    uint8_t modes[MAX_OPS];
    int failed, fast, overclock, run_remaining, run_full;
} emitter;
static void emit(emitter *e, uint32_t word)
{
    if (e->n >= WORDS) { e->failed=1; return; }
    e->b->code[e->n++]=word;
}
static void imm(emitter *e, unsigned op, unsigned rt, unsigned rs, int value)
{ emit(e,(op<<26)|(rs<<21)|(rt<<16)|(uint16_t)value); }
static void reg(emitter *e, unsigned fn, unsigned rd, unsigned rs, unsigned rt)
{ emit(e,(rs<<21)|(rt<<16)|(rd<<11)|fn); }
static void shift(emitter *e, unsigned fn, unsigned rd, unsigned rt, unsigned n)
{ emit(e,(rt<<16)|(rd<<11)|(n<<6)|fn); }
static void constant(emitter *e, unsigned r, uint32_t value)
{
    imm(e,15,r,ZERO,value>>16);
    imm(e,13,r,r,value);
}
static void mov(emitter *e, unsigned dest, unsigned source)
{ reg(e,33,dest,source,ZERO); }
static unsigned branch(emitter *e, unsigned op, unsigned a, unsigned b)
{
    unsigned at=e->n;
    imm(e,op,b,a,0); emit(e,0); return at;
}
static void patch(emitter *e, unsigned at, unsigned target)
{ if (!e->failed) e->b->code[at] |= (uint16_t)(target-at-1); }
static void exit_if(emitter *e, unsigned op, unsigned a, unsigned b)
{ e->exits[e->exits_n++]=branch(e,op,a,b); }
static void stale_if(emitter *e, unsigned op, unsigned a, unsigned b)
{ e->stale[e->stale_n++]=branch(e,op,a,b); }
static void call(emitter *e, uintptr_t target)
{ emit(e,(3u<<26)|((target>>2)&0x03ffffff));emit(e,0); }
static void load_global(emitter *e, unsigned dest, const void *address)
{ constant(e,T7,(uintptr_t)address);imm(e,35,dest,T7,0); }
static void revoke_contract(emitter *e)
{
    /* T7 is a temporary; preserve callback arguments, return values and the
     * original call's delay-slot operand. Zero disables reuse for this slice. */
    constant(e,T7,(uintptr_t)&h1_jit_contract_epoch);imm(e,43,ZERO,T7,0);
}
static void unknown_contract(emitter *e)
{
    revoke_contract(e);
    constant(e,T7,(uintptr_t)&h1_jit_contract_dirty);imm(e,13,T0,ZERO,1);imm(e,43,T0,T7,0);
    /* The C scheduler may carry an absolute timestamp deadline across this
     * native call. Retire it before any callback can change APU timing. */
    constant(e,T7,(uintptr_t)&timestamp);imm(e,35,T0,T7,0);
    constant(e,T7,(uintptr_t)&h1_cpu_apu_deadline);
    imm(e,43,T0,T7,0);
}
static void native_prologue(emitter *e, uint16_t pc)
{
    imm(e,9,SP,SP,-64);
    imm(e,43,RA,SP,60);imm(e,43,S0,SP,56);imm(e,43,S1,SP,52);
    imm(e,43,S2,SP,48);imm(e,43,S3,SP,44);
    imm(e,43,S4,SP,40);imm(e,43,S5,SP,36);
    imm(e,43,S6,SP,32);imm(e,43,S7,SP,28);
    mov(e,S0,A0);mov(e,S1,A1);imm(e,13,S2,ZERO,pc);mov(e,S3,ZERO);
    /* Pin once for this shared O32 frame, including public direct calls.
     * Native links skip the entire prologue; recursive CPU calls pin again. */
    load_global(e,T0,&h1_jit_native_depth);imm(e,9,T0,T0,1);imm(e,43,T0,T7,0);
}
static int is_immediate(uint8_t op);
static unsigned op_length(unsigned mode)
{ return mode==1?1:mode>=4?3:2; }
static uint16_t absolute_address(uint16_t pc)
{ return Page[pc>>11][pc+1]|(Page[pc>>11][pc+2]<<8); }
static int pure_read(readfunc handler)
{
    return handler==CartBR || handler==h1_cpu_jit_namco_wram ||
           handler==h1_cpu_jit_raml || handler==h1_cpu_jit_ramh;
}
static int pure_ram_write(uint16_t address)
{
    return address<0x2000 && BWrite[address]==(address<0x800?
           h1_cpu_jit_ram_write_l:h1_cpu_jit_ram_write_h);
}
/* The original no-hook register handlers change only PPU state (B2000 may
 * also raise NMI). $2007 writes through mutable pointers and stays opaque. */
static int pure_ppu_write(uint16_t address)
{
    return address>=0x2000 && address<0x4000 && (address&7)<7 &&
           BWrite[address]==h1_ppu_code_safe_writes[address&7] &&
           !PPU_hook && !InputScanlineHook;
}
static int terminal_io(uint16_t pc, unsigned mode)
{ return (mode==6 || mode==8) && !pure_ram_write(absolute_address(pc)); }
static void cpu_load(emitter *e, unsigned dest, unsigned offset)
{
    if(e->fast && (offset==6 || offset==10))mov(e,dest,offset==6?S4:S7);
    else imm(e,36,dest,S0,offset);
}
static void cpu_store(emitter *e, unsigned source, unsigned offset)
{
    if(e->fast && (offset==6 || offset==10))mov(e,offset==6?S4:S7,source);
    else imm(e,40,source,S0,offset);
}
static void db_store(emitter *e, unsigned source)
{
    /* Complete pure runs have no observable intermediate bus state. The
     * prefix path still materializes DB after every guest instruction. */
    if(!e->run_full || e->run_remaining==1)imm(e,40,source,S0,24);
}
static void spill_cpu(emitter *e)
{ if(e->fast) { imm(e,40,S4,S0,6);imm(e,40,S7,S0,10); } }
static void reload_cpu(emitter *e)
{ if(e->fast) { imm(e,36,S4,S0,6);imm(e,36,S7,S0,10); } }
static void irq_guard(emitter *e)
{
    imm(e,35,T0,S0,20);unsigned none=branch(e,4,T0,ZERO);
    imm(e,12,T1,T0,FCEU_IQRESET|FCEU_IQNMI2|FCEU_IQNMI);exit_if(e,5,T1,ZERO);
    /* mooPI is the previous instruction's P: CLI must execute one more
     * instruction before a masked IRQ becomes serviceable. */
    imm(e,36,T1,S0,11);imm(e,12,T1,T1,I_FLAG);exit_if(e,4,T1,ZERO);
    constant(e,T1,~(uint32_t)FCEU_IQTEMP);reg(e,36,T0,T0,T1);imm(e,43,T0,S0,20);
    patch(e,none,e->n);
}

static void guard(emitter *e, uint16_t pc, uint8_t op)
{
    imm(e,35,T0,S0,16);exit_if(e,6,T0,ZERO); /* BLEZ */
    irq_guard(e);
    if (e->fast)return;
    imm(e,36,T0,S0,12);exit_if(e,5,T0,ZERO);
    constant(e,T1,(uintptr_t)&encryptOpcodes);imm(e,36,T0,T1,0);stale_if(e,5,T0,ZERO);
    constant(e,T1,(uintptr_t)&PRGIsRAM[pc>>11]);imm(e,36,T0,T1,0);stale_if(e,5,T0,ZERO);
    imm(e,35,T0,S5,0);constant(e,T1,e->b->page);stale_if(e,5,T0,T1);
    constant(e,T1,(uintptr_t)&ARead[pc]);imm(e,35,T0,T1,0);stale_if(e,5,T0,S7);
    imm(e,36,T0,S4,(int)pc-32768);imm(e,13,T1,ZERO,op);stale_if(e,5,T0,T1);
}
static void stable_entry_guard(emitter *e, uint16_t pc, uint8_t *ops,
                                uint16_t *pcs, unsigned n)
{
    /* This tier is limited to the pinned NROM and Namco 163 contracts:
     * pure PRG/DMC reads; mapper writes can only terminate the block; the
     * known IRQ callback cannot remap/modify code. All instruction bytes
     * are checked on entry, before any possible terminal IO write. */
    load_global(e,T0,&MapIRQHook);constant(e,T1,known_irq);stale_if(e,5,T0,T1);
    load_global(e,T0,&PPU_hook);stale_if(e,5,T0,ZERO);
    load_global(e,T0,&overclocked);imm(e,13,T1,ZERO,e->overclock);stale_if(e,5,T0,T1);
    /* A native chain may reach the previous bank's L1 entry before the C
     * dispatcher sees this PC. Return without invalidating that version so
     * the cold dispatcher can move it into L2 and bind the current bank. */
    imm(e,35,T0,S5,0);constant(e,T1,e->b->page);
    e->page_miss=branch(e,5,T0,T1);
    constant(e,T1,(uintptr_t)&PRGIsRAM[pc>>11]);imm(e,36,T0,T1,0);stale_if(e,5,T0,ZERO);
    constant(e,T5,(uintptr_t)&ARead[pc]);constant(e,T4,(uintptr_t)CartBR);
    for(unsigned i=0;i<n;++i) {
        imm(e,35,T0,T5,(pcs[i]-pc)*4);stale_if(e,5,T0,T4);
        if (e->modes[i]==4) {
            uint16_t address=Page[pc>>11][pcs[i]+1]|(Page[pc>>11][pcs[i]+2]<<8);
            readfunc handler=ARead[address];
            constant(e,T1,(uintptr_t)&ARead[address]);imm(e,35,T0,T1,0);
            constant(e,T1,(uintptr_t)handler);stale_if(e,5,T0,T1);
            if(handler==known_status_read && address==0x2002) {
                load_global(e,T0,&InputScanlineHook);stale_if(e,5,T0,ZERO);
            }
        }
        if(e->modes[i]==6 || e->modes[i]==8) {
            uint16_t address=absolute_address(pcs[i]);
            constant(e,T1,(uintptr_t)&BWrite[address]);imm(e,35,T0,T1,0);
            constant(e,T1,(uintptr_t)BWrite[address]);stale_if(e,5,T0,T1);
            if(pure_ppu_write(address)) {
                /* Private entries inherit the proved no-hook scope;
                 * strict entries establish the input-hook constraint. */
                load_global(e,T0,&InputScanlineHook);stale_if(e,5,T0,ZERO);
            }
        }
        if(e->modes[i]==8 || e->modes[i]==9) {
            uint16_t address=absolute_address(pcs[i]);
            unsigned reads=e->modes[i]==9?2:1;
            for(unsigned j=0;j<reads;++j) {
                uint16_t a=j?((address&0xff00)|((address+1)&255)):address;
                constant(e,T1,(uintptr_t)&ARead[a]);imm(e,35,T0,T1,0);
                constant(e,T1,(uintptr_t)ARead[a]);stale_if(e,5,T0,T1);
            }
        }
    }
    (void)ops;
    /* Exact byte validation, packed into naturally aligned reads. This also
     * detects test-owned in-place changes without a checksum collision. */
    unsigned end=pcs[n-1]+op_length(e->modes[n-1]);
    for (unsigned address=pc;address<end;) {
        uintptr_t physical=e->b->page+address;
        unsigned width=!(physical&3) && end-address>=4?4:
                       !(physical&1) && end-address>=2?2:1;
        uint32_t value=0;
        for(unsigned j=0;j<width;++j)value|=(uint32_t)Page[pc>>11][address+j]<<(j*8);
        imm(e,width==4?35:width==2?37:36,T0,S4,(int)address-32768);
        if(width==4)constant(e,T1,value);else imm(e,13,T1,ZERO,value);
        stale_if(e,5,T0,T1);address+=width;
    }
}

/* Common no-event path: commit the same counters at the same instruction
 * boundary. Do not modify them before calling the original event handler. */
static void sound_clock(emitter *e, uint16_t pc, uint8_t op)
{
    _Static_assert(offsetof(h1_jit_apu_clock,dma)==12, "APU clock ABI");
    constant(e,T5,(uintptr_t)&h1_apu_clock);imm(e,35,T0,T5,0);
    shift(e,0,T1,S5,4);shift(e,0,T2,S5,5);reg(e,33,T1,T1,T2);
    reg(e,35,T2,T0,T1);unsigned frame_event=branch(e,6,T2,ZERO);
    imm(e,35,T0,T5,4);reg(e,35,T3,T0,S5);
    unsigned dmc_event=branch(e,6,T3,ZERO);
    imm(e,35,T0,T5,8);unsigned no_dma=branch(e,4,T0,ZERO);
    imm(e,36,T0,T5,12);
    unsigned dma_event=branch(e,4,T0,ZERO);patch(e,no_dma,e->n);
    imm(e,43,T2,T5,0);imm(e,43,T3,T5,4);
    unsigned done=branch(e,4,ZERO,ZERO);
    patch(e,frame_event,e->n);patch(e,dmc_event,e->n);patch(e,dma_event,e->n);
    spill_cpu(e);
    emit(e,(3u<<26)|(((uintptr_t)FCEU_SoundCPUHook>>2)&0x03ffffff));mov(e,A0,S5);
    /* Ordinary frame/bit events remain in the block. Only a newly pending
     * byte fetch requires leaving before the next guest instruction. */
    constant(e,T1,(uintptr_t)&h1_apu_clock.size);imm(e,35,T0,T1,0);
    unsigned no_pending=branch(e,4,T0,ZERO);
    imm(e,36,T0,T1,4);unsigned have_dma=branch(e,5,T0,ZERO);
    /* A bit event can make DMA pending for the following instruction. Finish
     * this already-clocked opcode through the original semantic body, then
     * return to dispatch. Thus no inline back edge bypasses DMA admission,
     * and no uncommon callback can leave baked operands on its continuation. */
    imm(e,13,A0,ZERO,pc);imm(e,41,A0,S0,4);imm(e,9,S3,S3,1);
    imm(e,13,A1,ZERO,op);call(e,(uintptr_t)h1_cpu_jit_complete);mov(e,S2,V0);
    reload_cpu(e);
    exit_if(e,4,ZERO,ZERO);
    patch(e,no_pending,e->n);patch(e,have_dma,e->n);reload_cpu(e);
    patch(e,done,e->n);
}
static void namco_clock(emitter *e)
{
    constant(e,T1,(uintptr_t)&h1_namco_irq_enabled);imm(e,36,T0,T1,0);
    unsigned disabled=branch(e,4,T0,ZERO);
    constant(e,T1,(uintptr_t)&h1_namco_irq_count);imm(e,37,T0,T1,0);
    reg(e,33,T0,T0,S5);imm(e,12,T0,T0,65535);imm(e,11,T2,T0,32767);
    unsigned event=branch(e,4,T2,ZERO);imm(e,41,T0,T1,0);
    unsigned done=branch(e,4,ZERO,ZERO);patch(e,event,e->n);
    spill_cpu(e);
    emit(e,(3u<<26)|((known_irq>>2)&0x03ffffff));mov(e,A0,S5);
    reload_cpu(e);
    patch(e,done,e->n);patch(e,disabled,e->n);
}
static unsigned cycles(uint8_t op, unsigned mode)
{
    if (op==0x48)return 3;
    if (op==0x68)return 4;
    if (op==0x60 || op==0x20)return 6;
    if (mode==4)return 4;
    if (mode==5)return 3;
    if (mode==6 || mode==7)return 4;
    if (mode==8)return 6;
    if (mode==9)return 5;
    if (mode!=2 || is_immediate(op))return 2;
    switch(op) {
    case 0xe6:case 0xc6:return 5;case 0xf6:case 0xd6:return 6;
    case 0xb5:case 0xb6:case 0xb4:case 0x95:case 0x96:case 0x94:
    case 0x35:case 0x55:case 0x15:case 0xd5:case 0x75:case 0xf5:return 4;
    default:return 3;
    }
}
static void tick(emitter *e, uint16_t pc, uint8_t op, unsigned mode)
{
    unsigned c=cycles(op,mode);
    cpu_load(e,T0,10);imm(e,40,T0,S0,11);
    imm(e,13,T0,ZERO,op);db_store(e,T0);
    unsigned temp=e->fast?S5:A0;
    imm(e,35,temp,S0,0);imm(e,9,temp,temp,c);imm(e,43,ZERO,S0,0);
    imm(e,35,T0,S0,16);imm(e,9,T0,T0,-(int)c*48);imm(e,43,T0,S0,16);
    imm(e,35,T0,S6,0);imm(e,9,T0,T0,c);imm(e,43,T0,S6,0);
    if (e->fast) {
        if (!e->overclock) {
            intptr_t gap=(intptr_t)(uintptr_t)&sound_timestamp-(intptr_t)(uintptr_t)&timestamp;
            if (gap>=-32768 && gap<=32767) {
                imm(e,35,T0,S6,gap);imm(e,9,T0,T0,c);imm(e,43,T0,S6,gap);
            } else {
                constant(e,T1,(uintptr_t)&sound_timestamp);imm(e,35,T0,T1,0);
                imm(e,9,T0,T0,c);imm(e,43,T0,T1,0);
            }
        }
        if (known_irq) namco_clock(e);
        if (!e->overclock) sound_clock(e,pc,op);
        imm(e,13,T0,ZERO,pc);imm(e,41,T0,S0,4);imm(e,9,S3,S3,1);return;
    }
    load_global(e,T0,&overclocked);unsigned skip=branch(e,5,T0,ZERO);
    constant(e,T1,(uintptr_t)&sound_timestamp);imm(e,35,T0,T1,0);
    imm(e,9,T0,T0,c);imm(e,43,T0,T1,0);patch(e,skip,e->n);
    imm(e,43,A0,SP,16);
    load_global(e,T9,&MapIRQHook);skip=branch(e,4,T9,ZERO);
    unknown_contract(e);
    reg(e,9,RA,T9,ZERO);emit(e,0); /* JALR */
    unknown_contract(e);patch(e,skip,e->n);
    load_global(e,T0,&overclocked);skip=branch(e,5,T0,ZERO);
    imm(e,35,A0,SP,16);call(e,(uintptr_t)FCEU_SoundCPUHook);patch(e,skip,e->n);
    imm(e,13,T0,ZERO,pc);imm(e,41,T0,S0,4); /* SH X.PC, after hooks */
    imm(e,9,S3,S3,1);
}
static void operand(emitter *e, uint16_t pc)
{
    if (e->fast) {
        imm(e,13,V0,ZERO,Page[e->b->pc>>11][pc]);db_store(e,V0);return;
    }
    /* Current mapping, not a baked operand: hooks can switch the page. */
    constant(e,T1,(uintptr_t)&ARead[pc]);imm(e,35,T0,T1,0);
    unsigned slow=branch(e,5,T0,S7);
    imm(e,35,T0,S5,0);imm(e,13,T1,ZERO,pc);reg(e,33,T0,T0,T1);
    imm(e,36,V0,T0,0);db_store(e,V0);
    unsigned end=branch(e,4,ZERO,ZERO);
    patch(e,slow,e->n);imm(e,13,A0,ZERO,pc);call(e,(uintptr_t)h1_cpu_jit_read);
    patch(e,end,e->n);
}

/* T0 is always an 8-bit result. Preserve the remaining status flags. */
static void nz(emitter *e)
{
    /* P is already resident: update it directly instead of shuttling the
     * same complete flag byte through a temporary twice. Every instruction
     * still materializes N/Z, including a possible CPU-budget prefix exit. */
    if(e->fast) {
        imm(e,12,S7,S7,0x7d);imm(e,12,T3,T0,0x80);reg(e,37,S7,S7,T3);
        imm(e,11,T3,T0,1);shift(e,0,T3,T3,1);reg(e,37,S7,S7,T3);return;
    }
    cpu_load(e,T2,10); imm(e,12,T2,T2,0x7d);
    imm(e,12,T3,T0,0x80); reg(e,37,T2,T2,T3);
    imm(e,11,T3,T0,1); shift(e,0,T3,T3,1);
    reg(e,37,T2,T2,T3); cpu_store(e,T2,10);
}
static unsigned register_offset(uint8_t op)
{
    switch(op) {
    case 0xae:case 0xbe:case 0x8e:
    case 0xa2:case 0xa6:case 0xb6:case 0x86:case 0x96:case 0xe0:case 0xe4:return 7;
    case 0xac:case 0xbc:case 0x8c:
    case 0xa0:case 0xa4:case 0xb4:case 0x84:case 0x94:case 0xc0:case 0xc4:return 8;
    default:return 6;
    }
}
static void bit(emitter *e)
{
    if(e->fast) {
        imm(e,12,S7,S7,0x3d);reg(e,36,T1,S4,T0);
        imm(e,11,T1,T1,1);shift(e,0,T1,T1,1);reg(e,37,S7,S7,T1);
        imm(e,12,T1,T0,0xc0);reg(e,37,S7,S7,T1);return;
    }
    cpu_load(e,T2,10);imm(e,12,T2,T2,0x3d);
    cpu_load(e,T1,6);reg(e,36,T1,T1,T0);
    imm(e,11,T1,T1,1);shift(e,0,T1,T1,1);reg(e,37,T2,T2,T1);
    imm(e,12,T1,T0,0xc0);reg(e,37,T2,T2,T1);cpu_store(e,T2,10);
}
static void branch_constant_cycles(emitter *e, unsigned c)
{
    imm(e,35,T0,S0,0);imm(e,9,T0,T0,c);imm(e,43,T0,S0,0);
    imm(e,35,T0,S0,16);imm(e,9,T0,T0,-(int)c*48);imm(e,43,T0,S0,16);
    imm(e,35,T0,S6,0);imm(e,9,T0,T0,c);imm(e,43,T0,S6,0);
    if (!e->overclock) {
        intptr_t gap=(intptr_t)(uintptr_t)&sound_timestamp-(intptr_t)(uintptr_t)&timestamp;
        if (gap>=-32768 && gap<=32767) {
            imm(e,35,T0,S6,gap);imm(e,9,T0,T0,c);imm(e,43,T0,S6,gap);
        } else {
            constant(e,T1,(uintptr_t)&sound_timestamp);imm(e,35,T0,T1,0);
            imm(e,9,T0,T0,c);imm(e,43,T0,T1,0);
        }
    }
}
static int is_immediate(uint8_t op)
{
    return op==0xa9 || op==0xa2 || op==0xa0 || op==0x29 || op==0x49 ||
           op==0x09 || op==0xc9 || op==0xe0 || op==0xc0 || op==0x69 || op==0xe9;
}
static void extra_branch_cycles(emitter *e)
{
    /* T0=sign-extended displacement; S2=PC after displacement byte. */
    reg(e,33,T1,S2,T0); imm(e,12,T1,T1,65535);
    reg(e,38,T2,S2,T1); imm(e,12,T2,T2,0x100); shift(e,2,T2,T2,8);
    imm(e,9,T2,T2,1); mov(e,S2,T1);
    imm(e,35,T3,S0,0); reg(e,33,T3,T3,T2); imm(e,43,T3,S0,0);
    shift(e,0,T3,T2,4); shift(e,0,T4,T2,5); reg(e,33,T3,T3,T4);
    imm(e,35,T4,S0,16); reg(e,35,T4,T4,T3); imm(e,43,T4,S0,16);
    imm(e,35,T4,S6,0);reg(e,33,T4,T4,T2);imm(e,43,T4,S6,0);
    if (e->fast) {
        if (!e->overclock) {
            intptr_t gap=(intptr_t)(uintptr_t)&sound_timestamp-(intptr_t)(uintptr_t)&timestamp;
            if (gap>=-32768 && gap<=32767) {
                imm(e,35,T4,S6,gap);reg(e,33,T4,T4,T2);imm(e,43,T4,S6,gap);
            } else {
                constant(e,T3,(uintptr_t)&sound_timestamp);imm(e,35,T4,T3,0);
                reg(e,33,T4,T4,T2);imm(e,43,T4,T3,0);
            }
        }
        return;
    }
    constant(e,T3,(uint32_t)&overclocked); imm(e,35,T4,T3,0);
    unsigned skip=branch(e,5,T4,ZERO);
    constant(e,T3,(uint32_t)&sound_timestamp); imm(e,35,T4,T3,0);
    reg(e,33,T4,T4,T2); imm(e,43,T4,T3,0);
    patch(e,skip,e->n);
}
/* New addressing families share the original bus callbacks on the generic
 * verification tier. Production only carries state across proven pure RAM /
 * PRG reads and RAM writes; an IO write is the final instruction of a block. */
static void absolute_operand(emitter *e, uint16_t pc)
{
    if(e->fast) {
        uint16_t address=absolute_address(pc);
        imm(e,13,A0,ZERO,address);imm(e,13,T0,ZERO,address>>8);
        db_store(e,T0);
    } else {
        operand(e,(uint16_t)(pc+1));imm(e,43,V0,SP,20);
        operand(e,(uint16_t)(pc+2));shift(e,0,A0,V0,8);
        imm(e,35,T0,SP,20);reg(e,37,A0,A0,T0);
    }
}
static void dynamic_read_guard(emitter *e, unsigned address, readfunc handler)
{
    constant(e,T1,(uintptr_t)ARead);shift(e,0,T0,address,2);
    reg(e,33,T1,T1,T0);imm(e,35,T0,T1,0);
    /* This guard can run after earlier native instructions. Use the normal
     * exit, which spills resident A/P; entry-stale exits precede the reload. */
    constant(e,T1,(uintptr_t)handler);exit_if(e,5,T0,T1);
}
/* A0 is a checked address. Pure handlers cannot observe resident A/P. */
static void pure_bus_read(emitter *e, readfunc handler)
{
    if(handler==CartBR) {
        shift(e,2,T0,A0,11);shift(e,0,T0,T0,2);
        constant(e,T1,(uintptr_t)Page);reg(e,33,T0,T0,T1);
        imm(e,35,T0,T0,0);reg(e,33,T0,T0,A0);imm(e,36,V0,T0,0);
    } else if(handler==h1_cpu_jit_raml || handler==h1_cpu_jit_ramh) {
        imm(e,12,T0,A0,0x7ff);reg(e,33,T0,T0,S1);imm(e,36,V0,T0,0);
    } else call(e,(uintptr_t)handler);
    db_store(e,V0);
}
static void generic_bus_read(emitter *e)
{ spill_cpu(e);call(e,(uintptr_t)h1_cpu_jit_read);reload_cpu(e); }
static void generic_bus_write(emitter *e, int preserves_code)
{
    spill_cpu(e);constant(e,T1,(uintptr_t)BWrite);shift(e,0,T0,A0,2);
    reg(e,33,T1,T1,T0);imm(e,35,T9,T1,0);
    if(!preserves_code)unknown_contract(e);
    reg(e,9,RA,T9,ZERO);emit(e,0);
    /* A recursive CPU call can grant a new credential inside the callback;
     * its remaining side effects must not leave that inner credential live. */
    if(!preserves_code)unknown_contract(e);
    reload_cpu(e);
}
static void indexed_guard(emitter *e, uint16_t pc, uint8_t op)
{
    unsigned index=(op==0xb9 || op==0xbe)?8:7;
    uint16_t base=absolute_address(pc);
    imm(e,36,T6,S0,index);imm(e,13,T0,ZERO,base);reg(e,33,T6,T6,T0);
    imm(e,12,T6,T6,65535);imm(e,43,T6,SP,20);
    dynamic_read_guard(e,T6,ARead[base]);
    /* Preserve the pinned core's target^0x100 dummy-read behavior, including
     * carries across xxFF and FFFF. Guard its actual bus address before tick. */
    imm(e,14,T0,T6,base);imm(e,12,T0,T0,0x100);
    unsigned same=branch(e,4,T0,ZERO);
    imm(e,14,T6,T6,0x100);
    dynamic_read_guard(e,T6,ARead[base]);
    patch(e,same,e->n);
}
static void extra_read_cycle(emitter *e)
{
    if(e->fast) { branch_constant_cycles(e,1);return; }
    imm(e,35,T0,S0,0);imm(e,9,T0,T0,1);imm(e,43,T0,S0,0);
    imm(e,35,T0,S0,16);imm(e,9,T0,T0,-48);imm(e,43,T0,S0,16);
    imm(e,35,T0,S6,0);imm(e,9,T0,T0,1);imm(e,43,T0,S6,0);
    load_global(e,T0,&overclocked);unsigned skip=branch(e,5,T0,ZERO);
    constant(e,T1,(uintptr_t)&sound_timestamp);imm(e,35,T0,T1,0);
    imm(e,9,T0,T0,1);imm(e,43,T0,T1,0);patch(e,skip,e->n);
}
/* Stack accesses use the pinned core's direct RAM helpers, including its
 * uint8 S wrap and DB changes only on POP. They have no bus callbacks: JSR
 * still reads low, pushes return high/low, then reads high in that order. */
static void stack_push(emitter *e, unsigned source)
{
    imm(e,36,T4,S0,9);reg(e,33,T5,S1,T4);imm(e,40,source,T5,0x100);
    imm(e,9,T4,T4,-1);imm(e,40,T4,S0,9);
}
static void stack_pop(emitter *e, unsigned dest)
{
    imm(e,36,T4,S0,9);imm(e,9,T4,T4,1);imm(e,12,T4,T4,255);
    imm(e,40,T4,S0,9);reg(e,33,T5,S1,T4);imm(e,36,dest,T5,0x100);
    db_store(e,dest);
}
static void instruction(emitter *e, uint16_t pc, uint8_t op, unsigned mode)
{
    if(e->run_remaining) {
        /* Event-free regions can stop at any CPU budget boundary. T8 holds
         * clock debt and S5 the remaining CPU budget. T9/A3 retain the last
         * executed instruction's previous P / PC until the common commit.
         * Admitted operations cannot call C or inspect these CPU fields. */
        if(!e->run_full) {
            e->run_exits[e->run_exits_n++]=branch(e,6,S5,ZERO);
            imm(e,9,S5,S5,-(int)cycles(op,mode)*48);
            imm(e,9,T8,T8,cycles(op,mode));
        }
        if(!e->run_full || e->run_remaining==1) {
            mov(e,T9,S7);imm(e,13,A3,ZERO,pc);
        }
        /* Every admitted instruction with an operand replaces DB before
         * any observable boundary. Implied instructions retain opcode DB. */
        if(mode==1 && (!e->run_full || e->run_remaining==1)) {
            imm(e,13,T0,ZERO,op);db_store(e,T0);
        }
        if(!e->run_full) {
            imm(e,13,S2,ZERO,(uint16_t)(pc+op_length(mode)));imm(e,9,S3,S3,1);
        }
    } else {
        guard(e,pc,op);
        if(e->fast && mode==7)indexed_guard(e,pc,op);
        tick(e,pc,op,mode);
        imm(e,13,S2,ZERO,(uint16_t)(pc+op_length(mode)));
    }
    if(mode==6) {
        absolute_operand(e,pc);
        cpu_load(e,A1,register_offset(op));
        if(e->fast && pure_ram_write(absolute_address(pc))) {
            imm(e,40,A1,S1,absolute_address(pc)&0x7ff);
        } else generic_bus_write(e,e->fast && pure_ppu_write(absolute_address(pc)));
        return;
    }
    if(mode==7) {
        uint16_t base=absolute_address(pc);
        if(e->fast) {
            imm(e,13,T0,ZERO,base>>8);db_store(e,T0);
            imm(e,35,T6,SP,20);imm(e,14,T0,T6,base);imm(e,12,T0,T0,0x100);
            unsigned same=branch(e,4,T0,ZERO);
            imm(e,14,A0,T6,0x100);pure_bus_read(e,ARead[base]);
            extra_read_cycle(e);patch(e,same,e->n);
            imm(e,35,A0,SP,20);pure_bus_read(e,ARead[base]);
        } else {
            absolute_operand(e,pc);imm(e,43,A0,SP,24);
            imm(e,36,T0,S0,(op==0xb9||op==0xbe)?8:7);
            reg(e,33,T6,A0,T0);imm(e,12,T6,T6,65535);imm(e,43,T6,SP,20);
            reg(e,38,T0,T6,A0);imm(e,12,T0,T0,0x100);
            unsigned same=branch(e,4,T0,ZERO);
            imm(e,14,A0,T6,0x100);generic_bus_read(e);extra_read_cycle(e);
            patch(e,same,e->n);imm(e,35,A0,SP,20);generic_bus_read(e);
        }
        mov(e,T0,V0);cpu_store(e,T0,register_offset(op));nz(e);return;
    }
    if(mode==8) {
        absolute_operand(e,pc);
        if(e->fast) {
            unsigned address=absolute_address(pc)&0x7ff;
            imm(e,36,T0,S1,address);db_store(e,T0);
            imm(e,9,T0,T0,op==0xee?1:-1);imm(e,12,T0,T0,255);
            imm(e,40,T0,S1,address);nz(e);
        } else {
            imm(e,43,A0,SP,24);generic_bus_read(e);imm(e,43,V0,SP,20);
            imm(e,35,A0,SP,24);mov(e,A1,V0);generic_bus_write(e,0);
            imm(e,35,T0,SP,20);imm(e,9,T0,T0,op==0xee?1:-1);
            imm(e,12,T0,T0,255);imm(e,43,T0,SP,20);nz(e);
            imm(e,35,A0,SP,24);imm(e,35,A1,SP,20);generic_bus_write(e,0);
        }
        return;
    }
    if(mode==9) {
        uint16_t address=absolute_address(pc);
        absolute_operand(e,pc);
        if(e->fast) {
            pure_bus_read(e,ARead[address]);imm(e,43,V0,SP,20);
            uint16_t high=(address&0xff00)|((address+1)&255);
            imm(e,13,A0,ZERO,high);pure_bus_read(e,ARead[high]);
        } else {
            imm(e,43,A0,SP,24);generic_bus_read(e);imm(e,43,V0,SP,20);
            imm(e,35,T0,SP,24);imm(e,9,T1,T0,1);imm(e,12,T1,T1,255);
            imm(e,12,A0,T0,0xff00);reg(e,37,A0,A0,T1);generic_bus_read(e);
        }
        shift(e,0,S2,V0,8);imm(e,35,T0,SP,20);reg(e,37,S2,S2,T0);return;
    }
    if(mode==5) {
        uint16_t target=Page[pc>>11][pc+1]|(Page[pc>>11][pc+2]<<8);
        if(op==0x20) {
            operand(e,(uint16_t)(pc+1));imm(e,43,V0,SP,20);
            imm(e,13,T0,ZERO,(uint16_t)(pc+2)>>8);stack_push(e,T0);
            imm(e,13,T0,ZERO,(uint16_t)(pc+2)&255);stack_push(e,T0);
            operand(e,(uint16_t)(pc+2));shift(e,0,S2,V0,8);
            imm(e,35,T0,SP,20);reg(e,37,S2,S2,T0);
        } else
        if(e->fast) {
            imm(e,13,T0,ZERO,target>>8);db_store(e,T0);
            imm(e,13,S2,ZERO,target);
        } else {
            operand(e,(uint16_t)(pc+1));imm(e,43,V0,SP,20);
            operand(e,(uint16_t)(pc+2));shift(e,0,S2,V0,8);
            imm(e,35,T0,SP,20);reg(e,37,S2,S2,T0);
        }
        /* A batched control tail must commit its clock debt before taking
         * any back edge. Its normal linker will perform the next admission. */
        for(unsigned i=0;!e->run_remaining && i<e->emitted;++i)if(e->pcs[i]==target) {
            imm(e,11,T0,S3,CHAIN_LIMIT);exit_if(e,4,T0,ZERO);
            unsigned back;
            if(e->fast)back=branch(e,4,ZERO,ZERO);
            else { imm(e,13,T0,ZERO,target);back=branch(e,4,S2,T0); }
            patch(e,back,e->labels[i]);break;
        }
        return;
    }
    if (mode==3) {
        unsigned mask=op<0x40?0x80:op<0x80?0x40:op<0xc0?1:2;
        if(e->run_remaining) {
            /* Only fixed base clocks join the region. Taken extra cycles
             * remain pending tcount after commit, as in the opcode body. */
            imm(e,13,T0,ZERO,op);db_store(e,T0);
            imm(e,13,S2,ZERO,(uint16_t)(pc+2));
            imm(e,12,T0,S7,mask);
            unsigned not_taken=branch(e,(op&0x20)?4:5,T0,ZERO);
            operand(e,(uint16_t)(pc+1));
            uint16_t target=(uint16_t)(pc+2+(int8_t)Page[pc>>11][(uint16_t)(pc+1)]);
            imm(e,13,S2,ZERO,target);
            imm(e,13,T0,ZERO,1);imm(e,43,T0,SP,20);
            patch(e,not_taken,e->n);return;
        }
        if(e->fast)imm(e,12,T0,S7,mask);
        else { cpu_load(e,T0,10);imm(e,12,T0,T0,mask); }
        e->ends[e->ends_n++]=branch(e,(op&0x20)?4:5,T0,ZERO);
        operand(e,(uint16_t)(pc+1));
        uint16_t target=(uint16_t)(pc+2+(int8_t)Page[pc>>11][(uint16_t)(pc+1)]);
        if (e->fast) {
            branch_constant_cycles(e,1+(((uint16_t)(pc+2)^target)&0x100?1:0));
            imm(e,13,S2,ZERO,target);
        } else {
            shift(e,0,T0,V0,24);shift(e,3,T0,T0,24);extra_branch_cycles(e);
        }
        for (unsigned i=0;i<e->emitted;++i) if(e->pcs[i]==target) {
            imm(e,11,T0,S3,CHAIN_LIMIT);exit_if(e,4,T0,ZERO);
            unsigned back;
            if (e->fast)back=branch(e,4,ZERO,ZERO);
            else { imm(e,13,T0,ZERO,target);back=branch(e,4,S2,T0); }
            patch(e,back,e->labels[i]);break;
        }
        return;
    }
    if (mode==4) {
        if (e->fast) {
            uint16_t address=Page[pc>>11][pc+1]|(Page[pc>>11][pc+2]<<8);
            readfunc handler=ARead[address];
            imm(e,13,A0,ZERO,address);imm(e,13,T0,ZERO,address>>8);
            if(pure_read(handler)) {
                db_store(e,T0);pure_bus_read(e,handler);
            } else {
                /* Entry excludes PPU/input callbacks. Pending DMA returned to
                 * the interpreter before tick, so no opaque callback can have
                 * installed one between that validation and this pure read. */
                emit(e,(3u<<26)|(((uintptr_t)handler>>2)&0x03ffffff));db_store(e,T0);
                db_store(e,V0);
            }
        } else {
            operand(e,(uint16_t)(pc+1));imm(e,43,V0,SP,20);
            operand(e,(uint16_t)(pc+2));shift(e,0,A0,V0,8);
            imm(e,35,T0,SP,20);reg(e,37,A0,A0,T0);call(e,(uintptr_t)h1_cpu_jit_read);
        }
        mov(e,T0,V0);
        if(op==0x2c)bit(e);
        else { cpu_store(e,T0,register_offset(op));nz(e); }
        return;
    }
    if (mode==1) {
        if(op==0x48) { cpu_load(e,T0,6);stack_push(e,T0);return; }
        if(op==0x68) { stack_pop(e,T0);cpu_store(e,T0,6);nz(e);return; }
        if(op==0x60) {
            stack_pop(e,T0);imm(e,43,T0,SP,20);stack_pop(e,T0);
            shift(e,0,S2,T0,8);imm(e,35,T0,SP,20);reg(e,37,S2,S2,T0);
            imm(e,9,S2,S2,1);imm(e,12,S2,S2,65535);return;
        }
        if(op==0x0a || op==0x4a || op==0x2a || op==0x6a) {
            cpu_load(e,T0,6);cpu_load(e,T2,10);
            if(op==0x0a || op==0x2a) {
                shift(e,2,T4,T0,7);shift(e,0,T0,T0,1);
                if(op==0x2a) { imm(e,12,T1,T2,1);reg(e,37,T0,T0,T1); }
                imm(e,12,T0,T0,255);
            } else {
                imm(e,12,T4,T0,1);shift(e,2,T0,T0,1);
                if(op==0x6a) { imm(e,12,T1,T2,1);shift(e,0,T1,T1,7);reg(e,37,T0,T0,T1); }
            }
            imm(e,12,T2,T2,254);reg(e,37,T2,T2,T4);cpu_store(e,T2,10);
            cpu_store(e,T0,6);nz(e);return;
        }
        unsigned dest=0,source=0;
        int delta=0;
        switch(op) {
        case 0xaa:dest=7;source=6;break; case 0xa8:dest=8;source=6;break;
        case 0x8a:dest=6;source=7;break; case 0x98:dest=6;source=8;break;
        case 0xba:dest=7;source=9;break; case 0x9a:dest=9;source=7;break;
        case 0xe8:dest=source=7;delta=1;break;case 0xc8:dest=source=8;delta=1;break;
        case 0xca:dest=source=7;delta=-1;break;case 0x88:dest=source=8;delta=-1;break;
        }
        if (dest) {
            cpu_load(e,T0,source);
            if (delta) { imm(e,9,T0,T0,delta); imm(e,12,T0,T0,255); }
            cpu_store(e,T0,dest); if (op!=0x9a) nz(e);
        } else if (op!=0xea) {
            unsigned mask=(op==0x18||op==0x38)?1:(op==0x58||op==0x78)?4:
                          (op==0xb8)?0x40:8;
            unsigned flags=e->fast?S7:T0;
            if(!e->fast)cpu_load(e,T0,10);
            if (op==0x38||op==0x78||op==0xf8) imm(e,13,flags,flags,mask);
            else imm(e,12,flags,flags,255^mask);
            if(!e->fast)cpu_store(e,T0,10);
        }
        return;
    }
    operand(e,(uint16_t)(pc+1));mov(e,T0,V0);
    int immediate=is_immediate(op);
    int store=op==0x85||op==0x86||op==0x84||op==0x95||op==0x96||op==0x94;
    if (!immediate) {
        unsigned index=(op==0xb6||op==0x96)?8:
                       (op==0xb5||op==0xb4||op==0x95||op==0x94||
                        op==0x35||op==0x55||op==0x15||op==0xd5||
                        op==0xf6||op==0xd6||op==0x75||op==0xf5)?7:0;
        if (index) {
            imm(e,36,T1,S0,index); reg(e,33,T0,T0,T1); imm(e,12,T0,T0,255);
        }
        reg(e,33,T6,S1,T0);
        if (store) {
            cpu_load(e,T0,register_offset(op)); imm(e,40,T0,T6,0); return;
        }
        imm(e,36,T0,T6,0); db_store(e,T0);
    }
    switch(op) {
    case 0x69:case 0x65:case 0x75:case 0xe9:case 0xe5:case 0xf5: {
        int subtract=op==0xe9||op==0xe5||op==0xf5;
        cpu_load(e,T1,6);cpu_load(e,T2,10);imm(e,12,T3,T2,1);
        if(subtract) {
            imm(e,14,T3,T3,1);reg(e,35,T4,T1,T0);reg(e,35,T4,T4,T3);
        } else { reg(e,33,T4,T1,T0);reg(e,33,T4,T4,T3); }
        reg(e,38,T3,T1,T0);
        if(!subtract)imm(e,14,T3,T3,0x80);
        reg(e,38,T5,T1,T4);reg(e,36,T3,T3,T5);imm(e,12,T3,T3,0x80);
        shift(e,2,T3,T3,1);imm(e,12,T2,T2,0x3e);reg(e,37,T2,T2,T3);
        shift(e,2,T3,T4,8);imm(e,12,T3,T3,1);
        if(subtract)imm(e,14,T3,T3,1);
        reg(e,37,T2,T2,T3);cpu_store(e,T2,10);
        imm(e,12,T0,T4,255);cpu_store(e,T0,6);nz(e);break;
    }
    case 0x24:bit(e);break;
    case 0xa9:case 0xa2:case 0xa0:case 0xa5:case 0xa6:case 0xa4:
    case 0xb5:case 0xb6:case 0xb4:
        cpu_store(e,T0,register_offset(op)); nz(e); break;
    case 0x29:case 0x25:case 0x35:case 0x49:case 0x45:case 0x55:
    case 0x09:case 0x05:case 0x15:
        cpu_load(e,T1,6);
        reg(e,(op==0x29||op==0x25||op==0x35)?36:
              (op==0x49||op==0x45||op==0x55)?38:37,T0,T0,T1);
        cpu_store(e,T0,6); nz(e); break;
    case 0xc9:case 0xc5:case 0xd5:case 0xe0:case 0xe4:case 0xc0:case 0xc4:
        cpu_load(e,T1,register_offset(op)); reg(e,43,T4,T1,T0);
        imm(e,14,T4,T4,1); reg(e,35,T0,T1,T0); imm(e,12,T0,T0,255);
        nz(e); cpu_load(e,T2,10); imm(e,12,T2,T2,254);
        reg(e,37,T2,T2,T4); cpu_store(e,T2,10); break;
    case 0xe6:case 0xf6:case 0xc6:case 0xd6:
        imm(e,9,T0,T0,(op==0xe6||op==0xf6)?1:-1);
        imm(e,12,T0,T0,255); imm(e,40,T0,T6,0); nz(e); break;
    }
}

/* Minimal decoded metadata for a scheduling region, separate from machine
 * emission. Keep variable-cycle instructions and observable bus operations
 * outside these regions; they retain the original instruction scheduler. */
typedef struct {
    uint16_t pc;
    uint8_t opcode, mode, cycles, pure;
} decoded_op;
static int run_pure(uint16_t pc, unsigned mode)
{
    if(mode==1 || mode==2 || mode==3)return 1;
    if(mode==5)return 1; /* JMP/JSR have only proven ROM and direct stack RAM. */
    if(mode==4) {
        readfunc handler=ARead[absolute_address(pc)];
        return handler==CartBR || handler==h1_cpu_jit_raml || handler==h1_cpu_jit_ramh;
    }
    if(mode==6)return pure_ram_write(absolute_address(pc));
    if(mode==8)return 1; /* e.fast already proves both RAM handlers. */
    return 0;
}
static void run_guard(emitter *e, unsigned total)
{
    /* The complete run may use one CPU-budget admission; a short CPU prefix
     * retains per-instruction checks. This event admission proves
     * only the absence of hardware events for the region's maximum length. */
    imm(e,35,T0,S0,16);exit_if(e,6,T0,ZERO);
    imm(e,35,T0,S0,20);exit_if(e,5,T0,ZERO);
    /* Limit carry before unsigned addition. Real carry is a few cycles; a
     * corrupt or extreme diagnostic value simply takes the old scheduler. */
    imm(e,35,S5,S0,0);imm(e,11,T0,S5,256);exit_if(e,4,T0,ZERO);
    imm(e,9,S5,S5,total);
    if(!e->overclock) {
        constant(e,T5,(uintptr_t)&h1_apu_clock);
        shift(e,0,T1,S5,4);shift(e,0,T2,S5,5);reg(e,33,T1,T1,T2);
        imm(e,35,T0,T5,0);reg(e,42,T0,T1,T0);exit_if(e,4,T0,ZERO);
        imm(e,35,T0,T5,4);reg(e,42,T0,S5,T0);exit_if(e,4,T0,ZERO);
        imm(e,35,T0,T5,8);unsigned no_dma=branch(e,4,T0,ZERO);
        imm(e,36,T0,T5,12);exit_if(e,4,T0,ZERO);patch(e,no_dma,e->n);
    }
    if(known_irq) {
        constant(e,T1,(uintptr_t)&h1_namco_irq_enabled);imm(e,36,T0,T1,0);
        unsigned disabled=branch(e,4,T0,ZERO);
        constant(e,T1,(uintptr_t)&h1_namco_irq_count);imm(e,37,T0,T1,0);
        reg(e,33,T0,T0,S5);imm(e,11,T0,T0,32767);exit_if(e,4,T0,ZERO);
        patch(e,disabled,e->n);
    }
}
static void min_event_budget(emitter *e)
{
    /* A2 is a scaled-cycle horizon; T0 is another signed horizon. */
    reg(e,42,T1,T0,A2);unsigned keep=branch(e,4,T1,ZERO);
    mov(e,A2,T0);patch(e,keep,e->n);
}
static void run_prefix_guard(emitter *e, unsigned total, unsigned maximum)
{
    /* A rejected whole-run event proof need not return after one opcode.
     * Derive a conservative deadline for all pure instructions in the prefix.
     * Its final instruction may overshoot this synthetic CPU budget by at
     * most `maximum`, but cannot reach an APU/DMC/Namco event. A2 retains the
     * budget difference so commit restores the real CPU count exactly. */
    imm(e,35,S5,S0,16);exit_if(e,6,S5,ZERO);
    imm(e,35,T0,S0,20);exit_if(e,5,T0,ZERO);
    imm(e,35,T4,S0,0);imm(e,11,T0,T4,256);exit_if(e,4,T0,ZERO);
    imm(e,9,T6,T4,total);
    shift(e,0,A2,T6,4);shift(e,0,T0,T6,5);reg(e,33,A2,A2,T0);
    if(!e->overclock) {
        constant(e,T5,(uintptr_t)&h1_apu_clock);
        imm(e,35,T0,T5,0);exit_if(e,6,T0,ZERO);
        imm(e,9,T0,T0,-1);min_event_budget(e);
        imm(e,35,T0,T5,4);exit_if(e,6,T0,ZERO);
        reg(e,42,T1,T6,T0);unsigned later=branch(e,5,T1,ZERO);
        imm(e,9,T0,T0,-1);shift(e,0,T1,T0,4);shift(e,0,T0,T0,5);
        reg(e,33,T0,T0,T1);min_event_budget(e);patch(e,later,e->n);
        imm(e,35,T0,T5,8);unsigned no_dma=branch(e,4,T0,ZERO);
        imm(e,36,T0,T5,12);exit_if(e,4,T0,ZERO);patch(e,no_dma,e->n);
    }
    if(known_irq) {
        constant(e,T1,(uintptr_t)&h1_namco_irq_enabled);imm(e,36,T0,T1,0);
        unsigned disabled=branch(e,4,T0,ZERO);
        constant(e,T1,(uintptr_t)&h1_namco_irq_count);imm(e,37,T0,T1,0);
        imm(e,11,T1,T0,32767);exit_if(e,4,T1,ZERO);
        imm(e,14,T0,T0,32767);imm(e,9,T0,T0,-1);
        shift(e,0,T1,T0,4);shift(e,0,T0,T0,5);reg(e,33,T0,T0,T1);
        min_event_budget(e);patch(e,disabled,e->n);
    }
    shift(e,0,T0,T4,4);shift(e,0,T1,T4,5);reg(e,33,T0,T0,T1);
    reg(e,35,A2,A2,T0);imm(e,9,T0,A2,1-(int)maximum*48);
    exit_if(e,6,T0,ZERO);
    reg(e,42,T1,T0,S5);unsigned cpu_first=branch(e,4,T1,ZERO);
    reg(e,35,A2,S5,T0);mov(e,S5,T0);unsigned done=branch(e,4,ZERO,ZERO);
    patch(e,cpu_first,e->n);mov(e,A2,ZERO);patch(e,done,e->n);
}
static void schedule_count(emitter *e, unsigned index)
{
    load_global(e,T0,&h1_jit_schedule_stats[index]);
    imm(e,9,T0,T0,1);imm(e,43,T0,T7,0);
}
static void run_commit(emitter *e)
{
    /* All memory operations in this region are pure or private CPU RAM.
     * No event handler can observe the temporarily uncommitted clocks. */
    imm(e,40,T9,S0,11);imm(e,41,A3,S0,4);
    if(!e->run_full)reg(e,33,S5,S5,A2);
    imm(e,43,S5,S0,16);
    imm(e,35,S5,S0,0);reg(e,33,S5,S5,T8);imm(e,43,ZERO,S0,0);
    imm(e,35,T0,S6,0);reg(e,33,T0,T0,T8);imm(e,43,T0,S6,0);
    if(!e->overclock) {
        intptr_t gap=(intptr_t)(uintptr_t)&sound_timestamp-(intptr_t)(uintptr_t)&timestamp;
        if(gap>=-32768 && gap<=32767) {
            imm(e,35,T0,S6,gap);reg(e,33,T0,T0,T8);imm(e,43,T0,S6,gap);
        } else {
            constant(e,T1,(uintptr_t)&sound_timestamp);imm(e,35,T0,T1,0);
            reg(e,33,T0,T0,T8);imm(e,43,T0,T1,0);
        }
        constant(e,T5,(uintptr_t)&h1_apu_clock);
        shift(e,0,T1,S5,4);shift(e,0,T2,S5,5);reg(e,33,T1,T1,T2);
        imm(e,35,T0,T5,0);reg(e,35,T0,T0,T1);imm(e,43,T0,T5,0);
        imm(e,35,T0,T5,4);reg(e,35,T0,T0,S5);imm(e,43,T0,T5,4);
    }
    if(known_irq) {
        constant(e,T1,(uintptr_t)&h1_namco_irq_enabled);imm(e,36,T0,T1,0);
        unsigned disabled=branch(e,4,T0,ZERO);
        constant(e,T1,(uintptr_t)&h1_namco_irq_count);imm(e,37,T0,T1,0);
        reg(e,33,T0,T0,S5);imm(e,41,T0,T1,0);patch(e,disabled,e->n);
    }
    load_global(e,T0,&h1_jit_run_stats[2]);imm(e,9,T0,T0,1);imm(e,43,T0,T7,0);
}
static void conditional_tail_commit(emitter *e, const decoded_op *tail)
{
    if(tail->mode!=3)return;
    imm(e,35,T0,SP,20);unsigned not_taken=branch(e,4,T0,ZERO);
    uint16_t pc=tail->pc;
    uint16_t target=(uint16_t)(pc+2+(int8_t)Page[pc>>11][(uint16_t)(pc+1)]);
    branch_constant_cycles(e,1+(((uint16_t)(pc+2)^target)&0x100?1:0));
    /* Commit before a back edge. Every target has its own budget/event
     * admission, never an entry halfway into a deferred-clock body. */
    for(unsigned i=0;i<e->emitted;++i)if(e->pcs[i]==target) {
        /* Region admission temporarily reuses e.exits for its fallback
         * fixups. Keep this ordinary end outside that pending guard list. */
        imm(e,11,T0,S3,CHAIN_LIMIT);
        e->ends[e->ends_n++]=branch(e,4,T0,ZERO);
        unsigned back=branch(e,4,ZERO,ZERO);patch(e,back,e->labels[i]);break;
    }
    patch(e,not_taken,e->n);
}
static void emit_region(emitter *e, decoded_op *decoded, unsigned start, unsigned end)
{
    unsigned total=0, maximum=0;
    int control_tail=decoded[end-1].mode==3 || decoded[end-1].mode==5 || decoded[end-1].opcode==0x60;
    for(unsigned i=start;i<end;++i) {
        total+=decoded[i].cycles;
        if(decoded[i].cycles>maximum)maximum=decoded[i].cycles;
    }
    e->labels[start]=e->n;
    unsigned exits_begin=e->exits_n;
    run_guard(e,total);
    /* Admission rejects target the local slow path; ordinary native exits
     * target the final return. Own these fixups separately so later body
     * emission cannot overwrite a still-pending admission branch. */
    unsigned full_rejects[16], full_rejects_n=e->exits_n-exits_begin;
    if(full_rejects_n>16) { e->failed=1;return; }
    memcpy(full_rejects,e->exits+exits_begin,full_rejects_n*sizeof(unsigned));
    e->exits_n=exits_begin;
    imm(e,35,S5,S0,16);
    /* Match FCEUmm's final-instruction overshoot: the CPU budget only needs
     * to remain positive before the last instruction, not after it. */
    imm(e,10,T0,S5,(int)(total-decoded[end-1].cycles)*48+1);
    unsigned short_budget=branch(e,5,T0,ZERO);
    e->run_full=1;
    if(decoded[end-1].mode==3)imm(e,43,ZERO,SP,20);
    for(unsigned i=start;i<end;++i) {
        e->pcs[i]=decoded[i].pc;e->emitted=i+1;e->run_remaining=end-i;
        instruction(e,decoded[i].pc,decoded[i].opcode,decoded[i].mode);
    }
    e->run_remaining=0;
    imm(e,9,S5,S5,-(int)total*48);imm(e,13,T8,ZERO,total);
    if(!control_tail)
        imm(e,13,S2,ZERO,(uint16_t)(decoded[end-1].pc+op_length(decoded[end-1].mode)));
    imm(e,9,S3,S3,end-start);run_commit(e);schedule_count(e,0);
    conditional_tail_commit(e,&decoded[end-1]);
    e->run_full=0;
    unsigned full_done=branch(e,4,ZERO,ZERO);
    patch(e,short_budget,e->n);mov(e,A2,ZERO);schedule_count(e,1);
    unsigned cpu_prefix=branch(e,4,ZERO,ZERO);
    for(unsigned i=0;i<full_rejects_n;++i)patch(e,full_rejects[i],e->n);
    exits_begin=e->exits_n;
    run_prefix_guard(e,total,maximum);
    unsigned prefix_rejects[16], prefix_rejects_n=e->exits_n-exits_begin;
    if(prefix_rejects_n>16) { e->failed=1;return; }
    memcpy(prefix_rejects,e->exits+exits_begin,prefix_rejects_n*sizeof(unsigned));
    e->exits_n=exits_begin;
    schedule_count(e,2);patch(e,cpu_prefix,e->n);
    mov(e,T8,ZERO);e->run_exits_n=0;
    if(control_tail)imm(e,43,S3,SP,24);
    if(decoded[end-1].mode==3)imm(e,43,ZERO,SP,20);
    for(unsigned i=start;i<end;++i) {
        e->pcs[i]=decoded[i].pc;e->emitted=i+1;
        e->run_remaining=end-i;
        instruction(e,decoded[i].pc,decoded[i].opcode,decoded[i].mode);
    }
    e->run_remaining=0;
    for(unsigned i=0;i<e->run_exits_n;++i)patch(e,e->run_exits[i],e->n);
    run_commit(e);
    conditional_tail_commit(e,&decoded[end-1]);
    if(end-start==2 && decoded[end-1].mode==3) {
        /* A one-instruction event prefix used to leave this short branch
         * in native code. Finish it through the original guarded scheduler
         * when real CPU budget remains; never repeat an executed tail.
         * Prefix admission proves positive initial credit, so exactly one
         * instruction has retired here whenever the full two have not. */
        imm(e,35,T0,SP,24);imm(e,9,T0,T0,2);
        unsigned finished=branch(e,4,S3,T0);
        instruction(e,decoded[end-1].pc,decoded[end-1].opcode,decoded[end-1].mode);
        patch(e,finished,e->n);
    }
    /* A short CPU slice commits exactly the executed prefix before exit. */
    unsigned prefix_exit;
    if(control_tail) {
        /* A target may equal the linear continuation, so PC is not proof
         * that the tail executed. Count the actual retired instructions. */
        imm(e,35,T0,SP,24);imm(e,9,T0,T0,end-start);
        prefix_exit=branch(e,5,S3,T0);
    } else {
        imm(e,13,T0,ZERO,(uint16_t)(decoded[end-1].pc+op_length(decoded[end-1].mode)));
        prefix_exit=branch(e,5,S2,T0);
    }
    unsigned done=branch(e,4,ZERO,ZERO);
    /* An event due at the first boundary, pending DMA/IRQ, or an extreme
     * diagnostic carry retains the exact one-instruction scheduler. */
    for(unsigned i=0;i<prefix_rejects_n;++i)patch(e,prefix_rejects[i],e->n);
    e->exits[e->exits_n++]=prefix_exit;
    load_global(e,T0,&h1_jit_run_stats[3]);imm(e,9,T0,T0,1);imm(e,43,T0,T7,0);
    instruction(e,decoded[start].pc,decoded[start].opcode,decoded[start].mode);
    if(end-start==2 && decoded[end-1].mode==3) {
        instruction(e,decoded[end-1].pc,decoded[end-1].opcode,decoded[end-1].mode);
        e->ends[e->ends_n++]=branch(e,4,ZERO,ZERO);
    } else exit_if(e,4,ZERO,ZERO);
    patch(e,done,e->n);patch(e,full_done,e->n);
    ++h1_jit_run_stats[0];h1_jit_run_stats[1]+=end-start;
}

static void flush_code(uint32_t *start, unsigned words)
{
    uintptr_t p=(uintptr_t)start, end=p+words*4;
    __asm__ volatile("sync" ::: "memory");
    for (;p<end;p+=16) __asm__ volatile("cache 0x15, 0(%0)"::"r"(p):"memory");
    __asm__ volatile("sync" ::: "memory");
    for (p=(uintptr_t)start;p<end;p+=16)
        __asm__ volatile("cache 0x10, 0(%0)"::"r"(p):"memory");
    __asm__ volatile("sync\n\tnop\n\tnop\n\tnop" ::: "memory");
}
static int link_candidate(uint16_t pc)
{
    if(h1_hle_sgz2_enabled && pc==0xfb28)return 0;
    if(pc<0x8000 || ARead[pc]!=CartBR || PRGIsRAM[pc>>11])return 0;
    uint8_t op=Page[pc>>11][pc];
    unsigned mode=h1_jit_modes[op];
    if(op==0x60)return 0; /* one-instruction returns stay in the C dispatcher */
    if(!mode || mode==3 || mode==5 || mode==9)return 0;
    unsigned next=(unsigned)pc+op_length(mode);
    if((next>>11)!=(pc>>11) || ARead[next]!=CartBR)return 0;
    if(terminal_io(pc,mode))return 0;
    return h1_jit_modes[Page[pc>>11][next]]!=0;
}
static void link_next(emitter *e, uint16_t *pcs, uint8_t *modes, unsigned n)
{
    /* One pinned O32 frame spans the chain. A validated target may reuse its
     * credential; recursive CPU compilation can append but cannot reclaim
     * the arena until every suspended native caller has returned. */
    unsigned last=n-1;
    if(terminal_io(pcs[last],modes[last]))return;
    uint16_t next=(uint16_t)(pcs[last]+op_length(modes[last]));
    uint16_t taken=modes[last]==3?(uint16_t)(next+(int8_t)Page[pcs[last]>>11][pcs[last]+1]):next;
    if(modes[last]==5) {
        taken=Page[pcs[last]>>11][pcs[last]+1]|(Page[pcs[last]>>11][pcs[last]+2]<<8);
        next=taken;
    }
    int dynamic=modes[last]==9 || Page[pcs[last]>>11][pcs[last]]==0x60;
    int fall=dynamic || link_candidate(next), take=dynamic || link_candidate(taken);
    for(unsigned i=0;i<n;++i)if(pcs[i]==taken)take=0; /* already an inline back edge */
    if(!fall && !take)return;
    if(!dynamic && (!fall || !take || taken==next)) {
        imm(e,13,T0,ZERO,fall?next:taken);exit_if(e,5,S2,T0);
    }
    imm(e,35,T0,S0,16);exit_if(e,6,T0,ZERO);
    irq_guard(e);
    imm(e,11,T0,S3,CHAIN_LIMIT);exit_if(e,4,T0,ZERO);
    load_global(e,T1,&h1_jit_entries);shift(e,0,T0,S2,4);reg(e,33,T1,T1,T0);
    imm(e,35,T9,T1,0);exit_if(e,4,T9,ZERO);
    imm(e,36,T0,T1,15);exit_if(e,4,T0,ZERO); /* fast chained entry only */
    imm(e,35,T0,T1,8);load_global(e,T2,&h1_jit_epoch);exit_if(e,5,T0,T2);
    imm(e,35,T9,T9,-4); /* private gate validates its slice credential */
    load_global(e,T0,&h1_jit_links);imm(e,9,T0,T0,1);imm(e,43,T0,T7,0);
    imm(e,9,T9,T9,PROLOGUE_WORDS*4);emit(e,(T9<<21)|8);emit(e,0);
}
void h1_nes_jit_compile(uint16_t pc, uint8_t first, uint32_t page, int force)
{
    if (!cache || !h1_jit_entries || (!force && h1_jit_dispatch_disabled)) return;
    h1_nes_jit_bind(pc,first,page);
    h1_jit_entry *entry=&h1_jit_entries[pc];
    h1_jit_contract_stamps[pc]=0;
    entry->run=NULL;entry->page=page;entry->epoch=h1_jit_epoch;
    entry->opcode=first;entry->heat=255;entry->ops=0;entry->pad=0;
    ++h1_jit_stats[2];
    static block scratch;
    block *b=&scratch;
    uint8_t ops[MAX_OPS],modes[MAX_OPS]; uint16_t pcs[MAX_OPS]; unsigned n=0;
    uint32_t cursor=pc;
    b->epoch=h1_jit_epoch;b->pc=pc;b->opcode=first;b->page=page;b->ops=0;b->words=0;
    if (ARead[pc]!=CartBR || PRGIsRAM[pc>>11] || (uint32_t)Page[pc>>11]!=page)return;
    while (n<MAX_OPS && cursor<=65535 && (cursor>>11)==(pc>>11)) {
        /* The admitted ROM's wait loop must reach the semantic dispatcher,
         * including fall-through from a preceding block. A native back edge
         * would otherwise spend this slice repeating the original loop.
         * Rejected HLE admissions retain the ordinary interpreter here. */
        if(h1_hle_sgz2_enabled && cursor==0xfb28)break;
        if (ARead[cursor]!=CartBR) break;
        uint8_t op=Page[cursor>>11][cursor],mode=h1_jit_modes[op];
        unsigned length=op_length(mode);
        if (!mode || ((cursor+length-1)>>11)!=(pc>>11)) break;
        pcs[n]=(uint16_t)cursor;ops[n]=op;modes[n++]=mode;
        cursor+=length;if (mode==3 || mode==5 || mode==9 || op==0x60 || terminal_io(pcs[n-1],mode))break;
    }
    if (n<2) { ++h1_jit_stats[5];return; }
    /* A decoded block containing only legal NOPs has no useful native work.
     * Preserve the existing page/epoch negative cache; force still tests the
     * emitter. Mixed blocks and unofficial opcodes retain their old path. */
    if (!force && first==0xea) {
        unsigned i=0;
        while (i<n && ops[i]==0xea) ++i;
        if (i==n) { ++h1_jit_stats[5];return; }
    }
    emitter e={0};e.b=b;
    e.fast=pc>=0x8000 && stable_policy && (uintptr_t)MapIRQHook==known_irq && !PPU_hook;
    e.overclock=!!overclocked;
    for (unsigned i=0;i<n;++i) {
        e.modes[i]=modes[i];
        if (modes[i]==4) {
            uint16_t address=Page[pc>>11][pcs[i]+1]|(Page[pc>>11][pcs[i]+2]<<8);
            readfunc handler=ARead[address];
            if (handler!=CartBR && handler!=h1_cpu_jit_namco_wram &&
                handler!=h1_cpu_jit_raml && handler!=h1_cpu_jit_ramh &&
                (address!=0x2002 || handler!=known_status_read))e.fast=0;
            if(address==0x2002 && handler==known_status_read && InputScanlineHook)e.fast=0;
        }
        if(modes[i]==7) {
            uint16_t address=absolute_address(pcs[i]);
            readfunc handler=ARead[address];
            if(!pure_read(handler))e.fast=0;
            for(unsigned j=0;j<256;++j) {
                uint16_t target=(uint16_t)(address+j);
                if(ARead[target]!=handler || (((target^address)&0x100) &&
                   ARead[target^0x100]!=handler))e.fast=0;
            }
        }
        if(modes[i]==8) {
            uint16_t address=absolute_address(pcs[i]);
            if(!pure_ram_write(address) || ARead[address]!=(address<0x800?
                h1_cpu_jit_raml:h1_cpu_jit_ramh))e.fast=0;
        }
        if(modes[i]==9) {
            uint16_t address=absolute_address(pcs[i]);
            uint16_t high=(address&0xff00)|((address+1)&255);
            if(!pure_read(ARead[address]) || !pure_read(ARead[high]))e.fast=0;
        }
    }
    /* The complete guard tier is retained for backend verification. Its
     * measured cost is too high for automatic admission into a live game. */
    if (!e.fast && !force) { ++h1_jit_stats[5];return; }
    native_prologue(&e,pc);
    _Static_assert(sizeof(h1_jit_entry)==16, "Native chain table stride");
    if(e.n!=PROLOGUE_WORDS) return; /* shared-frame entry matches link_next */
    constant(&e,S4,page+32768);constant(&e,S5,(uintptr_t)&Page[pc>>11]);
    constant(&e,S6,(uintptr_t)&timestamp);
    constant(&e,S7,e.fast?(uintptr_t)ZNTable:(uintptr_t)CartBR);
    if (e.fast) {
        stable_entry_guard(&e,pc,ops,pcs,n);
        load_global(&e,T0,&h1_jit_contract_epoch);
        unsigned active=branch(&e,5,T0,ZERO);
        load_global(&e,T0,&h1_jit_contract_dirty);unsigned clean=branch(&e,4,T0,ZERO);
        call(&e,(uintptr_t)h1_nes_jit_contract_refresh);patch(&e,clean,e.n);
        /* Strict validation can start a fresh safe subinterval after an
         * interpreter operation or opaque callback retired the previous one.
         * No serial bookkeeping is paid for CPU slices without native code. */
        load_global(&e,T0,&h1_jit_contract_serial);imm(&e,9,T0,T0,1);imm(&e,43,T0,T7,0);
        unsigned serial_ok=branch(&e,5,T0,ZERO);
        call(&e,(uintptr_t)h1_nes_jit_contract_wrap);imm(&e,13,T0,ZERO,1);
        patch(&e,serial_ok,e.n);
        constant(&e,T1,(uintptr_t)&h1_jit_contract_epoch);imm(&e,43,T0,T1,0);
        patch(&e,active,e.n);
        constant(&e,T1,(uintptr_t)&h1_jit_contract_stamps[pc]);imm(&e,43,T0,T1,0);
    }
    reload_cpu(&e);
    unsigned body=e.n;
    if(e.fast && !e.overclock) {
        /* One check covers the block: its only path that can create a new
         * pending DMA is the original sound hook, which returns to C when it
         * makes DMA pending, after completing that opcode. Entering with DMA interprets
         * the complete instruction before any native clocks are changed. */
        constant(&e,T1,(uintptr_t)&h1_apu_clock.size);imm(&e,35,T0,T1,0);
        unsigned no_dma=branch(&e,4,T0,ZERO);
        imm(&e,36,T0,T1,4);exit_if(&e,4,T0,ZERO);patch(&e,no_dma,e.n);
    }
    decoded_op decoded[MAX_OPS];
    uint16_t back_target=0;
    if(modes[n-1]==3)back_target=(uint16_t)(pcs[n-1]+2+(int8_t)Page[pc>>11][pcs[n-1]+1]);
    else if(modes[n-1]==5)back_target=absolute_address(pcs[n-1]);
    for(unsigned i=0;i<n;++i) {
        decoded[i]=(decoded_op){pcs[i],ops[i],modes[i],cycles(ops[i],modes[i]),
                                e.fast && run_pure(pcs[i],modes[i])};
    }
    for(unsigned i=0;i<n;) {
        unsigned end=i;
        while(end<n && decoded[end].pure && (end==i || pcs[end]!=back_target))++end;
        /* Two instructions now amortize a complete-run clock admission. A run
         * begins at each possible internal branch destination, so no native
         * back edge can skip its budget proof or partial clock commit. */
        if(end-i>=2) { emit_region(&e,decoded,i,end);i=end; }
        else {
            e.pcs[i]=pcs[i];e.labels[i]=e.n;e.emitted=i+1;
            instruction(&e,pcs[i],ops[i],modes[i]);++i;
        }
    }
    for (unsigned i=0;i<e.ends_n;++i)patch(&e,e.ends[i],e.n);
    spill_cpu(&e);
    if(e.fast)link_next(&e,pcs,modes,n);
    unsigned normal=branch(&e,4,ZERO,ZERO);
    for (unsigned i=0;i<e.stale_n;++i) patch(&e,e.stale[i],e.n);
    constant(&e,T1,(uintptr_t)&entry->epoch);imm(&e,43,ZERO,T1,0);
    imm(&e,13,S3,S3,0x8000);
    unsigned stale_done=branch(&e,4,ZERO,ZERO);
    for (unsigned i=0;i<e.exits_n;++i)patch(&e,e.exits[i],e.n);
    spill_cpu(&e);patch(&e,normal,e.n);patch(&e,stale_done,e.n);
    /* The stable entry has not reloaded resident A/P yet. The preceding
     * chained block already spilled them; do not spill page/ZNTable pointers. */
    unsigned return_packet=e.n;
    if(e.fast)patch(&e,e.page_miss,return_packet);
    shift(&e,0,V0,S3,16);reg(&e,37,V0,V0,S2);
    load_global(&e,T0,&h1_jit_native_depth);imm(&e,9,T0,T0,-1);imm(&e,43,T0,T7,0);
    imm(&e,35,S7,SP,28);imm(&e,35,S6,SP,32);imm(&e,35,S5,SP,36);imm(&e,35,S4,SP,40);
    imm(&e,35,S3,SP,44);imm(&e,35,S2,SP,48);imm(&e,35,S1,SP,52);
    imm(&e,35,S0,SP,56);imm(&e,35,RA,SP,60);
    emit(&e,(RA<<21)|8);imm(&e,9,SP,SP,64);
    unsigned fast_offset=e.n;
    if(e.fast) {
        /* This entry is private to the proved dispatcher/linker protocol.
         * Its prologue has the same pinned shared-frame entry offset.
         * Retain the cheap Page guard even though mapped writes revoke. */
        native_prologue(&e,pc);
        load_global(&e,T0,&h1_jit_contract_epoch);
        unsigned unproved=branch(&e,4,T0,ZERO);patch(&e,unproved,PROLOGUE_WORDS);
        constant(&e,T1,(uintptr_t)&h1_jit_contract_stamps[pc]);imm(&e,35,T1,T1,0);
        unproved=branch(&e,5,T0,T1);patch(&e,unproved,PROLOGUE_WORDS);
        schedule_count(&e,3);
        constant(&e,T1,(uintptr_t)&Page[pc>>11]);imm(&e,35,T0,T1,0);
        constant(&e,T1,page);unsigned mismatch=branch(&e,5,T0,T1);
        patch(&e,mismatch,return_packet);
        constant(&e,S6,(uintptr_t)&timestamp);reload_cpu(&e);
        unsigned enter=branch(&e,4,ZERO,ZERO);patch(&e,enter,body);
    }
    if (e.failed) { ++h1_jit_stats[5];return; }
    /* The last header word contains the separately validated entry. Both
     * native entry addresses remain 32-bit functions, never writable code. */
    size_t bytes=32u+((e.n*4u+31u)&~31u);
    if (used_bytes+bytes>cache_bytes) {
        /* An opaque handler can recursively run the CPU while its caller
         * is suspended in generated code. Appending remains safe, but an
         * arena wrap would overwrite that caller's live return address.
         * Leave this hot entry retryable after the outer native call ends. */
        if (h1_jit_native_depth) { entry->heat=8;return; }
        h1_nes_jit_invalidate();used_bytes=0;++h1_jit_wraps;
        entry->epoch=h1_jit_epoch;
    }
    uint32_t *code=(uint32_t*)(cache+used_bytes+32u);
    code[-1]=(uintptr_t)(code+(e.fast?fast_offset:0));
    memcpy(code,b->code,e.n*4u);flush_code(code-8,e.n+8);used_bytes+=bytes;h1_jit_emitted_bytes+=(uint32_t)bytes;
    entry->ops=n;entry->pad=e.fast;entry->run=(h1_jit_fn)code;++h1_jit_stats[0];
    if (e.n>h1_jit_stats[7]) h1_jit_stats[7]=e.n;
    backoff_compile_end(force);
}
void h1_nes_jit_invalidate(void)
{
    h1_nes_jit_contract_revoke();
    /* Only metadata changes here: a hardware hook can trigger a reset while
     * native code is active. Arena reclamation happens only in the compiler. */
    if (++h1_jit_epoch==0) {
        h1_jit_epoch=1;
        if (h1_jit_entries) for(unsigned i=0;i<65536;++i)h1_jit_entries[i].epoch=0;
        if (bank_entries) for(unsigned i=0;i<BANK_SETS*BANK_WAYS;++i)bank_entries[i].entry.epoch=0;
    }
    ++h1_jit_stats[6];
}
void h1_nes_jit_contract_wrap(void)
{
    if(h1_jit_contract_stamps)memset(h1_jit_contract_stamps,0,65536u*sizeof(uint32_t));
    h1_jit_contract_serial=1;
}
static int frame_allowed(void)
{
    /* The PPU loop cannot write PRG or CPU handlers with these pinned hooks.
     * Input/cheats run before this scope, audio/frontend callbacks after it.
     * Unknown hooks and overclock transitions retain per-call validation. */
    /* An allocated native table is unnecessary for the same proved
     * PPU-frame clock scope. All observer/mapper/overclock guards below stay
     * unchanged; NULL-stamp lifecycle helpers already check their pointers. */
    return (h1_jit_entries || h1_hle_sgz2_enabled) && stable_policy &&
        (uintptr_t)MapIRQHook==known_irq && !PPU_hook &&
        !GameHBIRQHook && !GameHBIRQHook2 && !InputScanlineHook && !MMC5Hack &&
        GameInfo && GameInfo->type==GIT_CART && !overclock_enabled && !overclocked;
}
void h1_nes_jit_frame_begin(void)
{
    backoff_frame_begin();
    h1_cpu_apu_deadline=timestamp;
    h1_nes_jit_contract_revoke();h1_jit_contract_dirty=0;
    h1_jit_contract_frame=frame_allowed();
}
void h1_nes_jit_contract_refresh(void)
{
    /* An opaque callback can install a PPU/input hook without mapping PRG.
     * Revalidate before granting any new credential; if unsafe, every CPU
     * return retires it before the PPU can invoke that hook between slices. */
    if(h1_jit_contract_frame && !frame_allowed())h1_jit_contract_frame=0;
    h1_jit_contract_dirty=0;
}
void h1_nes_jit_frame_end(void)
{ h1_cpu_apu_deadline=timestamp;h1_jit_contract_frame=0;h1_nes_jit_contract_revoke();backoff_frame_end(); }
void h1_nes_jit_open(unsigned mapper)
{
    h1_nes_jit_close();memset(h1_jit_stats,0,sizeof(h1_jit_stats));h1_jit_wraps=0;h1_jit_links=0;memset(h1_jit_run_stats,0,sizeof(h1_jit_run_stats));
    memset(h1_jit_bank_stats,0,sizeof(h1_jit_bank_stats));
    memset(h1_jit_schedule_stats,0,sizeof(h1_jit_schedule_stats));
    memset(h1_hle_stats,0,sizeof(h1_hle_stats));
    known_irq=(uintptr_t)MapIRQHook;known_status_read=ARead[0x2002];
    stable_policy=(mapper==0 && !MapIRQHook) || (mapper==19 && MapIRQHook==NamcoIRQHook);
    if(PPU_hook)stable_policy=0;
    if (stable_policy) {
        for(unsigned i=16;i<32;++i)if(PRGIsRAM[i])stable_policy=0;
        for(unsigned i=0x8000;i<0x10000;++i)if(ARead[i]!=CartBR)stable_policy=0;
    }
    if (!stable_policy) {
        h1_diag("JIT_OPEN ready=0 reason=mapper_policy mapper=%u fallback=interpreter",mapper);return;
    }
    static const uint8_t sgz2_md5[16] = {
        0xde,0x31,0x9a,0x7a,0x80,0x99,0xc3,0x88,
        0x81,0x0c,0x59,0xcb,0x03,0x8e,0x8c,0x9d
    };
    h1_hle_sgz2_enabled = mapper == 19 && iNESCart.mapper == 19 &&
        iNESCart.PRGRomSize == 0x40000 && iNESCart.CHRRomSize == 0x40000 &&
        !memcmp(iNESCart.MD5,sgz2_md5,sizeof sgz2_md5);
    if (h1_hle_sgz2_enabled) {
        hle_sgz2_page = PRGptr[0] + 0x30000;
        h1_diag("HLE_OPEN ready=1 profile=SGZ2_CN_IRQ_WAIT pc=FB28 event_bound=1");
    }
    if (h1_hle_sgz2_enabled) {
        /* Keep HLE and the exact optimized C clock loop independently of
         * native executable storage. There is no table to stamp or compile.
         * Unload still owns normal cleanup; a live RAM transplant must keep
         * its original application's already-owned arena untouched. */
        h1_jit_contract_serial=0;h1_jit_contract_dirty=0;
        h1_nes_jit_invalidate();
        h1_diag("CPU_OPEN backend=C_EVENT_HLE profile=SGZ2_CN native=0 arena_bytes=0");
        return;
    }
    size_t bytes=(size_t)H1_JIT_CACHE_MIB*1024*1024;
    size_t table_bytes=65536u*sizeof(h1_jit_entry);
    size_t contract_bytes=65536u*sizeof(uint32_t);
    size_t bank_bytes=BANK_SETS*BANK_WAYS*sizeof(bank_entry)+BANK_SETS;
    allocation=malloc(bytes+table_bytes+bank_bytes+contract_bytes+31);
    uintptr_t aligned=((uintptr_t)allocation+31)&~(uintptr_t)31;
    if (!allocation || aligned<0x80000000u || aligned+bytes+table_bytes+bank_bytes+contract_bytes>0x83c00000u) {
        free(allocation);allocation=NULL;
        h1_diag("JIT_OPEN ready=0 fallback=interpreter");return;
    }
    cache=(uint8_t*)aligned;cache_bytes=bytes;used_bytes=0;
    h1_jit_entries=(h1_jit_entry*)(cache+bytes);
    bank_entries=(bank_entry*)((uint8_t*)h1_jit_entries+table_bytes);
    bank_victims=(uint8_t*)(bank_entries+BANK_SETS*BANK_WAYS);
    h1_jit_contract_stamps=(uint32_t*)(bank_victims+BANK_SETS);
    memset(h1_jit_contract_stamps,0,contract_bytes);h1_jit_contract_serial=0;
    memset(bank_entries,0,bank_bytes);
    memset(h1_jit_entries,0,table_bytes);h1_nes_jit_invalidate();
    h1_diag("JIT_OPEN ready=1 backend=MIPS32_EVENT_V6 code_bytes=%u entry_bytes=%u bank_bytes=%u contract_bytes=%u bank_slots=%u max_ops=%u hot_threshold=8 stable_hooks=%u",
            (unsigned)bytes,(unsigned)table_bytes,(unsigned)bank_bytes,(unsigned)contract_bytes,BANK_SETS*BANK_WAYS,MAX_OPS,(unsigned)stable_policy);
}
void h1_nes_jit_close(void)
{
    if (h1_hle_sgz2_enabled)
        h1_diag("HLE_CLOSE batches=%u pairs=%u cycles=%u",h1_hle_stats[0],h1_hle_stats[1],h1_hle_stats[2]);
    h1_hle_sgz2_enabled=0;hle_sgz2_page=NULL;
    if (allocation) h1_diag("JIT_CLOSE compiled=%u hits=%u misses=%u native_ops=%u deopts=%u rejected=%u invalidations=%u max_words=%u",
        h1_jit_stats[0],h1_jit_stats[1],h1_jit_stats[2],h1_jit_stats[3],
        h1_jit_stats[4],h1_jit_stats[5],h1_jit_stats[6],h1_jit_stats[7]);
    if (allocation)h1_diag("JIT_CACHE used_bytes=%u wraps=%u",(unsigned)used_bytes,h1_jit_wraps);
    if (allocation)h1_diag("JIT_LINK transitions=%u",h1_jit_links);
    if (allocation)h1_diag("JIT_RUN compiled=%u ops=%u executed=%u rejected=%u",h1_jit_run_stats[0],h1_jit_run_stats[1],h1_jit_run_stats[2],h1_jit_run_stats[3]);
    if (allocation)h1_diag("JIT_SCHEDULE full=%u cpu_prefix=%u event_prefix=%u contract_reuse=%u",h1_jit_schedule_stats[0],h1_jit_schedule_stats[1],h1_jit_schedule_stats[2],h1_jit_schedule_stats[3]);
    if (allocation)h1_diag("JIT_BANK lookups=%u hits=%u misses=%u stores=%u evictions=%u native_reuse=%u reject_reuse=%u epoch_misses=%u",
        h1_jit_bank_stats[0],h1_jit_bank_stats[1],h1_jit_bank_stats[2],h1_jit_bank_stats[3],
        h1_jit_bank_stats[4],h1_jit_bank_stats[5],h1_jit_bank_stats[6],h1_jit_bank_stats[7]);
    h1_jit_entries=NULL;bank_entries=NULL;bank_victims=NULL;
    h1_jit_contract_stamps=NULL;h1_nes_jit_frame_end();
    cache=NULL;cache_bytes=used_bytes=0;free(allocation);allocation=NULL;backoff_reset();
}
