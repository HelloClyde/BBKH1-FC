#include "h1_sdk.h"
#include "diagnostics.h"
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

static unsigned sequence;
static int active, verbose, writing;
static unsigned batch_depth;
static size_t batch_used;
static char batch[8192];
#define LOG_LIMIT (512u * 1024u)
#ifndef H1_TRACE
#define H1_TRACE 0
#endif

int h1_diag_init(void)
{
    h1_file *f;
    sequence = batch_depth = 0;batch_used=0;
    active = 0; verbose = H1_TRACE; writing = 0;
    f = h1_fopen(H1_LOG_PATH, "wb");
    if (!f) return 0;
    active = h1_fclose(f) == 0;
    return active;
}
void h1_diag_verbose(int enabled) { verbose = enabled && H1_TRACE; }
int h1_diag_is_verbose(void) { return active && verbose && !writing; }
static void write_bytes(const char *data, size_t n)
{
    h1_file *f = h1_fopen(H1_LOG_PATH, "r+b");
    int offset, rc;
    if (!f) f = h1_fopen(H1_LOG_PATH, "rb+");
    /* Never recreate/truncate an existing log if update-open fails. */
    if (!f) { active=0;return; }
    offset=h1_fseek(f,0,H1_SEEK_END);
    rc=offset>=0 && (unsigned)offset<LOG_LIMIT && n<=LOG_LIMIT-(unsigned)offset;
    if (rc) rc=h1_fwrite(data,1,(h1_size_t)n,f)==n;
    if (h1_fclose(f)!=0 || !rc) active=0;
}
void h1_diag_batch_begin(void)
{
    if (!batch_depth++) batch_used=0;
}
void h1_diag_batch_end(void)
{
    if (!batch_depth || --batch_depth) return;
    if (active && batch_used) {
        writing=1;write_bytes(batch,batch_used);writing=0;
    }
    batch_used=0;
}
void h1_diag(const char *format, ...)
{
    char line[512];
    int prefix;
    size_t n;
    va_list args;
    if (!active || writing) return;
    writing = 1;
    prefix = snprintf(line, sizeof(line), "%06u ", ++sequence);
    va_start(args, format);
    vsnprintf(line + prefix, sizeof(line) - (size_t)prefix - 2, format, args);
    va_end(args);
    n = strlen(line);
    line[n++]='\r';line[n++]='\n';
    if (batch_depth) {
        if (batch_used+n>sizeof batch) { write_bytes(batch,batch_used);batch_used=0; }
        if (active) { memcpy(batch+batch_used,line,n);batch_used+=n; }
    } else {
        /* Hardware-stage records remain durable before the next service call. */
        write_bytes(line,n);
    }
    writing = 0;
}
void h1_diag_hex(const char *label, const void *data, size_t length)
{
    const unsigned char *p = (const unsigned char *)data;
    const char digits[] = "0123456789ABCDEF";
    size_t base, i;
    for (base = 0; base < length; base += 64) {
        char hex[129];
        size_t count = length - base > 64 ? 64 : length - base;
        for (i = 0; i < count; ++i) {
            hex[i * 2] = digits[p[base + i] >> 4]; hex[i * 2 + 1] = digits[p[base + i] & 15];
        }
        hex[count * 2] = 0;
        h1_diag("%s offset=%u bytes=%s", label, (unsigned)base, hex);
    }
}
