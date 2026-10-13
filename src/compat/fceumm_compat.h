#ifndef H1_FCEUMM_COMPAT_H
#define H1_FCEUMM_COMPAT_H
#include <stddef.h>
/* Extra C library entry points needed by the pinned cartridge core. */
int sprintf(char *output, const char *format, ...);
unsigned long strtoul(const char *text, char **end, int base);
double atof(const char *text);
char *strtok(char *text, const char *delimiters);
int abs(int value);
#endif
