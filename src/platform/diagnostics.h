#ifndef H1_DIAGNOSTICS_H
#define H1_DIAGNOSTICS_H
#include <stddef.h>
#define H1_LOG_PATH "A:\\FC\\h1fc.log"
int h1_diag_init(void);
void h1_diag(const char *format, ...);
/* Only batch already-collected RAM statistics, never around hardware calls. */
void h1_diag_batch_begin(void);
void h1_diag_batch_end(void);
void h1_diag_hex(const char *label, const void *data, size_t length);
void h1_diag_verbose(int enabled);
int h1_diag_is_verbose(void);
#endif
