/* Minimal freestanding compatibility; not a desktop filesystem emulation. */
#include "fceumm_compat.h"
#include <ctype.h>
#include <stdio.h>
#include <stdarg.h>
#include <string.h>
#include <limits.h>
#include <streams/file_stream.h>
#include <file/file_path.h>
#include <compat/strl.h>
#include <string/stdstring.h>

int sprintf(char *output, const char *format, ...)
{
    int result;
    va_list args;
    va_start(args, format);
    result = vsnprintf(output, (size_t)INT_MAX, format, args);
    va_end(args);
    return result;
}

unsigned long strtoul(const char *text, char **end, int base)
{
    const char *begin = text;
    unsigned long value = 0;
    int negative = 0, any = 0;
    while (isspace((unsigned char)*text)) ++text;
    if (*text == '+' || *text == '-') negative = *text++ == '-';
    if ((base == 0 || base == 16) && text[0] == '0' && (text[1] == 'x' || text[1] == 'X')) {
        base = 16; text += 2;
    }
    if (!base) base = *text == '0' ? 8 : 10;
    if (base >= 2 && base <= 36) {
        for (;;) {
            unsigned char ch = (unsigned char)*text;
            unsigned digit = ch >= '0' && ch <= '9' ? ch - '0' :
                ch >= 'a' && ch <= 'z' ? ch - 'a' + 10 :
                ch >= 'A' && ch <= 'Z' ? ch - 'A' + 10 : 36;
            if (digit >= (unsigned)base) break;
            value = value * (unsigned)base + digit;
            any = 1; ++text;
        }
    }
    if (end) *end = (char *)(any ? text : begin);
    return negative ? 0ul - value : value;
}

double atof(const char *text)
{
    double value = 0, place = 0.1;
    int negative = 0;
    while (isspace((unsigned char)*text)) ++text;
    if (*text == '+' || *text == '-') negative = *text++ == '-';
    while (isdigit((unsigned char)*text)) value = value * 10 + *text++ - '0';
    if (*text == '.') {
        ++text;
        while (isdigit((unsigned char)*text)) { value += (*text++ - '0') * place; place *= 0.1; }
    }
    return negative ? -value : value;
}

int abs(int value) { return value < 0 ? -value : value; }

char *strtok(char *text, const char *delimiters)
{
    static char *next;
    char *start;
    if (text) next = text;
    if (!next) return NULL;
    while (*next && strchr(delimiters, *next)) ++next;
    if (!*next) { next = NULL; return NULL; }
    start = next;
    while (*next && !strchr(delimiters, *next)) ++next;
    if (*next) *next++ = 0; else next = NULL;
    return start;
}

char *string_trim_whitespace(char *text)
{
    char *start = text;
    size_t length;
    if (!text) return text;
    while (*start && isspace((unsigned char)*start)) ++start;
    length = strlen(start);
    while (length && isspace((unsigned char)start[length - 1])) --length;
    memmove(text, start, length);
    text[length] = 0;
    return text;
}

bool path_is_valid(const char *path)
{
    RFILE *stream;
    if (!path || !*path) return false;
    stream = filestream_open(path, RETRO_VFS_FILE_ACCESS_READ, 0);
    if (!stream) return false;
    filestream_close(stream);
    return true;
}

size_t fill_pathname_join(char *output, const char *directory, const char *name, size_t size)
{
    size_t length = strlcpy(output, directory, size);
    if (length && length < size && output[length - 1] != '/' && output[length - 1] != '\\')
        strlcat(output, "\\", size);
    return strlcat(output, name, size);
}
