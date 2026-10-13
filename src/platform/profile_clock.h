/* JZ4740/JZRISC has no CP0 Count. Use disabled, non-PWM TCU5 only;
 * V1.41 uses TCU0/1/2 and PWM3/4. Follow the TCU5-specific init flow.
 * RTC input: 32768 Hz, 30.52 us resolution, ~2 s period. IRQs stay masked.
 * Save/restore the borrowed channel, never change other timer channels.
 * Reference: JZ4740 Programming Manual sections 8.3 and 8.4.4 (p.159).
 * TDFR/TDHR writes require the counter input clock OFF; TCNT writes require
 * PCLK without prescaling. Selecting RTC before writing them is incorrect. */
#ifndef H1_PROFILE_CLOCK_H
#define H1_PROFILE_CLOCK_H
#include <stdint.h>
#define PC_CHANNEL 5u
#define PC_BIT (1u << PC_CHANNEL)
#define PC_IRQ_BITS (PC_BIT | (PC_BIT << 16))
#define PC_BASE (0x40u + PC_CHANNEL*16u)
#define PC_TOP 65535u
#define PC_PERIOD 65536u
#ifdef H1_PROFILE_TCU_TEST
extern uint32_t pc_read(unsigned offset, unsigned size);
extern void pc_write(unsigned offset, uint32_t value, unsigned size);
#else
static uint32_t pc_read(unsigned offset, unsigned size)
{
    uintptr_t address=0xb0002000u+offset;
    if (size==1) return *(volatile uint8_t *)address;
    if (size==2) return *(volatile uint16_t *)address;
    return *(volatile uint32_t *)address;
}
static void pc_write(unsigned offset, uint32_t value, unsigned size)
{
    uintptr_t address=0xb0002000u+offset;
    if (size==1) *(volatile uint8_t *)address=(uint8_t)value;
    else if (size==2) *(volatile uint16_t *)address=(uint16_t)value;
    else *(volatile uint32_t *)address=value;
}
#endif
static unsigned pc_owned, pc_stopped;
static uint32_t pc_mask, pc_total, pc_previous;
static uint16_t pc_saved[4];
/* Reads cross clock domains. Bound retries and require the hardware FULL flag
 * before extending a backwards sample: one bad read must not invent ~2 s. */
static unsigned pc_fault, pc_retries, pc_backwards, pc_wraps, pc_deferred, pc_terminal;
static uint32_t pc_bad_a, pc_bad_b, pc_bad_previous, pc_max_delta;
static int pc_start(void)
{
    if (pc_owned) return 1;
    if ((pc_read(0x10,1)&PC_BIT) || (pc_read(0x20,4)&PC_IRQ_BITS)) return 0;
    pc_stopped=(pc_read(0x1c,4)&PC_BIT)!=0;
    /* Registers are inaccessible while the channel clock is stopped. */
    if (pc_stopped) pc_write(0x3c,PC_BIT,4);
    for (unsigned i=0;i<4;++i) pc_saved[i]=(uint16_t)pc_read(PC_BASE+i*4,2);
    if (pc_saved[3]&0x80u) {
        if (pc_stopped) pc_write(0x2c,PC_BIT,4);
        return 0;
    }
    pc_mask=pc_read(0x30,4)&PC_IRQ_BITS;
    pc_write(0x34,PC_IRQ_BITS,4);
    pc_write(PC_BASE+12,0x200,2); /* Abrupt shutdown, all input clocks OFF. */
    pc_write(PC_BASE,65535,2);
    pc_write(PC_BASE+4,32767,2);
    pc_write(PC_BASE+12,0x201,2); /* PCLK / 1 is required to write TCNT5. */
    pc_write(PC_BASE+8,0,2);
    (void)pc_read(PC_BASE+8,2); /* Flush the uncached write before switching. */
    pc_write(PC_BASE+12,0x200,2);
    pc_write(PC_BASE+12,0x202,2); /* Now select RTC / 1; no PWM output. */
    pc_write(0x28,PC_IRQ_BITS,4);
    pc_total=pc_previous=0;
    pc_fault=pc_retries=pc_backwards=pc_wraps=pc_deferred=pc_terminal=0;
    pc_bad_a=pc_bad_b=pc_bad_previous=pc_max_delta=0;
    pc_owned=1;
    pc_write(0x14,PC_BIT,1);
    return 1;
}
static int pc_sample(uint32_t *value)
{
    uint32_t a=pc_read(PC_BASE+8,2);
    for (unsigned i=0;i<8;++i) {
        uint32_t b=pc_read(PC_BASE+8,2);
        uint32_t step=b>=a ? b-a : b+PC_PERIOD-a;
        /* The physical H1 can hold TCNT=FULL until the next RTC edge. This
         * is a valid terminal sample, not a torn read or a stopped clock. */
        if (a<=PC_TOP && b<=PC_TOP && step<=1u) { *value=b;return 1; }
        ++pc_retries;pc_bad_a=a;pc_bad_b=b;pc_bad_previous=pc_previous;a=b;
    }
    pc_fault=1;return 0;
}
static uint32_t pc_now(void)
{
    if (!pc_owned) return 0;
    if (pc_fault) return pc_total;
    uint32_t value, full;
    if (!pc_sample(&value)) return pc_total;
    full=pc_read(0x20,4)&PC_BIT;
    if (value<pc_previous && !full) {
        ++pc_backwards;pc_bad_a=pc_previous;pc_bad_b=value;pc_bad_previous=pc_previous;
        /* A stable but stale sample can occur too. Retry once; otherwise
         * reject this measurement rather than silently clamping/resetting. */
        if (!pc_sample(&value)) return pc_total;
        full=pc_read(0x20,4)&PC_BIT;
        if (value<pc_previous && !full) {
            /* Counter/flag visibility need not be simultaneous. Only near
             * FULL, hold the old timestamp for at most 64 RTC ticks (~2 ms)
             * until the flag arrives. Never invent a period without it. */
            if (pc_previous>=PC_TOP-64u && value<=64u) {
                ++pc_deferred;return pc_total;
            }
            pc_fault=2;return pc_total;
        }
    }
    if (full) {
        /* The flag may have appeared just after sampling. Re-read before
         * clearing it: FULL can be set while TCNT still equals 65535. */
        if (!pc_sample(&value)) return pc_total;
    }
    if (value==PC_TOP) {
        ++pc_terminal;
        /* Retain FULL until the counter has actually left the terminal
         * count. Extending here would invent almost an entire period. */
        full=0;
    } else if (full && value<pc_previous) {
        pc_write(0x28,PC_BIT,4);++pc_wraps;
    } else full=0; /* A FULL flag alone does not prove reset has occurred. */
    uint32_t delta=full ? value+PC_PERIOD-pc_previous : value-pc_previous;
    if (delta>pc_max_delta) pc_max_delta=delta;
    pc_total+=delta;pc_previous=value;
    return pc_total;
}
static void pc_stop(void)
{
    if (!pc_owned) return;
    pc_write(0x18,PC_BIT,1);
    pc_write(PC_BASE+12,0x200,2);
    pc_write(PC_BASE,pc_saved[0],2);
    pc_write(PC_BASE+4,pc_saved[1],2);
    pc_write(PC_BASE+12,0x201,2);
    pc_write(PC_BASE+8,pc_saved[2],2);
    (void)pc_read(PC_BASE+8,2);
    pc_write(PC_BASE+12,0x200,2);
    pc_write(PC_BASE+12,pc_saved[3],2);
    /* TCU5 raises flags on comparison writes even when counting is disabled. */
    pc_write(0x28,PC_IRQ_BITS,4);
    pc_write(0x38,PC_IRQ_BITS&~pc_mask,4);
    if (pc_stopped) pc_write(0x2c,PC_BIT,4);
    pc_owned=0;
}
#endif
