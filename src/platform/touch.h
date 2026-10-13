#ifndef H1_TOUCH_H
#define H1_TOUCH_H
#include "h1_sdk.h"
/* GUI+0x6C0: V1.41's exported calibrated pen-coordinate getter (u16*,u16*).
 * It atomically reads the ADC, applies the system's affine calibration and
 * returns LCD pixels. The cached pair alone stays stale in native game mode.
 * Validate the researched calling convention before calling unknown firmware. */
static inline int h1_touch_position(int *x, int *y)
{
    typedef void (*coordinate_fn)(uint16_t *,uint16_t *);
    const uint32_t *code=(const uint32_t *)h1_runtime_entry(
        h1_runtime_table(H1_RUNTIME_GUI_TABLE_SLOT),0x6c0u);
    if (!code || code[0]!=0x27bdffa8u || code[1]!=0xafbf0050u ||
        code[4]!=0x00a0b821u || code[5]!=0x0080b021u) return 0;
    uint16_t px=65535,py=65535;
    ((coordinate_fn)code)(&px,&py);
    *x=px;*y=py;
    return px<480 && py<272;
}
#endif
