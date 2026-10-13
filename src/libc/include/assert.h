#ifndef H1_LIBC_ASSERT_H
#define H1_LIBC_ASSERT_H

#include <stdlib.h>

#define assert(condition) ((condition) ? (void)0 : abort())

#endif
