"""Prepare the optimized C/event core and optional experimental native hooks."""
SCHEDULE = r'''
/* One exact no-event interval covers both the mapper and APU. Counters stay
 * current at each opcode; only event checks and mapper classification are
 * cached. Native event handlers and opaque I/O retire this same deadline. */
extern void NamcoIRQHook(int cycles);
extern uint16_t h1_namco_irq_count;
extern uint8_t h1_namco_irq_enabled;
static inline void h1_cpu_mapper_clock(int cycles);
uint32_t h1_cpu_apu_deadline;
static unsigned h1_cpu_clock_mode;
static inline void h1_cpu_apu_flush(int pending) {
    (void)pending;
    h1_cpu_apu_deadline = timestamp;
}
static __attribute__((noinline)) void h1_cpu_clock_slow(int cycles) {
    h1_cpu_apu_deadline = timestamp;
    /* The mapper observes the original temp before the APU, including
     * unknown callbacks that recursively execute CPU or change overclock. */
    h1_cpu_mapper_clock(cycles);
    if (overclocked) return;
    int32_t cost = cycles * 48;
    int32_t frame = h1_apu_clock.frame, dmc = h1_apu_clock.dmc;
    if (frame > cost && dmc > cycles && (!h1_apu_clock.size || h1_apu_clock.dma)) {
        frame -= cost;
        dmc -= cycles;
        h1_apu_clock.frame = frame;
        h1_apu_clock.dmc = dmc;
        /* A mapper callback may add DMA cycles or recursively execute CPU.
         * Timestamp then contains uncommitted APU carry: do not use it as
         * the next-event anchor until a later clock consumes that carry. */
        if (cycles > 0 && X.tcount == 0 &&
            (!MapIRQHook || MapIRQHook == NamcoIRQHook)) {
            /* ceil(frame/64) is conservative for the real 48-unit clock. */
            uint32_t credit = (((uint32_t)frame - 1) >> 6) + 1;
            if (credit > (uint32_t)dmc) credit = (uint32_t)dmc;
            h1_cpu_clock_mode = MapIRQHook == NamcoIRQHook && h1_namco_irq_enabled;
            if (h1_cpu_clock_mode) {
                uint32_t count = h1_namco_irq_count;
                if (count >= 0x7fff) return;
                uint32_t mapper_credit = 0x7fff - count;
                if (credit > mapper_credit) credit = mapper_credit;
            }
            h1_cpu_apu_deadline = timestamp + credit;
        }
        return;
    }
    FCEU_SoundCPUHook(cycles);
}
static inline void h1_cpu_clock(int cycles) {
    if ((int32_t)(h1_cpu_apu_deadline - timestamp) > 0 && cycles > 0) {
        if (h1_cpu_clock_mode)
            h1_namco_irq_count = (uint16_t)(h1_namco_irq_count + cycles);
        h1_apu_clock.frame -= cycles * 48;
        h1_apu_clock.dmc -= cycles;
    } else h1_cpu_clock_slow(cycles);
}
'''

CHECKPOINT = r'''
#include "platform/nes_jit.h"
#include "platform/profile.h"
extern uint8_t PRGIsRAM[32];
extern void NamcoIRQHook(int cycles);
extern uint16_t h1_namco_irq_count;
extern uint8_t h1_namco_irq_enabled;
/* The admitted mapper's inactive IRQ is the common case. Keep its uint16
 * wrap and event threshold exactly; unknown hooks retain their own call. */
static inline void h1_cpu_mapper_clock(int cycles) {
    if (MapIRQHook == NamcoIRQHook) {
        if (h1_namco_irq_enabled) {
            uint16_t next = (uint16_t)(h1_namco_irq_count + cycles);
            if (next < 0x7fff) h1_namco_irq_count = next;
            else NamcoIRQHook(cycles);
        }
    } else if (MapIRQHook) {
        h1_cpu_apu_flush(cycles);
        h1_nes_jit_contract_unknown(); MapIRQHook(cycles);
        h1_cpu_apu_flush(0);
        h1_nes_jit_contract_unknown();
    }
}
uint32_t h1_cpu_jit_read(uint32_t address) {
    return RdMemNorm(address);
}
'''


def _replace_jit_once(source, old, new):
    assert source.count(old) == 1, (source.count(old), old[:100])
    return source.replace(old, new)


def _prepare_c_hle_dispatcher(source):
    start = source.index('static void h1_cpu_run_jit(int32_t cycles)')
    end = source.index('\n#undef _FCEU_OPS_H\nstatic void h1_cpu_run_interpreter', start)
    original = source[start:end].rstrip('\n')
    specialized=original.replace('static void h1_cpu_run_jit(int32_t cycles)',
                                  'static void h1_cpu_run_c_hle(int32_t cycles)',1)
    start=specialized.index('        /* At most 96 count units')
    end=specialized.index('\n\n\t\tADDCYC(CycTable[b1]);',start)
    specialized=specialized[:start]+'''        /* The default backend uses the optimized C scheduler.
         * Fetch/PI/IRQ/clock/HLE semantics above and below are the exact same
         * source as the native dispatcher. Only native admission is absent;
         * no mode boolean is tested inside this opcode loop. */'''+specialized[end:]
    marker='\n#undef _FCEU_OPS_H\nstatic void h1_cpu_run_interpreter(int32_t cycles)'
    source=_replace_jit_once(source,marker,'\n#undef _FCEU_OPS_H\n'+specialized+'\n'+marker)
    marker='''void X6502_Run(int32_t cycles)
{
    if (h1_jit_entries) {'''
    source=_replace_jit_once(source,marker,'''void X6502_Run(int32_t cycles)
{
    if (h1_hle_sgz2_enabled || h1_jit_entries) {
        /* One policy selection per CPU scheduling slice, including recursive
         * calls. Keep the original frame/opaque boundaries around either
         * loop; no stale clocks or native frames cross this selection. */
        void (*run)(int32_t) = H1_NATIVE_JIT && !h1_hle_sgz2_enabled &&
            !h1_jit_dispatch_disabled ? h1_cpu_run_jit : h1_cpu_run_c_hle;''')
    assert source.count('h1_cpu_run_jit(cycles);')==2
    return source.replace('h1_cpu_run_jit(cycles);','run(cycles);')


def prepare_jit(core):
    ppu = core / 'src/ppu.c'
    text = ppu.read_text()
    assert text.count('static DECLFR(A2002)') == 1
    text = text.replace('static DECLFR(A2002)', 'DECLFR(A2002)')
    marker = 'static DECLFW(B2007)'
    assert text.count(marker) == 1
    text = text.replace(marker,
        'writefunc const h1_ppu_code_safe_writes[7] = {B2000, B2001, B2002, B2003, B2004, B2005, B2006};\n\n' + marker)
    ppu.write_text(text, newline='\n')
    cart = core / 'src/cart.c'
    text = cart.read_text()
    assert text.count('static uint8_t PRGIsRAM[32]') == 1
    text = text.replace('static uint8_t PRGIsRAM[32]', 'uint8_t PRGIsRAM[32]')
    # The admitted mapper writes Page/PRGIsRAM only through these cart APIs;
    # all PRG-bank and handler mutations retire the current slice credential.
    for marker in (
            'static INLINE void setpageptr(int s, uint32_t A, uint8_t *p, int ram) {',
            'void ResetCartMapping(void) {',
            'void SetupCartPRGMapping(int chip, uint8_t *p, uint32_t size, int ram) {',
            'DECLFW(CartBW) {'):
        assert text.count(marker) == 1, marker
        text = text.replace(marker, marker+'\n\th1_nes_jit_contract_revoke();')
    cart.write_text('#include "platform/nes_jit.h"\n'+text, newline='\n')
    namco = core / 'src/boards/n106.c'
    text = namco.read_text()
    marker = 'static void FP_FASTAPASS(1) NamcoIRQHook'
    assert text.count(marker) == 1
    namco.write_text(text.replace(marker, 'void FP_FASTAPASS(1) NamcoIRQHook'), newline='\n')
    text = namco.read_text().replace('AWRAM', 'h1_cpu_jit_namco_wram')
    text = text.replace('static DECLFR(h1_cpu_jit_namco_wram)', 'DECLFR(h1_cpu_jit_namco_wram)')
    assert text.count('static DECLFW(BWRAM)') == 1
    text = text.replace('BWRAM', 'h1_cpu_jit_namco_wram_write').replace(
        'static DECLFW(h1_cpu_jit_namco_wram_write)', 'DECLFW(h1_cpu_jit_namco_wram_write)')
    namco.write_text(text, newline='\n')
    text = namco.read_text()
    for original, exported, declaration in (
            ('IRQCount', 'h1_namco_irq_count', 'uint16_t'),
            ('IRQa', 'h1_namco_irq_enabled', 'uint8_t')):
        text = text.replace(original, exported).replace(
            'static '+declaration+' '+exported+';', declaration+' '+exported+';')
    namco.write_text(text, newline='\n')
    sound = core / 'src/sound.c'
    text = sound.read_text()
    import re
    for original, exported, declaration in (
            ('fhcnt', 'h1_apu_clock.frame', 'int32_t'),
            ('DMCacc', 'h1_apu_clock.dmc', 'int32_t'),
            ('DMCSize', 'h1_apu_clock.size', 'int32_t'),
            ('DMCHaveDMA', 'h1_apu_clock.dma', 'char')):
        assert len(re.findall(r'static '+declaration+' '+original+r'\b', text)) == 1
        text = re.sub(r'static '+declaration+' '+original+r'\s*=\s*\d+;', '', text)
        text = re.sub(r'\b'+original+r'\b', exported, text)
    text = '#include "platform/nes_jit.h"\nh1_jit_apu_clock h1_apu_clock = {0,1,0,0};\n' + text
    marker = 'void FASTAPASS(1) FCEU_SoundCPUHook(int cycles) {'
    assert text.count(marker) == 1
    text = text.replace(marker, marker + '\n\th1_cpu_apu_deadline = timestamp;')
    marker = '\t}\n}\n\nstatic void RDoPCM(void) {'
    assert text.count(marker) == 1
    text = text.replace(marker, '\t}\n\th1_cpu_apu_deadline = timestamp;\n}\n\nstatic void RDoPCM(void) {')
    text = text.replace('#include \"platform/nes_jit.h\"', '#include \"platform/nes_jit.h\"\nextern uint32_t h1_cpu_apu_deadline;', 1)
    sound.write_text(text, newline='\n')
    fceu = core / 'src/fceu.c'
    text = fceu.read_text()
    for original, exported in (('ARAML','h1_cpu_jit_raml'),('ARAMH','h1_cpu_jit_ramh')):
        assert text.count('static DECLFR('+original+')') == 1
        text = text.replace(original, exported).replace('static DECLFR('+exported+')', 'DECLFR('+exported+')')
    for original, exported in (('BRAML','h1_cpu_jit_ram_write_l'),('BRAMH','h1_cpu_jit_ram_write_h')):
        assert text.count('static DECLFW('+original+')') == 1
        text = text.replace(original, exported).replace('static DECLFW('+exported+')', 'DECLFW('+exported+')')
    for marker in (
            'void FlushGenieRW(void)\n{',
            'void FASTAPASS(3) SetReadHandler(int32_t start, int32_t end, readfunc func)\n{',
            'void FASTAPASS(3) SetWriteHandler(int32_t start, int32_t end, writefunc func)\n{'):
        assert text.count(marker) == 1, marker
        text = text.replace(marker, marker+'\n\th1_nes_jit_contract_revoke();')
    marker = '\tr = FCEUPPU_Loop(skip);'
    assert text.count(marker) == 1
    text = text.replace(marker, '\th1_nes_jit_frame_begin();\n'+marker+'\n\th1_nes_jit_frame_end();')
    fceu.write_text('#include "platform/nes_jit.h"\n'+text, newline='\n')
    path = core / 'src/x6502.c'
    text = path.read_text()
    # Untranslated instructions may continue a proved code interval when all
    # memory callbacks are pinned pure handlers. Unknown callbacks retire it
    # before invocation, including callbacks that modify their own ARead slot.
    declarations = '''#include "platform/nes_jit.h"
#include "platform/jit_backoff.h"
#include "ppu.h"
#include "input.h"
extern DECLFR(A2002);
extern DECLFR(h1_cpu_jit_namco_wram);
extern DECLFW(h1_cpu_jit_namco_wram_write);
extern DECLFR(h1_cpu_jit_raml);
extern DECLFR(h1_cpu_jit_ramh);
extern DECLFW(h1_cpu_jit_ram_write_l);
extern DECLFW(h1_cpu_jit_ram_write_h);
extern writefunc const h1_ppu_code_safe_writes[7];
static inline void h1_cpu_apu_flush(int pending);
'''
    text = text.replace('#include "sound.h"', '#include "sound.h"\n'+declarations)
    marker = ('static INLINE uint8_t RdMemNorm(uint32_t A) {\n'
              '\treadfunc handler = ARead[A];\n'
              '\tif (handler == CartBR) return (_DB = Page[A >> 11][A]);\n'
              '\treturn (_DB = handler(A));\n}')
    assert text.count(marker) == 1
    text = text.replace(marker, '''static INLINE uint8_t RdMemNorm(uint32_t A) {
    readfunc handler = ARead[A];
    if (handler == CartBR) return (_DB = Page[A >> 11][A]);
    if (handler != h1_cpu_jit_raml &&
        handler != h1_cpu_jit_ramh && handler != h1_cpu_jit_namco_wram &&
        !(handler == A2002 && !PPU_hook && !InputScanlineHook)) {
        h1_cpu_apu_flush(0);
        h1_nes_jit_contract_unknown();
        uint8_t result = handler(A);
        h1_cpu_apu_flush(0);
        h1_nes_jit_contract_unknown();
        return (_DB = result);
    }
    return (_DB = handler(A));
}''')
    marker = 'static INLINE void WrMemNorm(uint32_t A, uint8_t V) {\n\tBWrite[A](A, V);\n}'
    assert text.count(marker) == 1
    text = text.replace(marker, '''static INLINE void WrMemNorm(uint32_t A, uint8_t V) {
    writefunc handler = BWrite[A];
    if (handler != h1_cpu_jit_ram_write_l && handler != h1_cpu_jit_ram_write_h &&
        handler != h1_cpu_jit_namco_wram_write &&
        !(A >= 0x2000 && A < 0x4000 && (A & 7) < 7 &&
          handler == h1_ppu_code_safe_writes[A & 7] && !PPU_hook && !InputScanlineHook)) {
        h1_cpu_apu_flush(0);
        h1_nes_jit_contract_unknown();
        handler(A, V);
        h1_cpu_apu_flush(0);
        h1_nes_jit_contract_unknown();
        return;
    }
    handler(A, V);
}''')
    # DMA retains the original cycle/DB sequence, sharing the same exact
    # callback classification as ordinary bus accesses. Pure APU events may
    # then keep code credentials; an unknown DMC reader still retires them
    # before it can alter mappings, code, CPU handlers or PPU callbacks.
    marker = 'uint8_t FASTAPASS(1) X6502_DMR(uint32_t A) {\n\tADDCYC(1);\n\treturn(X.DB = ARead[A](A));\n}'
    assert text.count(marker) == 1
    text = text.replace(marker, 'uint8_t FASTAPASS(1) X6502_DMR(uint32_t A) {\n\tADDCYC(1);\n\treturn RdMemNorm(A);\n}')
    marker = 'void FASTAPASS(2) X6502_DMW(uint32_t A, uint8_t V) {\n\tADDCYC(1);\n\tBWrite[A](A, V);\n}'
    assert text.count(marker) == 1
    text = text.replace(marker, 'void FASTAPASS(2) X6502_DMW(uint32_t A, uint8_t V) {\n\tADDCYC(1);\n\tWrMemNorm(A, V);\n}')
    assert text.count('static uint8_t ZNTable[256]') == 1
    text = text.replace('static uint8_t ZNTable[256]', 'uint8_t ZNTable[256]')
    run_marker = 'void X6502_Run(int32_t cycles)'
    assert text.count(run_marker) == 1
    original_run = text[text.index(run_marker):]
    marker = 'void X6502_Reset(void) {'
    assert text.count(marker) == 1
    text = text.replace(marker, SCHEDULE + CHECKPOINT + '\n' + marker + '\n\th1_nes_jit_invalidate();')
    marker = '\t\t_PI = _P;\n\t\tb1 = RdMem(_PC);'
    assert text.count(marker) == 1
    text = text.replace(marker,
        '\t\tif (pbackus == 0xfb28 && _count > 432 && h1_nes_hle_wait()) continue;\n' + marker)
    marker = '\t\tb1 = RdMem(_PC);\n'
    assert text.count(marker) == 1
    text = text.replace(marker, '''
        /* CartBR is a pure Page read. Reuse its already checked page for
         * native lookup and revoke before every opaque opcode fetch. */
        uint8_t *opcode_page = NULL;
        if (ARead[pbackus] == CartBR) {
            opcode_page = Page[pbackus >> 11];
            b1 = _DB = opcode_page[pbackus];
        } else {
            h1_cpu_apu_flush(0);
            h1_nes_jit_contract_unknown();
            b1 = RdMem(pbackus);
        }
        /* At most 96 count units allow exactly one shortest opcode to start.
         * Keep its ordinary C semantics, including any cycle overshoot, but
         * avoid entering a native block just to leave at this same boundary. */
        if (_count > 96 && h1_jit_modes[b1] && h1_jit_modes[b1] != 3 &&
            !encryptOpcodes && !X.jammed &&
            opcode_page && !PRGIsRAM[pbackus >> 11]) {
            uint32_t page = (uint32_t)opcode_page;
            h1_jit_entry *entry = &h1_jit_entries[pbackus];
            if (entry->epoch != h1_jit_epoch || entry->page != page || entry->opcode != b1) {
                h1_nes_jit_bind(pbackus,b1,page);
            }
            if (!entry->run && entry->heat < 8) ++entry->heat;
            if (!entry->run && entry->heat == 8) {
                h1_profile_push(HP_JIT);
                h1_nes_jit_compile(pbackus,b1,page,0);
                h1_profile_pop();
            }
            if (entry->run) {
                /* The private native gate checks the credential itself,
                 * avoiding a second indexed metadata walk in C. Generic
                 * forced-test blocks store their strict entry in this slot. */
                h1_jit_fn run = ((h1_jit_fn *)entry->run)[-1];
                uint32_t result = run(&X,RAM);
                unsigned executed = (result >> 16) & 0x7fff;
                ++h1_jit_stats[1]; h1_jit_stats[3] += executed;
                /* Native stale exits clear the failing block's own L1 epoch.
                 * A chain may fail in a later block; keep its valid source. */
                if (result & 0x80000000u) ++h1_jit_stats[4];
                if (executed) { pbackus = (uint16_t)result; continue; }
            }
        }
''')
    text = text.replace(run_marker, 'static void h1_cpu_run_jit(int32_t cycles)')
    assert text.count('\t#define RdMem RdMemNorm\n\t#define WrMem WrMemNorm') == 1
    marker = '\t\tif (MapIRQHook) MapIRQHook(temp);\n\t\tif (!overclocked)\n\t\t\tFCEU_SoundCPUHook(temp);'
    assert text.count(marker) == 1
    text = text.replace(marker, '\t\th1_cpu_clock(temp);')
    marker = '\t\tif (_IRQlow) {'
    assert text.count(marker) == 1
    text = text.replace(marker, marker+'\n\t\t\th1_cpu_apu_flush(0);\n\t\t\th1_nes_jit_contract_revoke();')
    marker = '\t#undef RdRAM\n\t#undef WrRAM\n}'
    assert text.count(marker) == 1
    text = text.replace(marker, '\t#undef RdMem\n\t#undef WrMem\n'+marker)
    # ops.h contains a guard even though its contents are switch cases.
    # Each independently compiled interpreter body must include those cases.
    text += '\n#undef _FCEU_OPS_H\n' + original_run.replace(run_marker, 'static void h1_cpu_run_interpreter(int32_t cycles)')
    text += '''
/* Native event exit: base cycles, previous P, opcode DB and the original
 * mapper/sound hooks are already complete. Execute exactly the original
 * opcode body, including dynamic operand reads and any extra page/branch
 * cycles. Keep X.PC at the current instruction as the original interpreter
 * does during callbacks; return the independently advanced dispatch PC. */
#undef _FCEU_OPS_H
#undef _PC
#define _PC pbackus
#define RdRAM RdRAMFast
#define WrRAM WrRAMFast
#define RdMem RdMemNorm
#define WrMem WrMemNorm
uint32_t h1_cpu_jit_complete(uint32_t pc, uint32_t opcode)
{
    uint16_t pbackus = (uint16_t)(pc + 1);
    switch (opcode) {
        #include "ops.h"
    }
    return pbackus;
}
#undef _PC
#define _PC X.PC
#undef RdRAM
#undef WrRAM
'''
    text += '''
void X6502_Run(int32_t cycles)
{
    if (h1_jit_entries) {
        if (h1_jit_contract_frame) {
            h1_cpu_run_jit(cycles);
            if (!h1_jit_contract_frame) {
                h1_cpu_apu_flush(0);
                h1_nes_jit_contract_revoke();
            } else if (h1_jit_contract_dirty) h1_cpu_apu_flush(0);
            return;
        }
        h1_cpu_apu_flush(0);
        h1_nes_jit_contract_revoke();
        h1_cpu_run_jit(cycles);
        h1_cpu_apu_flush(0);
        h1_nes_jit_contract_revoke();
    } else {
        h1_cpu_apu_flush(0);
        h1_cpu_run_interpreter(cycles);
        h1_cpu_apu_flush(0);
    }
}
'''
    text = _prepare_c_hle_dispatcher(text)
    path.write_text(text, newline='\n')
