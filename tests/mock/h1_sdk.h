#ifndef H1_TEST_SDK_H
#define H1_TEST_SDK_H
#include <stdio.h>
#include <stdint.h>
#include <stddef.h>
typedef FILE h1_file;
typedef uint32_t h1_size_t;
#define H1_RUNTIME_GUI_TABLE_SLOT 0x83C00004u
void *h1_runtime_table(uint32_t);
void *h1_runtime_entry(void *, uint32_t);
#define H1_SEEK_SET SEEK_SET
#define H1_SEEK_CUR SEEK_CUR
#define H1_SEEK_END SEEK_END
#define H1_EVENT_KEY_DOWN 9
#define H1_EVENT_KEY_UP 10
#define H1_KEY_A 8
#define H1_KEY_S 9
#define H1_KEY_F 11
#define H1_KEY_Z 16
#define H1_KEY_Q 1
#define H1_KEY_X 17
#define H1_KEY_V 19
#define H1_KEY_SPACE 23
#define H1_KEY_ESCAPE 24
#define H1_KEY_ENTER 25
#define H1_KEY_DOWN 27
#define H1_KEY_RIGHT 35
#define H1_KEY_M 36
#define H1_KEY_UP 37
#define H1_KEY_CONFIRM 39
#define H1_KEY_LEFT 40
#define H1_KEY_BACK 41
h1_file *h1_fopen(const char *, const char *);
int h1_fclose(h1_file *);
h1_size_t h1_fread(void *, h1_size_t, h1_size_t, h1_file *);
h1_size_t h1_fwrite(const void *, h1_size_t, h1_size_t, h1_file *);
int h1_fseek(h1_file *, int, int);
int h1_event_fetch(int *, int *);
int h1_blit_rgb565(int, int, int, int, const uint16_t *);
int h1_present_full_screen(void);
#endif
