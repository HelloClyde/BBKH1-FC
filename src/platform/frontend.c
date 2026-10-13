#include "h1_sdk.h"
#include "frontend.h"
#include "diagnostics.h"
#include "game_input.h"
#include <string.h>
#include <libretro.h>

uint32_t crc32_update(uint32_t previous, const void *data, size_t n)
{
    const uint8_t *p = (const uint8_t *)data;
    static uint32_t table[256];
    static int ready;
    if (!ready) {
        for (unsigned i=0;i<256;++i) {
            uint32_t value=i;
            for (unsigned b=0;b<8;++b) value=(value>>1)^(0xedb88320u&(0u-(value&1u)));
            table[i]=value;
        }
        ready=1;
    }
    uint32_t c = ~previous;
    while (n--) {
        c = (c >> 8) ^ table[(c ^ *p++) & 255u];
    }
    return ~c;
}
uint32_t crc32_bytes(const void *data, size_t n)
{ return crc32_update(0, data, n); }
static int key_button(int key)
{
    switch (key) {
    case H1_KEY_LEFT: return RETRO_DEVICE_ID_JOYPAD_LEFT;
    case H1_KEY_RIGHT: return RETRO_DEVICE_ID_JOYPAD_RIGHT;
    case H1_KEY_UP: return RETRO_DEVICE_ID_JOYPAD_UP;
    case H1_KEY_DOWN: return RETRO_DEVICE_ID_JOYPAD_DOWN;
    case H1_KEY_Z: case H1_KEY_CONFIRM: return RETRO_DEVICE_ID_JOYPAD_A;
    case H1_KEY_X: return RETRO_DEVICE_ID_JOYPAD_B;
    case H1_KEY_ENTER: return RETRO_DEVICE_ID_JOYPAD_START;
    case H1_KEY_SPACE: return RETRO_DEVICE_ID_JOYPAD_SELECT;
    default: return -1;
    }
}
int input_key_reserved(unsigned key)
{
    return !key || key > 44 || key == H1_KEY_ESCAPE || key == H1_KEY_BACK ||
        key == H1_KEY_M || key == H1_KEY_V || key == H1_KEY_F || key == 31 || key == 44;
}
void input_mapping_defaults(input_state *s)
{
    memset(s->mapping, 0, sizeof s->mapping);
    for (unsigned key=1; key<=44; ++key) {
        int button=key_button(key);
        if (button>=0) s->mapping[key]=(uint8_t)(button+1);
    }
    s->mapping_ready=1;
}
int input_map_button(input_state *s, unsigned id, unsigned key)
{
    if (id>=16 || input_key_reserved(key)) return 0;
    if (!s->mapping_ready) input_mapping_defaults(s);
    for (unsigned k=1;k<=44;++k) if (s->mapping[k]==id+1) s->mapping[k]=0;
    s->mapping[key]=(uint8_t)(id+1);
    memset(s->held,0,sizeof s->held);s->mask=0;
    return 1;
}
void input_event(input_state *s, int pressed, int key)
{
    int i, edge;
    if (key < 1 || key > 44) return;
    edge = pressed && !s->held[key];
    s->held[key] = pressed != 0;
    s->mask = 0;
    /* Recompute to preserve A when one of its two physical aliases releases. */
    for (i = 1; i <= 44; ++i) {
        int b = s->mapping_ready ? (int)s->mapping[i]-1 : key_button(i);
        if (s->held[i] && b >= 0) s->mask |= 1u << b;
    }
    if (!edge) return;
    if (key == H1_KEY_ESCAPE || key == H1_KEY_BACK) s->exit_requested = 1;
    if (key == 31) s->pause_requested = 1;
    if (key == H1_KEY_M) s->menu_requested = 1;
    if (key == H1_KEY_V) { s->scale = (s->scale + 1) % H1_SCALE_COUNT; s->scale_changed = 1; }
    if (key == H1_KEY_F) { s->skip = (s->skip + 1) % 3; s->skip_changed = 1; }
}
static int game_input_active;
static uint8_t alias_owner[256];
static uint8_t wait_for_release[256];
void input_discard_held(input_state *s)
{
    uint8_t sampled[256]={0};
    memset(alias_owner,0,sizeof alias_owner);
    memset(wait_for_release,0,sizeof wait_for_release);
    /* A menu consumes the key-down event before returning, while the native
     * game query still reports the physical key held. Suppress that complete
     * native alias group until release instead of interpreting Confirm as
     * Enter/Start or passing the menu activation through as an in-game A. */
    for (unsigned key=1;key<=42;++key) {
        unsigned code=h1_game_key_code(key);
        if (!sampled[code]) {
            wait_for_release[code]=(uint8_t)h1_game_code_down(code);
            sampled[code]=1;
        }
    }
    memset(s->held,0,sizeof s->held);s->mask=0;
}
void input_frame(input_state *s)
{
    uint8_t sampled[256]={0},down_codes[256]={0};
    if (!s->mapping_ready) input_mapping_defaults(s);
    game_input_active=1;
    input_poll(s);
    /* Query assigned keys and frontend controls once per core frame. Never
     * rescan them on each iteration of the audio pacing busy loop. */
    for (unsigned key=1;key<=42;++key) {
        if (!s->mapping[key] && !input_key_reserved(key)) continue;
        unsigned code=h1_game_key_code(key);
        if (!sampled[code]) {
            down_codes[code]=(uint8_t)h1_game_code_down(code);sampled[code]=1;
            if (!down_codes[code]) { alias_owner[code]=0;wait_for_release[code]=0; }
        }
        int down=down_codes[code] && !wait_for_release[code];
        /* Enter/Confirm and the Alt/direction pairs share native scancodes.
         * Use the queue's physical identity for these aliases, while the
         * native query remains authoritative for whether the group is held. */
        if (code==28 || code==105 || code==106)
            down=down && (alias_owner[code] ? alias_owner[code]==key :
                key==(code==28 ? H1_KEY_ENTER : code==105 ? H1_KEY_LEFT : H1_KEY_RIGHT));
        if (s->held[key]!=down) input_event(s,down,(int)key);
    }
}
void input_poll(input_state *s)
{
    unsigned n;
    int code, key;
    for (n = 0; n < 128; ++n) {
        code = key = -1;
        h1_event_fetch(&code, &key);
        if (code == -1 && key == -1) break;
        if (code == 11) { s->pause_requested=1; break; }
        if (code==H1_EVENT_KEY_DOWN && key>=1 && key<=42) {
            unsigned native=h1_game_key_code((unsigned)key);
            if (native==28 || native==105 || native==106) alias_owner[native]=(uint8_t)key;
        }
        /* GUI's single-key queue must not overwrite the native game query. */
        if (game_input_active && key>=1 && key<=42) continue;
        if (code == H1_EVENT_KEY_DOWN || code == H1_EVENT_KEY_UP)
            input_event(s, code == H1_EVENT_KEY_DOWN, key);
    }
}
/* Keep red/blue in two byte lanes and green in the low byte while walking
 * source intervals. Store packed RGB888 rows for the MXU vertical kernel.
 * Unsigned arithmetic wraps modulo 2^32; the final weighted sums fit, so
 * the subtraction form is bit-exact, including the old truncation. */
typedef struct { uint32_t rb,g; } linear_pixel;
static int linear_mxu_state;
#ifdef __mips__
extern void h1_linear_vertical_mxu(uint32_t *,const uint32_t *,const uint32_t *,unsigned,unsigned);
#endif
static uint32_t blend_packed(uint32_t a,uint32_t b,unsigned f)
{
    uint32_t inverse=256-f;
    return ((((a&0xff00ffu)*inverse+(b&0xff00ffu)*f)>>8)&0xff00ffu) |
           ((((a&0xff00u)*inverse+(b&0xff00u)*f)>>8)&0xff00u);
}
int h1_linear_init(void)
{
    if (linear_mxu_state) return linear_mxu_state>0;
    linear_mxu_state=-1;
#ifdef __mips__
    uint32_t a[64] __attribute__((aligned(32))),b[64] __attribute__((aligned(32))),out[64] __attribute__((aligned(32)));
    for (unsigned i=0;i<64;++i) {
        a[i]=(i*0x9717efu)&0xffffffu;b[i]=(~(i*0x35436bu))&0xffffffu;
    }
    a[0]=0;b[0]=0xffffffu;a[1]=0xffffffu;b[1]=0;
    a[2]=0xff00ffu;b[2]=0x00ff00u;
    for (unsigned f=1;f<256;++f) {
        h1_linear_vertical_mxu(out,a,b,64,f);
        for (unsigned i=0;i<64;++i) if (out[i]!=blend_packed(a[i],b[i],f)) return 0;
    }
    linear_mxu_state=1;
#endif
    return linear_mxu_state>0;
}
static linear_pixel expand_linear(unsigned p)
{
    linear_pixel v={((p&0xf800u)<<8)|((p&31u)<<3),(p&0x07e0u)>>3};
    return v;
}
/* Walk source intervals: adjacent output pixels share the same endpoints.
 * Keep endpoints, differences and shifted bases in registers for the group. */
static int __attribute__((noinline)) linear_horizontal(uint32_t *dst,
    const uint16_t *row,unsigned width,const uint16_t *end,const uint16_t *fraction)
{
    linear_pixel a=expand_linear(row[0]);
    unsigned i=1;
    while (i<width && row[i]==row[0]) ++i;
    if (i==width) {
        uint32_t color=a.rb|(a.g<<8);
        for (unsigned x=0;x<end[width-1];++x) dst[x]=color;
        return 1;
    }
    unsigned x=0;
    for (unsigned left=0;left<width;++left) {
        linear_pixel b=left+1<width ? expand_linear(row[left+1]) : a;
        uint32_t rb=a.rb<<8,g=a.g<<8,drb=b.rb-a.rb,dg=b.g-a.g;
        if (!(drb|dg)) {
            uint32_t color=a.rb|(a.g<<8);
            while (x<end[left]) dst[x++]=color;
            a=b;continue;
        }
        while (x<end[left]) {
            unsigned f=fraction[x];
            dst[x]=(((rb+drb*f)>>8)&0xff00ffu)|((g+dg*f)&0xff00u);
            ++x;
        }
        a=b;
    }
    return 0;
}
static void linear_axis(uint16_t *base,uint16_t *fraction,unsigned source,unsigned output)
{
    for (unsigned i=0;i<output;++i) {
        int position=(int)((2*i+1)*source*128/output)-128;
        if (position<0) position=0;
        if (position>(int)((source-1)*256)) position=(int)((source-1)*256);
        base[i]=(uint16_t)((unsigned)position>>8);fraction[i]=(uint16_t)(position&255);
    }
}
static void scale_linear_rgb32(uint32_t *out,const void *data,size_t pitch,
                              unsigned width,unsigned height,unsigned w)
{
    static uint16_t xbase[480],xend[480],xfrac[480],ybase[272],yfrac[272];
    static unsigned map_width,map_source,map_height;
    static uint32_t horizontal[2][480] __attribute__((aligned(32)));
    static uint32_t mixed[480] __attribute__((aligned(32)));
    unsigned tags[2]={~0u,~0u},solid[2]={0,0},ox=(480-w)/2;
    if (!linear_mxu_state) h1_linear_init();
    if (map_width!=w || map_source!=width || map_height!=height) {
        linear_axis(xbase,xfrac,width,w);linear_axis(ybase,yfrac,height,272);
        memset(xend,0,sizeof xend);
        for (unsigned x=0;x<w;++x) xend[xbase[x]]=(uint16_t)(x+1);
        map_width=w;map_source=width;map_height=height;
    }
    for (unsigned y=0;y<272;++y) {
        unsigned top=ybase[y],bottom=top+1<height?top+1:top;
        unsigned a=top&1,b=bottom&1;
        for (unsigned n=top;n<=bottom;++n) if (tags[n&1]!=n) {
            const uint16_t *row=(const uint16_t *)((const uint8_t *)data+n*pitch);
            solid[n&1]=(unsigned)linear_horizontal(horizontal[n&1],row,width,xend,xfrac);
            tags[n&1]=n;
        }
        uint32_t *dst=out+y*480+ox;
        unsigned f=yfrac[y];
        if (!f || top==bottom) { memcpy(dst,horizontal[a],w*4);continue; }
        if (solid[a] && solid[b]) {
            uint32_t color=blend_packed(horizontal[a][0],horizontal[b][0],f);
            for (unsigned x=0;x<w;++x) mixed[x]=color;
            memcpy(dst,mixed,w*4);
            continue;
        }
#ifdef __mips__
        if (linear_mxu_state>0) {
            h1_linear_vertical_mxu(mixed,horizontal[a],horizontal[b],w,f);
            memcpy(dst,mixed,w*4);continue;
        }
#endif
        unsigned x=0;
        for (;x+3<w;x+=4) {
            mixed[x]=blend_packed(horizontal[a][x],horizontal[b][x],f);
            mixed[x+1]=blend_packed(horizontal[a][x+1],horizontal[b][x+1],f);
            mixed[x+2]=blend_packed(horizontal[a][x+2],horizontal[b][x+2],f);
            mixed[x+3]=blend_packed(horizontal[a][x+3],horizontal[b][x+3],f);
        }
        for (;x<w;++x) {
            mixed[x]=blend_packed(horizontal[a][x],horizontal[b][x],f);
        }
        memcpy(dst,mixed,w*4);
    }
}
void scale_frame_rgb32(uint32_t *out, const void *data, size_t pitch, int scale, unsigned width, unsigned height, int clear)
{
    static uint32_t colors[65536];
    static uint32_t scaled_row[480] __attribute__((aligned(32)));
    static uint16_t columns[480];
    static unsigned ready, map_width, map_source;
    unsigned w=scale==2?480:scale?width*272/height:width,h=scale?272:height;
    unsigned ox=(480-w)/2,oy=(272-h)/2,previous=~0u;
    if (clear) memset(out,0,480*272*4);
    if (scale==H1_SCALE_LINEAR) { scale_linear_rgb32(out,data,pitch,width,height,w);return; }
    if (!ready) {
        for (unsigned p=0;p<65536;++p) colors[p]=((p&0xf800u)<<8)|((p&0x07e0u)<<5)|((p&0x001fu)<<3);
        ready=1;
    }
    if (map_width!=w || map_source!=width) {
        for (unsigned x=0;x<w;++x) columns[x]=(uint16_t)(x*width/w);
        map_width=w;map_source=width;
    }
    for (unsigned y=0;y<h;++y) {
        unsigned sy=y*height/h;
        const uint16_t *row=(const uint16_t *)((const uint8_t *)data+sy*pitch);
        uint32_t *dst=out+(oy+y)*480+ox;
        /* The native LCD buffer is uncached. Build each distinct source row
         * in cached RAM and copy it with the unrolled word memcpy; never read
         * the framebuffer back to duplicate a vertically scaled row. */
        if (sy!=previous)
            for (unsigned x=0;x<w;++x) scaled_row[x]=colors[row[columns[x]]];
        memcpy(dst,scaled_row,w*4);
        previous=sy;
    }
}
void scale_frame(uint16_t *out, const void *data, size_t pitch, int scale)
{
    scale_frame_dimensions(out, data, pitch, scale, 256, 240);
}
void scale_frame_dimensions(uint16_t *out, const void *data, size_t pitch, int scale, unsigned width, unsigned height)
{
    unsigned y, x, w = scale == 2 ? 480 : scale ? width * 272 / height : width, h = scale ? 272 : height;
    unsigned ox = (480 - w) / 2, oy = (272 - h) / 2;
    static uint16_t columns[480];
    static unsigned map_width, map_source;
    unsigned previous = ~0u;
    if (map_width != w || map_source != width) {
        for (x = 0; x < w; ++x) columns[x] = (uint16_t)(x * width / w);
        map_width = w; map_source = width;
    }
    for (y = 0; y < h; ++y) {
        unsigned source_y = y * height / h;
        const uint16_t *row = (const uint16_t *)((const uint8_t *)data + source_y * pitch);
        uint16_t *destination = out + (oy + y) * 480 + ox;
        if (!scale) memcpy(destination, row, w * sizeof(*row));
        else if (source_y == previous) memcpy(destination, destination - 480, w * sizeof(*row));
        else for (x = 0; x < w; ++x) destination[x] = row[columns[x]];
        previous = source_y;
    }
}
