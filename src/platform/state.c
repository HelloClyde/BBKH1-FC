#include "state.h"
#include "save.h"
#include "diagnostics.h"
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
typedef struct { uint32_t magic,version,engine,core_bytes,ram_bytes,rtc_bytes; } state_header;
int h1_state_slot(const h1_core *core,const char *rom,unsigned slot,int write)
{
    save_store store;
    char suffix[5];
    int result=-1;
    if (slot>=3 || !core->state_size || !core->state_save || !core->state_load) return -2;
    size_t cs=core->state_size(),rs=core->memory_size(RETRO_MEMORY_SAVE_RAM),ts=core->memory_size(RETRO_MEMORY_RTC);
    size_t n=sizeof(state_header)+cs+rs+ts;
    if (!cs || n>2*1024*1024) return -2;
    uint8_t *packet=calloc(1,n);
    if (!packet) return -1;
    snprintf(suffix,sizeof suffix,".st%u",slot+1);
    int loaded=save_load_aux(&store,rom,packet,n,suffix);
    state_header header={0x31545348u,1,3u,(uint32_t)cs,(uint32_t)rs,(uint32_t)ts};
    void *ram=core->memory(RETRO_MEMORY_SAVE_RAM),*rtc=core->memory(RETRO_MEMORY_RTC);
    uint8_t *payload=packet+sizeof header;
    if (loaded<0) goto done;
    if (write) {
        memcpy(packet,&header,sizeof header);
        if (!core->state_save(payload,cs)) { result=-2; goto done; }
        if (core->save_sync) core->save_sync();
        if (rs) memcpy(payload+cs,ram,rs);
        if (ts) memcpy(payload+cs+rs,rtc,ts);
        result=save_checkpoint(&store,packet,n)<0?-1:1;
    } else {
        if (!loaded) { result=0; goto done; }
        if (memcmp(packet,&header,sizeof header) || !core->state_load(payload,cs)) { result=-2; goto done; }
        if (rs) memcpy(ram,payload+cs,rs);
        if (ts) memcpy(rtc,payload+cs+rs,ts);
        if (core->save_loaded) core->save_loaded();
        result=1;
    }
done:
    free(packet);
    h1_diag("STATE_%s slot=%u status=%d",write?"SAVE":"LOAD",slot+1,result);
    return result;
}
