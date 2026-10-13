#ifndef H1_LOADING_H
#define H1_LOADING_H
#include <stddef.h>
#include <stdint.h>
void h1_loading_begin(uint16_t *screen);
void h1_loading_step(unsigned percent, const char *stage);
void h1_loading_size(size_t bytes);
void h1_loading_read(size_t position);
void h1_loading_end(void);
int h1_loading_active(void);
#endif
