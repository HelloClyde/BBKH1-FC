#ifndef H1_FILE_SELECTOR_H
#define H1_FILE_SELECTOR_H
#include <stddef.h>
#define H1_ROM_PATH_MAX 260
#define H1_GUI_FILE_SELECTOR_OFFSET 0x9ECu
/* 1 selected; 0 cancelled/empty output; -1 invalid output or absent API. */
int h1_select_rom(const char *directory, char *path, size_t capacity);
/* Path bytes remain in the firmware encoding (GBK for Chinese names). */
int h1_rom_path_valid(const char *path, size_t length);
void h1_rom_directory(const char *path, char *directory, size_t capacity);
#endif
