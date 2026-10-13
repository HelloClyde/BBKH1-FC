#ifndef H1_GBA_GUI_CONTEXT_H
#define H1_GBA_GUI_CONTEXT_H
#include "h1_sdk.h"
#include "diagnostics.h"

/* BBVM pairs GUI+0x84C after selection with +0x850 on exit. H1 V1.41
 * requires this drawing/input window before BDA pixels can be displayed. */
static int gba_gui_open(void)
{
    typedef int (*function_type)(void);
    function_type fn = (function_type)h1_runtime_entry(
        h1_runtime_table(H1_RUNTIME_GUI_TABLE_SLOT), 0x84cu);
    int result;
    h1_diag("GUI_CONTEXT_BEGIN");
    result = fn ? fn() : 0;
    h1_diag("GUI_CONTEXT_END ready=%d", result);
    return result != 0;
}

static void gba_gui_close(void)
{
    typedef int (*function_type)(void);
    function_type fn = (function_type)h1_runtime_entry(
        h1_runtime_table(H1_RUNTIME_GUI_TABLE_SLOT), 0x850u);
    h1_diag("GUI_CONTEXT_CLOSE_BEGIN");
    if (fn) fn();
    h1_diag("GUI_CONTEXT_CLOSE_END");
}
#endif
