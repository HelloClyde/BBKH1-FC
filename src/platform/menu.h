#ifndef H1_MENU_H
#define H1_MENU_H
#include "frontend.h"
#include "core.h"
#include "save.h"
typedef struct { save_store store; int enabled,sound; unsigned volume; } h1_config;
void h1_config_load(h1_config *, input_state *, const char *rom);
void h1_config_save(h1_config *, const input_state *);
/* 0 resume, 1 choose ROM, 2 exit. Core and audio are stopped by caller. */
int h1_pause_menu(input_state *, h1_config *, const h1_core *, const char *, uint16_t *canvas);
#endif
