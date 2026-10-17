#include "h1_sdk.h"
#include "frontend.h"
#include "save.h"
#include "save_policy.h"
#include "diagnostics.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Exact SGZ2 music-only periodic changes use a separate committed
 * snapshot. All other cartridges retain the original full-CRC predicate.
 * Static storage has no heap ownership and contains no callbacks. */
#define SGZ2_SAVE_BYTES 8320u
typedef struct {
    unsigned valid;
    const save_store *store;
    const void *data;
    h1_save_identity identity;
    save_store key;
    uint8_t snapshot[SGZ2_SAVE_BYTES];
} save_policy_context;
static save_policy_context policy_context;

void save_policy_reset(void) { policy_context.valid=0; }

static int policy_admit(const save_store *s,const void *data,size_t n,
                        h1_save_identity *identity)
{
    return s && n==SGZ2_SAVE_BYTES && s->rom_id==0x2128e249u &&
        (s->slot==-1 || s->slot==0 || s->slot==1) &&
        (s->loaded==0 || s->loaded==1) &&
        h1_nes_sgz2_save_identity(data,n,identity);
}
static int policy_identity_equal(const h1_save_identity *a,const h1_save_identity *b)
{ return a->prg==b->prg && a->wram==b->wram && a->iram==b->iram; }
static int policy_matches(const save_store *s,const void *data,const h1_save_identity *id)
{
    return policy_context.valid==1 && policy_context.store==s &&
        policy_context.data==data && policy_identity_equal(&policy_context.identity,id) &&
        !memcmp(&policy_context.key,s,sizeof(*s));
}
static void policy_commit(const save_store *s,const void *data,const h1_save_identity *id)
{
    policy_context.valid=0;
    policy_context.store=s;policy_context.data=data;policy_context.identity=*id;
    memcpy(&policy_context.key,s,sizeof(*s));
    memcpy(policy_context.snapshot,data,SGZ2_SAVE_BYTES);
    policy_context.valid=1;
}
static int policy_protected_equal(const uint8_t *a,const uint8_t *b)
{
    static const uint8_t holes[12]={0x60,0x62,0x66,0x67,0x68,0x6a,
                                  0x6e,0x6f,0x70,0x72,0x76,0x77};
    size_t from=0;
    for(unsigned i=0;i<12;i++) {
        size_t to=8192u+holes[i];
        if(memcmp(a+from,b+from,to-from))return 0;
        from=to+1;
    }
    return !memcmp(a+from,b+from,SGZ2_SAVE_BYTES-from);
}

#define SAVE_MAGIC 0x31534748u
typedef struct { uint32_t magic, version, rom_id, generation, size, crc, header_crc; } save_header;
static int valid_header(const save_header *h, uint32_t id, size_t n)
{
    return h->magic == SAVE_MAGIC && h->version == 1 && h->rom_id == id && h->size == n &&
           h->header_crc == crc32_bytes(h, offsetof(save_header, header_crc));
}
static void slot_path(const save_store *s, int slot, char *p)
{ snprintf(p, H1_ROM_PATH_MAX + 8, "%s.s%d", s->path, slot); }
static int read_slot(save_store *s, int slot, void *data, size_t n, save_header *h)
{
    char path[H1_ROM_PATH_MAX + 8];
    h1_file *f;
    int ok = 0, size, rc;
    h1_size_t got;
    slot_path(s, slot, path);
    if (h1_diag_is_verbose()) h1_diag("SAVE_SLOT_OPEN_BEGIN slot=%d path=%s", slot, path);
    f = h1_fopen(path, "rb");
    if (h1_diag_is_verbose()) h1_diag("SAVE_SLOT_OPEN_END slot=%d handle=%p", slot, f);
    if (!f) return 0;
    if (h1_diag_is_verbose()) h1_diag("SAVE_SLOT_SIZE_BEGIN slot=%d", slot);
    size = h1_fseek(f, 0, H1_SEEK_END);
    if (h1_diag_is_verbose()) h1_diag("SAVE_SLOT_SIZE_END slot=%d bytes=%d", slot, size);
    if (size != (int)(sizeof(*h) + n)) goto close_slot;
    if (h1_diag_is_verbose()) h1_diag("SAVE_SLOT_REWIND_BEGIN slot=%d", slot);
    rc = h1_fseek(f, 0, H1_SEEK_SET);
    if (h1_diag_is_verbose()) h1_diag("SAVE_SLOT_REWIND_END slot=%d offset=%d", slot, rc);
    if (rc != 0) goto close_slot;
    if (h1_diag_is_verbose()) h1_diag("SAVE_SLOT_HEADER_BEGIN slot=%d", slot);
    got = h1_fread(h, 1, sizeof(*h), f);
    if (h1_diag_is_verbose()) h1_diag("SAVE_SLOT_HEADER_END slot=%d received=%u", slot, got);
    if (got != sizeof(*h) || !valid_header(h, s->rom_id, n)) goto close_slot;
    if (h1_diag_is_verbose()) h1_diag("SAVE_SLOT_DATA_BEGIN slot=%d bytes=%u", slot, (unsigned)n);
    got = h1_fread(data, 1, n, f);
    if (h1_diag_is_verbose()) h1_diag("SAVE_SLOT_DATA_END slot=%d received=%u", slot, got);
    if (got != n) goto close_slot;
    if (h1_diag_is_verbose()) h1_diag("SAVE_SLOT_CRC_BEGIN slot=%d", slot);
    ok = crc32_bytes(data, n) == h->crc;
    if (h1_diag_is_verbose()) h1_diag("SAVE_SLOT_CRC_END slot=%d valid=%d", slot, ok);
close_slot:
    if (h1_diag_is_verbose()) h1_diag("SAVE_SLOT_CLOSE_BEGIN slot=%d", slot);
    h1_fclose(f);
    if (h1_diag_is_verbose()) h1_diag("SAVE_SLOT_CLOSE_END slot=%d valid=%d", slot, ok);
    return ok ? 1 : -1;
}
static int load(save_store *s, const char *rom, void *data, size_t n, const char *suffix)
{
    save_header a, b;
    uint8_t header[4096], *scratch;
    uint32_t identity = 0;
    h1_file *f;
    int size, ra, rb, rc;
    h1_size_t got;
    save_policy_reset();
    memset(s, 0, sizeof(*s)); s->slot = -1;
    if (strlen(rom) >= H1_ROM_PATH_MAX) return -1;
    snprintf(s->path, sizeof s->path, "%s%s", rom, suffix);
    if (h1_diag_is_verbose()) h1_diag("SAVE_ROM_OPEN_BEGIN");
    f = h1_fopen(rom, "rb");
    if (h1_diag_is_verbose()) h1_diag("SAVE_ROM_OPEN_END handle=%p", f);
    if (!f) return -1;
    if (h1_diag_is_verbose()) h1_diag("SAVE_ROM_SIZE_BEGIN");
    size = h1_fseek(f, 0, H1_SEEK_END);
    if (h1_diag_is_verbose()) h1_diag("SAVE_ROM_SIZE_END bytes=%d", size);
    if (h1_diag_is_verbose()) h1_diag("SAVE_ROM_REWIND_BEGIN");
    rc = h1_fseek(f, 0, H1_SEEK_SET);
    if (h1_diag_is_verbose()) h1_diag("SAVE_ROM_REWIND_END offset=%d", rc);
    if (h1_diag_is_verbose()) h1_diag("SAVE_ROM_HEADER_BEGIN");
    if (size < 16 || size > 16*1024*1024 || rc != 0) {
        h1_fclose(f); return -1;
    }
    /* NES titles often share the same header and reset code. Hash the whole
     * cartridge so replacing a ROM cannot silently reuse an unrelated save. */
    for (int remaining=size; remaining>0;) {
        unsigned chunk=remaining>(int)sizeof header ? sizeof header : (unsigned)remaining;
        got=h1_fread(header,1,chunk,f);
        if (got!=chunk) { h1_fclose(f); return -1; }
        identity=crc32_update(identity,header,got);
        remaining-=(int)got;
    }
    if (h1_diag_is_verbose()) h1_diag("SAVE_ROM_HEADER_END received=%u",(unsigned)size);
    if (h1_diag_is_verbose()) h1_diag("SAVE_ROM_CLOSE_BEGIN");
    h1_fclose(f);
    if (h1_diag_is_verbose()) h1_diag("SAVE_ROM_CLOSE_END");
    s->rom_id = identity ^ (uint32_t)size;
    scratch = (uint8_t *)malloc(n);
    if (!scratch) return -1;
    ra = read_slot(s, 0, scratch, n, &a);
    if (ra == 1) { memcpy(data, scratch, n); s->slot = 0; s->generation = a.generation; s->crc = a.crc; }
    rb = read_slot(s, 1, scratch, n, &b);
    if (rb == 1 && (ra != 1 || (int32_t)(b.generation - a.generation) > 0)) {
        memcpy(data, scratch, n); s->slot = 1; s->generation = b.generation; s->crc = b.crc;
    }
    free(scratch);
    s->loaded = s->slot >= 0;
    if (s->loaded) return 1;
    /* Do not silently overwrite two damaged/incompatible existing slots. */
    if (ra < 0 || rb < 0) return -1;
    if (h1_diag_is_verbose()) h1_diag("SAVE_INITIAL_CRC_BEGIN bytes=%u", (unsigned)n);
    s->crc = crc32_bytes(data, n);
    if (h1_diag_is_verbose()) h1_diag("SAVE_INITIAL_CRC_END crc=%08X", s->crc);
    return 0;
}
int save_load(save_store *s, const char *rom, void *data, size_t n)
{ return load(s, rom, data, n, ""); }
int save_load_rtc(save_store *s, const char *rom, void *data, size_t n)
{ return load(s, rom, data, n, ".rtc"); }
int save_load_aux(save_store *s, const char *rom, void *data, size_t n, const char *suffix)
{
    if (!suffix || suffix[0]!='.' || strlen(suffix)>4 || !n || n>2*1024*1024) return -1;
    return load(s,rom,data,n,suffix);
}
int save_dirty(const save_store *s, const void *data, size_t n)
{
    h1_save_identity identity;
    if(!policy_admit(s,data,n,&identity)) {
        save_policy_reset();return crc32_bytes(data,n)!=s->crc;
    }
    if(!policy_matches(s,data,&identity))save_policy_reset();
    if(!policy_context.valid) {
        int dirty=crc32_bytes(data,n)!=s->crc;
        /* Preserve the original full-CRC initial evidence. Merely loading a
         * serialized machine state never makes that state persisted. */
        if(!dirty)policy_commit(s,data,&identity);
        return dirty;
    }
    return !policy_protected_equal(policy_context.snapshot,data);
}
static int checkpoint_impl(save_store *s, const void *data, size_t n)
{
    save_header h, check;
    char path[H1_ROM_PATH_MAX + 8];
    h1_file *f;
    uint8_t *scratch;
    uint32_t crc = crc32_bytes(data, n);
    int next = s->slot == 0 ? 1 : 0, wrote, closed, verified;
    if (crc == s->crc) return 0;
    if (n > (size_t)-1 - sizeof(h)) return -1;
    scratch = malloc(sizeof(h) + n);
    if (!scratch) return -1;
    h.magic = SAVE_MAGIC; h.version = 1; h.rom_id = s->rom_id;
    h.generation = s->generation + 1; h.size = n; h.crc = crc;
    h.header_crc = crc32_bytes(&h, offsetof(save_header, header_crc));
    /* One firmware write for the complete transaction. The on-disk format and
     * close/reopen/CRC/byte-for-byte verification are unchanged. */
    memcpy(scratch, &h, sizeof(h)); memcpy(scratch + sizeof(h), data, n);
    slot_path(s, next, path);
    /* Reuse only a same-length inactive slot. H1 fseek returns the absolute
     * position, not stdio's zero-on-success result. Avoiding truncation keeps
     * its allocation while the original complete write/verification remains. */
    f = h1_fopen(path, "r+b");
    if (!f) f = h1_fopen(path, "rb+");
    if (f) {
        int length = h1_fseek(f, 0, H1_SEEK_END);
        if (length < 0 || (size_t)length != sizeof(h) + n ||
            h1_fseek(f, 0, H1_SEEK_SET) != 0) {
            /* A valid H1 handle is released even when close reports an IO
             * failure. Never close it twice or truncate after that failure. */
            closed = h1_fclose(f) == 0;
            f = NULL;
            if (!closed) { free(scratch); return -1; }
        }
    }
    if (!f) f = h1_fopen(path, "wb");
    if (!f) { free(scratch); return -1; }
    wrote = h1_fwrite(scratch, 1, sizeof(h) + n, f) == sizeof(h) + n;
    closed = h1_fclose(f) == 0;
    verified = wrote && closed && read_slot(s, next, scratch + sizeof(h), n, &check) == 1 &&
               check.generation == h.generation && check.crc == crc && !memcmp(data, scratch + sizeof(h), n);
    free(scratch);
    if (!verified) return -1;
    s->slot = next; s->generation = h.generation; s->crc = crc; s->loaded = 1;
    return 1;
}

int save_checkpoint(save_store *s,const void *data,size_t n)
{
    h1_save_identity before,after;
    save_store old;
    int admitted=policy_admit(s,data,n,&before),rc;
    if(admitted) {
        memcpy(&old,s,sizeof(old));
        if(!policy_matches(s,data,&before))save_policy_reset();
    } else save_policy_reset();
    /* Exit and every real checkpoint always execute the complete original
     * transaction, including all8320 bytes and close/reopen verification. */
    rc=checkpoint_impl(s,data,n);
    if(!admitted)return rc;
    if(!policy_admit(s,data,n,&after) || !policy_identity_equal(&before,&after) ||
       memcmp(old.path,s->path,sizeof(old.path)) || old.rom_id!=s->rom_id) {
        save_policy_reset();return rc;
    }
    if(rc==1) {
        if(s->generation==old.generation+1u && s->slot==(old.slot==0?1:0) &&
           s->loaded==1 && crc32_bytes(data,n)==s->crc)policy_commit(s,data,&after);
        else save_policy_reset();
    } else if(memcmp(&old,s,sizeof(old)))save_policy_reset();
    else if(rc==0 && !policy_context.valid && crc32_bytes(data,n)==s->crc)
        policy_commit(s,data,&after);
    /* An already valid exact snapshot is not replaced on rc0. Deliberate
     * full-CRC collisions with protected changes remain dirty for retry;
     * this does not change the original transaction's CRC-equal no-op. */
    return rc;
}
