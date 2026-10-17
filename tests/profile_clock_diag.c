/* Deterministic hardware-register model: expected elapsed ticks come from an
 * independent 64-bit timeline, not from the reader's extension algorithm. */
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#define H1_PROFILE_TCU_TEST 1
#ifndef H1_DIAGNOSTIC
#define H1_DIAGNOSTIC 1
#endif
#include "../src/platform/profile_clock.h"

static uint32_t regs[64], original[64], writes;
static uint64_t ticks, counter_base, next_full;
static uint32_t rtc_epoch=100, rtc_phase, forced_counter, model_period=65536u;
static int counter_forced, no_full;
static uint32_t samples[32], rtc_samples[32];
static unsigned sample_count,sample_at,rtc_count,rtc_at;
static unsigned cases,rtc_read_advance;

static int running(void) {
    return (regs[0x10/4]&PC_BIT) && (regs[(PC_BASE+12)/4]&7u)==2u;
}
uint32_t h1_wall_clock(void) {
    ticks+=rtc_read_advance;
    if (rtc_at<rtc_count) return rtc_samples[rtc_at++];
    return rtc_epoch+(uint32_t)((ticks+rtc_phase)/32768u);
}
uint32_t pc_read(unsigned offset,unsigned size) {
    if (offset>=PC_BASE && offset<PC_BASE+16) assert(!(regs[0x1c/4]&PC_BIT));
    assert(size==(offset>=PC_BASE ? 2u : offset==0x10 ? 1u : 4u));
    if (running() && ticks-counter_base>=next_full) {
        if (!no_full) regs[0x20/4]|=PC_BIT;
        next_full=((ticks-counter_base+1)/model_period+1)*model_period-1;
    }
    if (offset==PC_BASE+8) {
        if (sample_at<sample_count) return samples[sample_at++];
        if (counter_forced) return forced_counter;
        if (running()) return (uint32_t)((ticks-counter_base)%model_period);
    }
    return regs[offset/4];
}
void pc_write(unsigned offset,uint32_t value,unsigned size) {
    ++writes;
    if (offset>=PC_BASE && offset<PC_BASE+16) {
        assert(size==2 && !(regs[0x1c/4]&PC_BIT));
        uint32_t clock=regs[(PC_BASE+12)/4];
        if (offset==PC_BASE || offset==PC_BASE+4) assert((clock&7)==0);
        if (offset==PC_BASE+8) assert((clock&0x3f)==1);
        regs[offset/4]=value;
        if (offset==PC_BASE+8) { counter_base=ticks-value;next_full=model_period-1; }
    } else {
        assert(value==PC_BIT || (value&~PC_IRQ_BITS)==0);
        switch (offset) {
        case 0x14: assert(size==1);counter_base=ticks-regs[(PC_BASE+8)/4];regs[0x10/4]|=value;break;
        case 0x18: assert(size==1);regs[0x10/4]&=~value;break;
        case 0x28: regs[0x20/4]&=~value;break;
        case 0x2c: regs[0x1c/4]|=value;break;
        case 0x3c: regs[0x1c/4]&=~value;break;
        case 0x34: regs[0x30/4]|=value;break;
        case 0x38: regs[0x30/4]&=~value;break;
        default: assert(0);
        }
    }
}
static void reset(unsigned phase) {
    if (pc_owned) pc_stop();
    memset(regs,0,sizeof regs);
    regs[0x10/4]=7;regs[0x1c/4]=0xfff8;
    regs[0x30/4]=0xa5a55a5a;regs[0x20/4]=0x10001;
    regs[PC_BASE/4]=4000;regs[PC_BASE/4+1]=2000;
    regs[PC_BASE/4+2]=123;regs[PC_BASE/4+3]=4;
    memcpy(original,regs,sizeof regs);
    ticks=0;rtc_epoch=100;rtc_phase=phase;rtc_read_advance=0;model_period=65536u;
    counter_forced=no_full=0;sample_count=sample_at=rtc_count=rtc_at=0;
    assert(pc_start() && pc_owned);
    assert(pc_now()==0 && !pc_fault);
}
static void restore(void) {
    counter_forced=0;sample_count=sample_at=rtc_count=rtc_at=0;
    pc_stop();assert(!pc_owned && pc_now()==0);
    assert(!memcmp(original,regs,sizeof regs));++cases;
}
static void at(uint64_t t) {
    ticks=t;assert(pc_now()==(uint32_t)t && !pc_fault);
}
int main(void) {
#if !H1_DIAGNOSTIC
    reset(0);no_full=1;at(65464);ticks=65550;
    assert(pc_now()==65464 && pc_fault==2);
    regs[0x20/4]|=PC_BIT;
    assert(pc_now()==65464 && pc_fault==2);
    restore();
    puts("PASS legacy: retained 65464 -> 14 without FULL fault=2; later FULL cannot resurrect failed timing");
#else
    /* Old failing trace is accepted only with a coherent independent RTC. */
    reset(0);no_full=1;at(65464);at(65550);
    assert(pc_wraps==1 && pc_missing_full==1 && !pc_deferred);
    regs[0x20/4]|=PC_BIT;at(65551);
    assert(pc_wraps==1 && pc_flag_only==1);restore();
    /* Every initial RTC phase and both late/absent FULL flags. */
    for (unsigned phase=0;phase<32768;++phase) {
        reset(phase);no_full=1;
        at(16000);at(32000);at(48000);at(65464);at(65550);
        at(80000);at(100001);at(120000);at(131080);restore();
    }
    /* Terminal plateau, early flag, and read pair straddling wrap. */
    reset(0);at(65534);regs[0x20/4]|=PC_BIT;
    at(65534);assert(!pc_wraps);at(65535);
    for (unsigned i=0;i<32;++i) at(65535);
    samples[0]=65535;samples[1]=0;sample_count=2;sample_at=0;
    at(65536);assert(pc_wraps==1 && pc_terminal==33);restore();
    /* Torn and stale samples may recover; persistent ones fail closed. */
    reset(127);at(1000);ticks=1002;
    samples[0]=40960;samples[1]=1002;samples[2]=1002;sample_count=3;
    assert(pc_now()==1002 && pc_retries==1 && !pc_fault);
    samples[0]=1001;samples[1]=1001;sample_count=2;sample_at=0;ticks=1003;
    assert(pc_now()==1003 && pc_backwards==1 && !pc_fault);
    counter_forced=1;forced_counter=1002;
    assert(pc_now()==1003 && pc_fault==2);restore();
    reset(0);
    for (unsigned i=0;i<9;++i) samples[i]=i*10;
    sample_count=9;assert(pc_now()==0 && pc_fault==1 && pc_retries==8);restore();
    /* No FULL bit can make an incoherent backwards sample valid. */
    reset(0);at(1000);counter_forced=1;forced_counter=999;regs[0x20/4]|=PC_BIT;
    assert(pc_now()==1000 && pc_fault==2);restore();
    /* Gaps lose period information, even if TCNT appears to move forward. */
    for (unsigned seconds=2;seconds<12;++seconds) {
        reset(0);at(1000);ticks+=seconds*32768u;
        assert(pc_now()==1000 && pc_fault==4);restore();
    }
    /* RTC moves backwards/unavailable or never brackets consistently. */
    reset(0);rtc_epoch=99;assert(pc_now()==0 && pc_fault==3);restore();
    reset(0);rtc_epoch=0;assert(pc_now()==0 && pc_fault==3);restore();
    reset(0);ticks=32768;rtc_samples[0]=100;rtc_samples[1]=101;rtc_count=2;
    assert(pc_now()==32768 && !pc_fault && pc_rtc_retries==1);restore();
    reset(0);rtc_count=16;
    for (unsigned i=0;i<8;++i) { rtc_samples[i*2]=100;rtc_samples[i*2+1]=101; }
    assert(pc_now()==0 && pc_fault==5 && pc_rtc_retries==8);restore();
    /* Two RTC domains which advance at different rates eventually reject. */
    reset(0);at(32767);rtc_epoch=102;
    assert(pc_now()==32767 && pc_fault==4);restore();
    reset(0);at(32767);counter_forced=1;forced_counter=32767;ticks=65536;
    assert(pc_now()==32767 && pc_fault==4);restore();
    /* Existing QEMU uses %65535, whereas the H1 terminal plateau requires
     * 65536. Such a model must not be silently made consistent with the RTC. */
    reset(0);model_period=65535u;at(32767);at(32768);at(65534);ticks=65535;
    assert(pc_now()==65534 && pc_fault==2);restore();
    /* Full calibration lifecycle, with RTC edges occurring during reads.
     * The model deliberately charges 1 tick per RTC read, so this also checks
     * that instrumentation time remains visible instead of being subtracted. */
    reset(7301);rtc_read_advance=1;no_full=1;
    uint32_t edge=h1_wall_clock();
    while (h1_wall_clock()==edge) { (void)pc_now();assert(!pc_fault); }
    uint32_t begin=pc_now(),start_rtc=h1_wall_clock();
    while (h1_wall_clock()==start_rtc) { (void)pc_now();assert(!pc_fault); }
    uint32_t end=pc_now();
    assert(end-begin>=32760 && end-begin<=32776 && !pc_fault);
    rtc_read_advance=0;restore();
    ticks+=5u*32768u;assert(pc_start() && pc_now()==0 && !pc_fault);
    ticks+=548;assert(pc_now()==548 && !pc_fault);restore();
    /* Long continuous run crosses both TCNT and public uint32 total wrap. */
    reset(32123);no_full=1;
    for (uint64_t t=0;t<UINT64_C(4295100000);t+=1000) at(t);
    assert(pc_wraps>65536);restore();
    /* Pause restores exact registers; resume anchors a new zeroed epoch. */
    reset(317);at(50000);restore();ticks+=12u*32768u;
    assert(pc_start() && pc_now()==0 && !pc_fault);
    uint64_t resumed=ticks;
    for (unsigned i=1;i<=300;++i) { ticks=resumed+i*548u;assert(pc_now()==i*548u && !pc_fault); }
    restore();
    /* Busy enabled/PWM/IRQ channel rejection preserves every register. */
    reset(0);restore();regs[0x10/4]|=PC_BIT;writes=0;
    assert(!pc_start() && writes==0);
    regs[0x10/4]&=~PC_BIT;regs[0x20/4]|=PC_BIT;assert(!pc_start() && writes==0);
    regs[0x20/4]&=~PC_BIT;regs[(PC_BASE+12)/4]=0x80;
    uint32_t busy[64];memcpy(busy,regs,sizeof busy);
    assert(!pc_start() && !memcmp(busy,regs,sizeof busy));
    printf("PASS diagnostic: %u scenarios; all 32768 RTC phases; absent/early/late FULL; 4.29M continuous observations; tear/stale/gap/RTC fault; restore/resume\n",cases);
#endif
    return 0;
}
