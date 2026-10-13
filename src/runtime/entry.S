    .set noreorder
    .section .text.h1_bda_entry,"ax",@progbits
    .align 2
    .globl h1_bda_main
    .type h1_bda_main, @function
h1_bda_main:
    addiu $sp, $sp, -32
    sw $ra, 28($sp)
    lui $t0, %hi(__bss_start)
    addiu $t0, $t0, %lo(__bss_start)
    lui $t1, %hi(__bss_end)
    addiu $t1, $t1, %lo(__bss_end)
1:
    sltu $t2, $t0, $t1
    beqz $t2, 2f
    nop
    sw $zero, 0($t0)
    b 1b
    addiu $t0, $t0, 4
2:
    move $t3, $sp
    lui $sp, %hi(__app_stack_top)
    addiu $sp, $sp, %lo(__app_stack_top)
    addiu $sp, $sp, -32
    sw $t3, 24($sp)
    jal h1_run_init_array
    nop
    jal h1_app_main
    nop
    lw $sp, 24($sp)
    lw $ra, 28($sp)
    jr $ra
    addiu $sp, $sp, 32
    .size h1_bda_main, .-h1_bda_main
