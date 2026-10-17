#include "menu.h"
#include "state.h"
#include "touch.h"
#include "game_video.h"
#include "diagnostics.h"
#include "menu_font.h"
#include "audio.h"
#include <string.h>
#include <stdio.h>

/* Keep the v1 payload size. v3 releases Escape/Back for game bindings. */
typedef struct { uint32_t magic,version,scale,skip,sound; uint8_t mapping[45],volume,padding[2]; } config_data;
static void config_pack(config_data *d,const h1_config *c,const input_state *s)
{
    memset(d,0,sizeof *d);d->magic=0x31474643;d->version=3;d->volume=c->volume;
    d->scale=s->scale;d->skip=s->skip;d->sound=c->sound;memcpy(d->mapping,s->mapping,45);
}
void h1_config_load(h1_config *c,input_state *s,const char *rom)
{
    config_data d;
    s->scale=1;s->skip=H1_DEFAULT_FRAMESKIP;input_mapping_defaults(s);c->sound=1;c->volume=100;
    config_pack(&d,c,s);
    int result=save_load_aux(&c->store,rom,&d,sizeof d,".cfg");
    c->enabled=result>=0;
    if (result==1) {
        int valid=d.magic==0x31474643 && d.version>=1 && d.version<=3 &&
            (d.version==1 || d.volume<=100) && d.scale<H1_SCALE_COUNT && d.skip<3 && d.sound<2 && !d.mapping[0];
        for (unsigned k=1;k<=44;++k) if (d.mapping[k]>16 || (input_key_reserved(k)&&d.mapping[k])) valid=0;
        if (valid) {
            s->scale=d.scale;s->skip=d.skip;c->sound=d.sound;c->volume=d.version==1?100:d.volume;
            memcpy(s->mapping,d.mapping,45);
            /* These keys were reserved in old configs, so existing installs
             * must receive the new B bindings without resetting other keys. */
            if (d.version<3) s->mapping[H1_KEY_ESCAPE]=s->mapping[H1_KEY_BACK]=RETRO_DEVICE_ID_JOYPAD_B+1;
        }
        else c->enabled=0;
    }
    s->scale_changed=s->skip_changed=1;
    h1_diag("CONFIG_LOAD status=%d enabled=%d scale=%d skip=%d sound=%d volume=%u",result,c->enabled,s->scale,s->skip,c->sound,c->volume);
}
void h1_config_save(h1_config *c,const input_state *s)
{
    config_data d;config_pack(&d,c,s);
    int result=c->enabled?save_checkpoint(&c->store,&d,sizeof d):-1;
    if (result || h1_diag_is_verbose()) h1_diag("CONFIG_SAVE status=%d",result);
}
static uint16_t *canvas;
static void rect(int x,int y,int w,int h,uint16_t color)
{
    for (int r=y;r<y+h && r<272;++r) for (int col=x;col<x+w && col<480;++col)
        if (r>=0 && col>=0) canvas[r*480+col]=color;
}
static void text(const char *s,int x,int y,uint16_t color)
{
    while (*s && x<470) {
        unsigned cp=(uint8_t)*s++;
        if (cp>=0xe0) { cp=(cp&15)<<12;cp|=((uint8_t)*s++&63)<<6;cp|=(uint8_t)*s++&63; }
        else if (cp>=0xc0) { cp=(cp&31)<<6;cp|=(uint8_t)*s++&63; }
        unsigned advance=cp<128?10:20;
        for (unsigned i=0;i<MENU_GLYPH_COUNT;++i) if (menu_glyphs[i].code==cp) {
            advance=menu_glyphs[i].advance;
            for (unsigned r=0;r<21;++r) for (unsigned col=0;col<18;++col)
                if (menu_glyphs[i].rows[r]&(1u<<col)) rect(x+col,y+r,1,1,color);
            break;
        }
        x+=advance;
    }
}
static const unsigned logical[8]={RETRO_DEVICE_ID_JOYPAD_UP,RETRO_DEVICE_ID_JOYPAD_DOWN,
    RETRO_DEVICE_ID_JOYPAD_LEFT,RETRO_DEVICE_ID_JOYPAD_RIGHT,RETRO_DEVICE_ID_JOYPAD_A,
    RETRO_DEVICE_ID_JOYPAD_B,
    RETRO_DEVICE_ID_JOYPAD_START,RETRO_DEVICE_ID_JOYPAD_SELECT};
static const char *logical_names[8]={"上","下","左","右","A","B","START","SELECT"};
static const char *key_names[45]={"未设置","Q","W","E","R","T","Y","U","A","S","D","F","G","H","J","上页",
    "Z","X","C","V","B","N","下页","空格","退出","回车","左ALT","下","右ALT","I","O","P","K","L","符号","右","M","上","删除","确认","左","返回","SHIFT","FN","电源"};
static unsigned mapping_key(const input_state *s,unsigned id)
{ for (unsigned k=1;k<=44;++k) if (s->mapping[k]==id+1) return k; return 0; }
static void box(int page,int index,int *x,int *y,int *w,int *h)
{
    if (page==5) { *x=338;*y=240;*w=104;*h=28;return; }
    if (page==0 && index==8) { *x=18;*y=236;*w=444;*h=32;return; }
    if (page==0 || (page==1 && index<8)) {
        *x=18+(index%2)*230;*w=214;
        *y=54+(index/2)*(page==0?46:32);*h=page==0?40:28;
    } else if (page==1) { *x=18+(index-8)*230;*y=226;*w=214;*h=30; }
    else { *x=38;*w=404;*y=56+index*40;*h=34; }
}
static int count(int page) { return page==0?9:page==1?10:page==5?1:4; }
static int hit(int page,int x,int y)
{
    for (int i=0;i<count(page);++i) {
        int bx,by,w,h;box(page,i,&bx,&by,&w,&h);
        if (x>=bx && x<bx+w && y>=by && y<by+h) return i;
    }
    return -1;
}
static void draw(int page,int cursor,int capture,const char *status,const input_state *s,const h1_config *c)
{
    static const char *titles[]={"游戏已暂停","按键映射","即时存档","即时读档","显示设置","关于 NES","声音设置"};
    static const char *root[]={"继续游戏","按键映射","即时存档","即时读档","显示设置","","更换游戏","退出游戏","关于"};
    static const char *modes[]={"原始大小","保持比例","全屏拉伸","保持比例（线性平滑）"};
    char label[96];
    rect(0,0,480,272,0x0843);text(titles[page],18,8,0xffff);
    if (page==6 && !(status&&*status)) {
        snprintf(label,sizeof label,"音量：%u%%  左右键调节",c->volume);text(label,18,32,0xbdf7);
        rect(38,226,404,12,0x2128);rect(38,226,(int)(404*c->volume/100),12,0x05b4);
    } else text(page==5?"确认／返回回到暂停菜单":capture>=0?"请按实体键，P取消":status&&*status?status:"触摸选择，方向键／确认，返回继续",18,32,0xbdf7);
    if (page==5) {
        text("NES",38,56,0xffff);
        text("v0.1.16",350,56,0xffff);
        text("作者：HelloClyde",38,80,0xffff);
        text("核心：FC / NES: FCEUmm",38,104,0xbdf7);
        text("Thanks:",38,128,0xbdf7);
        text("FCEUmm / FCE Ultra",38,152,0xffff);
        text("H1 GBA / H1 BDA SDK",38,176,0xffff);
        text("Noto Sans CJK / OFL 1.1",38,200,0xffff);
        text("GPL v2",38,224,0xffff);
    }
    for (int i=0;i<count(page);++i) {
        const char *title=label;
        if (page==0) { title=root[i];if(i==5){if(c->sound)snprintf(label,sizeof label,"声音设置：%u%%",c->volume);else snprintf(label,sizeof label,"声音设置：关");title=label;} }
        else if (page==1) {
            if (i<8) snprintf(label,sizeof label,"%s：%s",logical_names[i],key_names[mapping_key(s,logical[i])]);
            else title=i==8?"恢复默认按键":"返回";
        } else if (page==2 || page==3) {
            if (i<3) snprintf(label,sizeof label,"%s槽位 %d",page==2?"存档到":"读取",i+1);
            else title="返回";
        } else if (page==5) title="返回";
        else if (page==6) {
            if (i==0) snprintf(label,sizeof label,"声音：%s",c->sound?"开":"关");
            if (i==1) title="减小音量（-5%）";
            if (i==2) title="增大音量（+5%）";
            if (i==3) title="返回";
        }
        else {
            if (i==0) snprintf(label,sizeof label,"画面：%s",modes[s->scale]);
            if (i==1) snprintf(label,sizeof label,"跳帧：%d（0 为关闭）",s->skip);
            if (i==2) title="恢复默认显示";
            if (i==3) title="返回";
        }
        int x,y,w,h;box(page,i,&x,&y,&w,&h);
        rect(x,y,w,h,i==cursor?0x05b4:0x2128);
        text(title,x+12,y+(h-20)/2,0xffff);
    }
    gba_game_present(canvas,0);
    h1_diag("MENU_PAGE page=%d cursor=%d capture=%d",page,cursor,capture);
}
int h1_pause_menu(input_state *s,h1_config *c,const h1_core *core,const char *rom,uint16_t *screen)
{
    int page=0,cursor=0,capture=-1,pen=-1,dirty=1,result=0;
    uint8_t held[45]={0};
    memcpy(held,s->held,sizeof held);
    const char *status=c->enabled?"":"设置文件损坏，本次不写入";
    canvas=screen;
    h1_diag("PAUSE_MENU_BEGIN");
    for (;;) {
        if (dirty) { draw(page,cursor,capture,status,s,c);dirty=0; }
        int code=-1,key=-1,activate=0,back=0,x=0,y=0;
        h1_event_fetch(&code,&key);
        if (code==11) {
            pen=h1_touch_position(&x,&y)?hit(page,x,y):-1;
            if (pen>=0 && capture<0) { cursor=pen;dirty=1; }
        } else if (code==8) {
            if (capture<0 && pen>=0 && h1_touch_position(&x,&y) && hit(page,x,y)==pen) { cursor=pen;activate=1; }
            pen=-1;
        } else if (code==H1_EVENT_KEY_DOWN || code==H1_EVENT_KEY_UP) {
            if (key<1 || key>44) continue;
            if (code==H1_EVENT_KEY_UP) { held[key]=0;continue; }
            if (held[key]) continue;
            held[key]=1;
            back=key==H1_KEY_ESCAPE || key==H1_KEY_BACK || key==H1_KEY_P;
            if (capture>=0) {
                if (key==H1_KEY_P) { capture=-1;status="已取消"; }
                else if (input_map_button(s,logical[capture],key)) { capture=-1;status="按键已修改"; }
                else status="此键用于菜单，请选择其他键";
                dirty=1;continue;
            }
            activate=key==H1_KEY_CONFIRM || key==H1_KEY_ENTER || key==H1_KEY_Z;
            if (page==6 && (key==H1_KEY_LEFT || key==H1_KEY_RIGHT)) {
                c->volume=key==H1_KEY_LEFT?(c->volume>5?c->volume-5:0):(c->volume<95?c->volume+5:100);
                gba_audio_set_volume(c->volume);status="";dirty=1;continue;
            }
            int delta=0;
            if (key==H1_KEY_LEFT) delta=-1;
            if (key==H1_KEY_RIGHT) delta=1;
            if (key==H1_KEY_UP) delta=page<=1?-2:-1;
            if (key==H1_KEY_DOWN) delta=page<=1?2:1;
            if (delta) { cursor=(cursor+delta+count(page))%count(page);dirty=1; }
        }
        if (back) { if (!page) break;cursor=page==5?8:page==6?5:0;page=0;status="";pen=-1;dirty=1; }
        if (!activate || capture>=0) continue;
        status="";dirty=1;pen=-1;
        if (page==0) {
            if (cursor==0) break;
            if (cursor==6 || cursor==7) { result=cursor==6?1:2;break; }
            if (cursor==5) { page=6;cursor=0;continue; }
            if (cursor==8) { page=5;cursor=0;continue; }
            page=cursor==1?1:cursor==2?2:cursor==3?3:4;cursor=0;
        } else if (page==1) {
            if (cursor<8) capture=cursor;
            else if (cursor==8) { input_mapping_defaults(s);status="已恢复默认按键"; }
            else { page=0;cursor=1; }
        } else if (page==2 || page==3) {
            if (cursor==3) { cursor=page==2?2:3;page=0;continue; }
            status="正在处理，请稍候";draw(page,cursor,-1,status,s,c);
            int rc=h1_state_slot(core,rom,(unsigned)cursor,page==2);
            status=rc==1?(page==2?"存档成功":"读档成功"):rc==0?"这个槽位还没有存档":rc==-2?"存档版本不兼容":"存档失败，请检查文件和空间";
        } else if (page==5) { page=0;cursor=8; }
        else if (page==6) {
            if (cursor==0) c->sound=!c->sound;
            if (cursor==1) c->volume=c->volume>5?c->volume-5:0;
            if (cursor==2) c->volume=c->volume<95?c->volume+5:100;
            if (cursor==3) { page=0;cursor=5; }
            gba_audio_set_volume(c->volume);
        }
        else {
            if (cursor==0) s->scale=(s->scale+1)%H1_SCALE_COUNT;
            if (cursor==1) s->skip=(s->skip+1)%3;
            if (cursor==2) { s->scale=1;s->skip=H1_DEFAULT_FRAMESKIP; }
            if (cursor==3) { page=0;cursor=4; }
            s->scale_changed=s->skip_changed=1;
        }
    }
    h1_config_save(c,s);
    s->scale_changed=s->skip_changed=1;
    memset(s->held,0,sizeof s->held);s->mask=0;s->pause_requested=0;
    h1_diag("PAUSE_MENU_END action=%d",result);
    return result;
}
