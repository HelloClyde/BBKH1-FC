#ifndef H1_STATE_H
#define H1_STATE_H
#include "core.h"
/* 1 success, 0 empty, -1 storage failure, -2 incompatible core state. */
int h1_state_slot(const h1_core *, const char *rom, unsigned slot, int write);
#endif
