typedef void (*init_fn)(void);
extern init_fn __init_array_start[], __init_array_end[];
void h1_run_init_array(void)
{
    init_fn *p;
    for (p = __init_array_start; p < __init_array_end; ++p) (*p)();
}
