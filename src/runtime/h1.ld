ENTRY(h1_bda_main)
SECTIONS {
  . = 0x83c00020;
  .entry : { KEEP(*(.text.h1_bda_entry)) }
  .text : { *(.text*) }
  .rodata : { *(.rodata*) }
  .data : { *(.data*) *(.sdata*) }
  .init_array : {
    __init_array_start = .;
    KEEP(*(SORT_BY_INIT_PRIORITY(.init_array.*))) KEEP(*(.init_array))
    __init_array_end = .;
  }
  .got : { *(.got*) }
  . = ALIGN(16);
  .bss (NOLOAD) : {
    __bss_start = .;
    *(.bss*) *(.sbss*) *(COMMON)
    . = ALIGN(16);
    __app_stack_bottom = .;
    . += 0x20000;
    __app_stack_top = .;
    __bss_end = .;
  }
  ASSERT(__bss_end <= 0x83ff0000, "H1 application exceeds 4 MiB loading window")
  ASSERT(h1_bda_main == 0x83c00020, "H1 BDA entry moved by input alignment")
  /DISCARD/ : { *(.comment*) *(.note*) *(.MIPS.abiflags*) *(.reginfo*) *(.eh_frame*) }
}
