#include "h1_sdk.h"
#include "platform/frontend.h"
#include "platform/save.h"
#include "platform/nes_video.h"
#include <assert.h>
#include <stdlib.h>
#include <string.h>
#include <libretro.h>

static const char *selected_path;
static int unterminated;
static void test_indexed_video(void)
{
    static uint8_t indices[264*240],emphasis[264*240];
    static uint32_t palette[1024],old_frame[480*272],new_frame[480*272];
    static uint16_t rgb[264*240];
    uint32_t seed=0x4740;
    for (unsigned test=0;test<12;++test) {
        unsigned height=(test%3==0?224:test%3==1?232:240),mask=test&1?63:255;
        for (unsigned i=0;i<1024;++i) { seed=seed*1664525u+1013904223u;palette[i]=(seed>>8)&65535u; }
        for (unsigned y=0;y<height;++y) {
            unsigned e=(y+test)&7u,base=e?256u+(e<<6):0u;
            for (unsigned x=0;x<264;++x) {
                seed=seed*1664525u+1013904223u;
                indices[y*264+x]=(uint8_t)(seed>>24);emphasis[y*264+x]=(uint8_t)e;
                rgb[y*264+x]=(uint16_t)palette[base+(indices[y*264+x]&(e?63u:mask))];
            }
        }
        h1_nes_video_frame frame={indices,emphasis,palette,256,height,264,mask};
        for (int scale=0;scale<H1_SCALE_COUNT;++scale) {
            for (unsigned i=0;i<480*272;++i) old_frame[i]=new_frame[i]=0x12345678u;
            scale_frame_rgb32(old_frame,rgb,264*2,scale,256,height,1);
            scale_nes_indexed_rgb32(new_frame,&frame,scale,1);
            assert(!memcmp(old_frame,new_frame,sizeof old_frame));
            /* Borders stay intact when the LCD buffer has not changed. */
            scale_frame_rgb32(old_frame,rgb,264*2,scale,256,height,0);
            scale_nes_indexed_rgb32(new_frame,&frame,scale,0);
            assert(!memcmp(old_frame,new_frame,sizeof old_frame));
        }
    }
}
static uint8_t physical_keys[45];
static int fake_game_key(unsigned code)
{
    static const uint8_t scancodes[43]={0,16,17,18,19,20,21,22,30,31,32,33,34,35,36,
        104,44,45,46,47,48,49,109,57,1,28,105,108,106,23,24,25,37,38,86,
        106,50,103,111,28,105,1,42};
    for (unsigned k=1;k<=42;++k) if (scancodes[k]==code && physical_keys[k]) return 1;
    return 0;
}
static void fake_selector(const char *directory, const char *extension, char *output)
{
    assert(!strcmp(directory, FC_ROOT) && !strcmp(extension, "nes"));
    if (unterminated) memset(output, 'x', 512);
    else if (selected_path) strcpy(output, selected_path);
}
void *h1_runtime_table(uint32_t slot) { assert(slot == H1_RUNTIME_GUI_TABLE_SLOT); return (void *)1; }
void *h1_runtime_entry(void *table, uint32_t offset)
{
    assert(table == (void *)1);
    if (offset==0x9d8u) return (void *)fake_game_key;
    assert(offset == H1_GUI_FILE_SELECTOR_OFFSET);return (void *)fake_selector;
}

static int fail_write;
h1_file *h1_fopen(const char *path, const char *mode)
{ return fopen(!strncmp(path, FC_ROOT, strlen(FC_ROOT)) ? path + strlen(FC_ROOT) : path, mode); }
int h1_fclose(h1_file *f) { return fclose(f); }
h1_size_t h1_fread(void *p, h1_size_t size, h1_size_t n, h1_file *f)
{ return (h1_size_t)fread(p, 1, (size_t)size * n, f); }
h1_size_t h1_fwrite(const void *p, h1_size_t size, h1_size_t n, h1_file *f)
{ return (h1_size_t)fwrite(p, 1, fail_write ? (size_t)size * n / 2 : (size_t)size * n, f); }
int h1_fseek(h1_file *f, int offset, int whence)
{ return fseek(f, offset, whence) ? -1 : (int)ftell(f); }
static int queued_code=-1,queued_key=-1;
int h1_event_fetch(int *code, int *key)
{ *code=queued_code;*key=queued_key;queued_code=queued_key=-1;return 0; }
int h1_blit_rgb565(int x, int y, int w, int h, const uint16_t *p)
{ (void)x; (void)y; (void)w; (void)h; (void)p; return 0; }
int h1_present_full_screen(void) { return 0; }
static void file_bytes(const char *path, const void *p, size_t n)
{ FILE *f = fopen(path, "wb"); assert(f); assert(fwrite(p, 1, n, f) == n); assert(!fclose(f)); }
static uint32_t linear_reference(const uint16_t *src,unsigned sw,unsigned sh,unsigned w,unsigned x,unsigned y)
{
    double px=(x+0.5)*sw/w-0.5,py=(y+0.5)*sh/272-0.5;
    if (px<0) px=0;
    if (py<0) py=0;
    if (px>sw-1) px=sw-1;
    if (py>sh-1) py=sh-1;
    unsigned x0=(unsigned)px,y0=(unsigned)py,x1=x0+1<sw?x0+1:x0,y1=y0+1<sh?y0+1:y0;
    unsigned wx=(unsigned)((px-x0)*256),wy=(unsigned)((py-y0)*256);
    uint32_t colors[4];
    unsigned indices[4]={y0*260+x0,y0*260+x1,y1*260+x0,y1*260+x1};
    unsigned weights[4]={(256-wx)*(256-wy),wx*(256-wy),(256-wx)*wy,wx*wy};
    for (unsigned i=0;i<4;++i) {
        uint32_t p=src[indices[i]];
        colors[i]=((p&0xf800)<<8)|((p&0x07e0)<<5)|((p&31)<<3);
    }
    uint32_t result=0;
    for (unsigned shift=0;shift<24;shift+=8) {
        unsigned sum=0;for (unsigned i=0;i<4;++i) sum+=((colors[i]>>shift)&255)*weights[i];
        result|=(sum/65536)<<shift;
    }
    return result;
}

int main(void)
{
    test_indexed_video();
    input_state input = {0};
    uint8_t header[8192] = {0}, data[131072], restore[131072];
    uint16_t *frame = calloc(480 * 272, 2), *source = malloc(260 * 240 * 2);
    save_store store, reloaded;
    FILE *f;
    unsigned x, y;
    char path[H1_ROM_PATH_MAX], directory[H1_ROM_PATH_MAX];
    assert(frame && source);
    assert(crc32_bytes("123456789", 9) == 0xcbf43926u);
    assert(crc32_update(crc32_bytes("1234",4),"56789",5)==0xcbf43926u);
    selected_path = FC_ROOT "test.nes";
    assert(h1_select_rom(FC_ROOT, path, sizeof(path)) == 1 && !strcmp(path, selected_path));
    selected_path = FC_ROOT "test.NES";
    assert(h1_select_rom(FC_ROOT, path, sizeof(path)) == 1 && !strcmp(path, selected_path));
    selected_path = FC_ROOT "test.NES";
    assert(h1_select_rom(FC_ROOT, path, sizeof(path)) == 1 && !strcmp(path, selected_path));
    selected_path = "A:\\" "\xbf\xda\xb4\xfc" ".NES";
    assert(h1_select_rom(FC_ROOT, path, sizeof(path)) == 1 && !strcmp(path, selected_path));
    selected_path = 0; assert(h1_select_rom(FC_ROOT, path, sizeof(path)) == 0 && !path[0]);
    selected_path = "A:\\other.bin"; assert(h1_select_rom(FC_ROOT, path, sizeof(path)) == -1 && !path[0]);
    unterminated = 1; assert(h1_select_rom(FC_ROOT, path, sizeof(path)) == -1 && !path[0]); unterminated = 0;
    h1_rom_directory("A:\\FC\\" "\x81\x5c" ".nes", directory, sizeof(directory));
    assert(!strcmp(directory, FC_ROOT));
    input_event(&input, 1, H1_KEY_Z); input_event(&input, 1, H1_KEY_CONFIRM);
    input_event(&input, 0, H1_KEY_Z); assert(input.mask & (1u << RETRO_DEVICE_ID_JOYPAD_A));
    input_event(&input, 0, H1_KEY_CONFIRM); assert(input.mask == 0);
    input_event(&input, 1, H1_KEY_LEFT); input_event(&input, 1, H1_KEY_X);
    assert(input.mask == ((1u << RETRO_DEVICE_ID_JOYPAD_LEFT) | (1u << RETRO_DEVICE_ID_JOYPAD_B)));
    input_event(&input, 1, H1_KEY_V); assert(input.scale == 1);
    input_event(&input, 1, H1_KEY_V); assert(input.scale == 1);
    input_event(&input, 0, H1_KEY_V); input_event(&input, 1, H1_KEY_V); assert(input.scale == 2);
    input_event(&input,0,H1_KEY_V);input_event(&input,1,H1_KEY_V);assert(input.scale==H1_SCALE_LINEAR);
    input_event(&input,0,H1_KEY_V);input_event(&input,1,H1_KEY_V);assert(input.scale==H1_SCALE_NATIVE);
    input_mapping_defaults(&input);
    assert(input_map_button(&input, RETRO_DEVICE_ID_JOYPAD_A, H1_KEY_Q));
    assert(input_map_button(&input, RETRO_DEVICE_ID_JOYPAD_A, H1_KEY_ESCAPE));
    assert(input_map_button(&input, RETRO_DEVICE_ID_JOYPAD_A, H1_KEY_Q));
    input_event(&input,1,H1_KEY_Q);assert(input.mask & (1u<<RETRO_DEVICE_ID_JOYPAD_A));
    input_event(&input,0,H1_KEY_Q);input_event(&input,1,H1_KEY_Z);assert(!input.mask);
    input_event(&input,1,31);assert(input.pause_requested);
    input_state exit_keys={0};input_mapping_defaults(&exit_keys);
    for (unsigned i=0;i<2;++i) {
        unsigned key=i?H1_KEY_BACK:H1_KEY_ESCAPE;
        assert(!input_key_reserved(key));
        input_event(&exit_keys,1,(int)key);
        assert(exit_keys.mask==(1u<<RETRO_DEVICE_ID_JOYPAD_B));
        assert(!exit_keys.pause_requested && !exit_keys.exit_requested);
        input_event(&exit_keys,1,(int)key);
        assert(exit_keys.mask==(1u<<RETRO_DEVICE_ID_JOYPAD_B));
        input_event(&exit_keys,0,(int)key);
        assert(!exit_keys.mask);
    }
    {
        input_state combo={0};
        assert(input_map_button(&combo,RETRO_DEVICE_ID_JOYPAD_RIGHT,10)); /* D */
        assert(input_map_button(&combo,RETRO_DEVICE_ID_JOYPAD_A,32)); /* K */
        physical_keys[10]=1;input_frame(&combo);
        assert(combo.mask==(1u<<RETRO_DEVICE_ID_JOYPAD_RIGHT));
        physical_keys[32]=1;input_frame(&combo);
        unsigned both=(1u<<RETRO_DEVICE_ID_JOYPAD_RIGHT)|(1u<<RETRO_DEVICE_ID_JOYPAD_A);
        assert(combo.mask==both);
        /* Firmware's spurious single-key release cannot erase the snapshot. */
        queued_code=H1_EVENT_KEY_UP;queued_key=10;input_poll(&combo);assert(combo.mask==both);
        physical_keys[32]=0;input_frame(&combo);assert(combo.mask==(1u<<RETRO_DEVICE_ID_JOYPAD_RIGHT));
        physical_keys[32]=1;input_frame(&combo);physical_keys[10]=0;input_frame(&combo);
        assert(combo.mask==(1u<<RETRO_DEVICE_ID_JOYPAD_A));
        physical_keys[32]=0;input_frame(&combo);assert(!combo.mask);
        /* Opposite press order, three buttons, and shortcut press edges. */
        physical_keys[32]=1;input_frame(&combo);physical_keys[10]=physical_keys[17]=1;input_frame(&combo);
        assert(combo.mask==(both|(1u<<RETRO_DEVICE_ID_JOYPAD_B)));
        physical_keys[31]=1;input_frame(&combo);assert(combo.pause_requested);
        combo.pause_requested=0;input_frame(&combo);assert(!combo.pause_requested);
        memset(physical_keys,0,45);input_frame(&combo);assert(!combo.mask);
        input_state aliases={0};
        physical_keys[H1_KEY_ENTER]=1;queued_code=9;queued_key=H1_KEY_ENTER;input_frame(&aliases);
        assert(aliases.mask==(1u<<RETRO_DEVICE_ID_JOYPAD_START));
        physical_keys[H1_KEY_ENTER]=0;input_frame(&aliases);
        physical_keys[H1_KEY_CONFIRM]=1;queued_code=9;queued_key=H1_KEY_CONFIRM;input_frame(&aliases);
        assert(aliases.mask==(1u<<RETRO_DEVICE_ID_JOYPAD_A));
        memset(physical_keys,0,45);input_frame(&aliases);assert(!aliases.mask);
        /* Menus consume their own key-down event. The native query remains
         * held on return, including Confirm's shared Enter/Start scancode. */
        const unsigned menu_keys[]={H1_KEY_CONFIRM,H1_KEY_ENTER,H1_KEY_Z,31,H1_KEY_ESCAPE,H1_KEY_BACK};
        for (unsigned i=0;i<sizeof(menu_keys)/sizeof(menu_keys[0]);++i) {
            unsigned key=menu_keys[i];
            physical_keys[key]=1;
            input_discard_held(&aliases);
            aliases.pause_requested=0;
            for (unsigned frame=0;frame<5;++frame) {
                input_frame(&aliases);
                assert(!aliases.mask && !aliases.pause_requested && !aliases.exit_requested);
            }
            physical_keys[key]=0;input_frame(&aliases);
            physical_keys[key]=1;queued_code=H1_EVENT_KEY_DOWN;queued_key=(int)key;
            input_frame(&aliases);
            if (key==31) {
                assert(aliases.pause_requested && !aliases.exit_requested);
            } else if (key==H1_KEY_ESCAPE || key==H1_KEY_BACK) {
                assert(!aliases.pause_requested && !aliases.exit_requested);
                assert(aliases.mask==(1u<<RETRO_DEVICE_ID_JOYPAD_B));
                assert(aliases.held[key]);
                assert(!aliases.held[key==H1_KEY_ESCAPE?H1_KEY_BACK:H1_KEY_ESCAPE]);
            } else assert(aliases.mask==(1u<<(key==H1_KEY_ENTER?RETRO_DEVICE_ID_JOYPAD_START:RETRO_DEVICE_ID_JOYPAD_A)));
            memset(physical_keys,0,45);input_frame(&aliases);
            aliases.pause_requested=0;
        }
        /* The shared firmware scancode must preserve each event's identity
         * when Escape and Back have different user-selected game bindings. */
        input_state custom={0};input_mapping_defaults(&custom);
        assert(input_map_button(&custom,RETRO_DEVICE_ID_JOYPAD_A,H1_KEY_ESCAPE));
        assert(input_map_button(&custom,RETRO_DEVICE_ID_JOYPAD_B,H1_KEY_BACK));
        assert(!input_map_button(&custom,RETRO_DEVICE_ID_JOYPAD_A,H1_KEY_P));
        for (unsigned i=0;i<2;++i) {
            unsigned key=i?H1_KEY_BACK:H1_KEY_ESCAPE;
            physical_keys[key]=1;queued_code=H1_EVENT_KEY_DOWN;queued_key=(int)key;
            input_frame(&custom);
            assert(custom.mask==(1u<<(i?RETRO_DEVICE_ID_JOYPAD_B:RETRO_DEVICE_ID_JOYPAD_A)));
            assert(!custom.pause_requested && !custom.exit_requested);
            memset(physical_keys,0,45);input_frame(&custom);assert(!custom.mask);
        }
        /* A touch return with held controls also waits for release. A later
         * fresh direction+A combination works, with no stale alias owner. */
        physical_keys[H1_KEY_LEFT]=physical_keys[H1_KEY_Z]=1;
        input_discard_held(&aliases);input_frame(&aliases);assert(!aliases.mask);
        memset(physical_keys,0,45);input_frame(&aliases);
        input_discard_held(&aliases);input_frame(&aliases);assert(!aliases.mask);
        physical_keys[H1_KEY_RIGHT]=physical_keys[H1_KEY_CONFIRM]=1;
        queued_code=H1_EVENT_KEY_DOWN;queued_key=H1_KEY_CONFIRM;input_frame(&aliases);
        assert(aliases.mask==((1u<<RETRO_DEVICE_ID_JOYPAD_RIGHT)|(1u<<RETRO_DEVICE_ID_JOYPAD_A)));
        physical_keys[H1_KEY_CONFIRM]=0;input_frame(&aliases);
        assert(aliases.mask==(1u<<RETRO_DEVICE_ID_JOYPAD_RIGHT));
        memset(physical_keys,0,45);input_frame(&aliases);assert(!aliases.mask);
    }
    for (y = 0; y < 240; ++y) for (x = 0; x < 260; ++x) source[y * 260 + x] = (uint16_t)(y * 240 + x);
    scale_frame(frame, source, 260 * 2, 1);
    assert(frame[95] == source[0] && frame[271 * 480 + 384] == source[239 * 260 + 255]);
    assert(frame[94] == 0 && frame[385] == 0);
    memset(frame, 0, 480 * 272 * 2); scale_frame(frame, source, 260 * 2, 0);
    assert(frame[16 * 480 + 112] == source[0]);
    assert(frame[255 * 480 + 367] == source[239 * 260 + 255]);
    {
        uint32_t *direct=malloc(480*272*4);
        assert(direct);
        for (unsigned kind=0;kind<2;++kind) for (int scaled=0;scaled<3;++scaled) {
            unsigned sw=256,sh=kind?224:240;
            memset(frame,0,480*272*2);
            scale_frame_dimensions(frame,source,260*2,scaled,sw,sh);
            memset(direct,255,480*272*4);
            scale_frame_rgb32(direct,source,260*2,scaled,sw,sh,1);
            for (unsigned i=0;i<480*272;++i) {
                uint32_t p=frame[i];
                assert(direct[i]==(((p&0xf800)<<8)|((p&0x07e0)<<5)|((p&31)<<3)));
            }
            /* A fresh frame must rebuild the RAM row cache, while clear=0
             * preserves borders (and the caller's overlay outside the image). */
            if (scaled!=2) { frame[0]=0x1357;direct[0]=((0x1357&0xf800)<<8)|((0x1357&0x07e0)<<5)|((0x1357&31)<<3); }
            for (unsigned i=0;i<260*240;++i) source[i]^=0xf81f;
            scale_frame_dimensions(frame,source,260*2,scaled,sw,sh);
            scale_frame_rgb32(direct,source,260*2,scaled,sw,sh,0);
            for (unsigned i=0;i<480*272;++i) {
                uint32_t p=frame[i];
                assert(direct[i]==(((p&0xf800)<<8)|((p&0x07e0)<<5)|((p&31)<<3)));
            }
        }
        for (unsigned kind=0;kind<2;++kind) for (unsigned pattern=0;pattern<4;++pattern) {
            unsigned sw=256,sh=kind?224:240,w=sw*272/sh,ox=(480-w)/2,intermediate=0;
            for (unsigned y=0;y<240;++y) for (unsigned x=0;x<260;++x)
                source[y*260+x]=x>=sw ? 0x5a5a :
                    pattern==0 ? ((x^y)&1?0xffff:0) :
                    pattern==1 ? (uint16_t)(x*977+y*4057) :
                    pattern==2 ? (uint16_t)(y*4057) :
                    (x>sw/3 && x<sw*2/3 && y>sh/3 && y<sh*2/3 ? (uint16_t)(x*977+y*4057) : 31);
            scale_frame_rgb32(direct,source,260*2,H1_SCALE_LINEAR,sw,sh,1);
            for (unsigned y=0;y<272;++y) for (unsigned x=0;x<480;++x) {
                uint32_t pixel=direct[y*480+x];
                if (x<ox || x>=ox+w) { assert(pixel==0);continue; }
                uint32_t reference=linear_reference(source,sw,sh,w,x-ox,y);
                for (unsigned shift=0;shift<24;shift+=8)
                    assert(abs((int)((pixel>>shift)&255)-(int)((reference>>shift)&255))<=1);
                if (pixel && pixel!=0xf8fcf8) ++intermediate;
            }
            assert(intermediate);
            if (!pattern) assert(direct[271*480+ox+w-1]==0);
            for (unsigned i=0;i<260*240;++i) source[i]=0x07e0;
            direct[0]=0x123456;
            scale_frame_rgb32(direct,source,260*2,H1_SCALE_LINEAR,sw,sh,0);
            assert(direct[0]==0x123456);
            for (unsigned y=0;y<272;++y) for (unsigned x=ox;x<ox+w;++x) assert(direct[y*480+x]==0x00fc00);
        }
        free(direct);
    }
    file_bytes("test.nes", header, sizeof(header)); memset(data, 255, sizeof(data));
    assert(save_load(&store, FC_ROOT "test.nes", data, sizeof(data)) == 0);
    assert(save_checkpoint(&store, data, sizeof(data)) == 0);
    data[0] = 1; assert(save_checkpoint(&store, data, sizeof(data)) == 1 && store.slot == 0);
    data[0] = 2; assert(save_checkpoint(&store, data, sizeof(data)) == 1 && store.slot == 1);
    memset(restore, 255, sizeof(restore));
    assert(save_load(&reloaded, FC_ROOT "test.nes", restore, sizeof(restore)) == 1 && restore[0] == 2);
    f = fopen("test.nes.s1", "r+b"); assert(f); fseek(f, 28, SEEK_SET); fputc(3, f); fclose(f);
    assert(save_load(&reloaded, FC_ROOT "test.nes", restore, sizeof(restore)) == 1 && restore[0] == 1);
    fail_write = 1; restore[0] = 4;
    assert(save_checkpoint(&reloaded, restore, sizeof(restore)) == -1 && reloaded.slot == 0);
    fail_write = 0;
    assert(save_load(&reloaded, FC_ROOT "test.nes", restore, sizeof(restore)) == 1 && restore[0] == 1);
    header[7000]=1; file_bytes("test.nes",header,sizeof header);
    assert(save_load(&reloaded, FC_ROOT "test.nes", restore, sizeof restore)==-1);
    header[7000]=0; file_bytes("test.nes",header,sizeof header);
    assert(save_load(&reloaded, FC_ROOT "test.nes", restore, sizeof restore)==1);
    file_bytes("test.nes.s0", "bad", 3);
    assert(save_load(&reloaded, FC_ROOT "test.nes", restore, sizeof(restore)) == -1);
    h1_video_release();
    free(frame); free(source);
    puts("PASS: native selector, cancellation, invalid output, GBK paths, CRC, keys, scaling, A/B save, corruption, short writes");
    return 0;
}
