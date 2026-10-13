/* JZ4740 MXU1: exact RGB888 interpolation, f=1..255, n>=1.
 * Integer ABI: dst,a,b,n,f (f at caller sp+16). All used XR state is restored.
 * IRQs are masked only while computing one cached RAM scanline; the slow
 * framebuffer copy stays outside this leaf. No GUI/PCM calls under the mask.
 */
.set noreorder
.set noat
.macro xrmove op,xr,gpr
 .word (0x70000000 | (\gpr << 16) | (\xr << 6) | \op)
.endm
.macro xrmem op,xr,gpr,offset
 .word (0x70000000 | (\gpr << 21) | ((\offset / 4) << 10) | (\xr << 6) | \op)
.endm
.macro xr4 op,a,b,c,d,ptn=0
 .word (0x70000000 | (\ptn << 24) | (\d << 18) | (\c << 14) | (\b << 10) | (\a << 6) | \op)
.endm
.macro pixel offset
 xrmem 0x10,3,5,\offset /* S32LDD XR3,a1,offset */
 xrmem 0x10,4,6,\offset /* S32LDD XR4,a2,offset */
 xr4 0x38,1,3,5,2 /* Q8MUL: XR1=(A,R)*inverse, XR2=(G,B)*inverse */
 xr4 0x3a,1,4,6,2 /* Q8MAC: add four channel products */
 .word (0x70000035 | (8 << 22) | (2 << 18) | (2 << 14) | (1 << 10) | (1 << 6))
 xr4 0x3d,0,1,2,3,1 /* S32SFL PTN1: XR3=0x00RRGGBB */
 xrmem 0x11,3,4,\offset
.endm
.section .text.h1_linear_vertical_mxu,"ax",@progbits
.word 0x48314d58 /* Test harness range marker, not executed. */
.global h1_linear_vertical_mxu
.type h1_linear_vertical_mxu,@function
h1_linear_vertical_mxu:
 lw $t0,16($sp)
 addiu $sp,$sp,-32
 mfc0 $t7,$12
 sw $t7,28($sp)
 srl $t8,$t7,1
 sll $t8,$t8,1
 mtc0 $t8,$12
 nop
 nop
 nop
 xrmove 0x2e,16,15 /* saved MXU control -> t7 */
 sw $t7,24($sp)
 ori $t7,$t7,1
 xrmove 0x2f,16,15
 xrmove 0x2e,1,15
 sw $t7,0($sp)
 xrmove 0x2e,2,15
 sw $t7,4($sp)
 xrmove 0x2e,3,15
 sw $t7,8($sp)
 xrmove 0x2e,4,15
 sw $t7,12($sp)
 xrmove 0x2e,5,15
 sw $t7,16($sp)
 xrmove 0x2e,6,15
 sw $t7,20($sp)
 li $t1,256
 subu $t1,$t1,$t0
 li $t2,0x01010101
 mul $t0,$t0,$t2
 mul $t1,$t1,$t2
 xrmove 0x2f,5,9
 xrmove 0x2f,6,8
 sltiu $t2,$a3,4
 bnez $t2,2f
 nop
1:
 pixel 0
 pixel 4
 pixel 8
 pixel 12
 addiu $a1,$a1,16
 addiu $a2,$a2,16
 addiu $a0,$a0,16
 addiu $a3,$a3,-4
 sltiu $t2,$a3,4
 beqz $t2,1b
 nop
2:
 beqz $a3,3f
 nop
 pixel 0
 addiu $a0,$a0,4
 addiu $a1,$a1,4
 addiu $a2,$a2,4
 b 2b
 addiu $a3,$a3,-1
3:
 lw $t7,0($sp)
 xrmove 0x2f,1,15
 lw $t7,4($sp)
 xrmove 0x2f,2,15
 lw $t7,8($sp)
 xrmove 0x2f,3,15
 lw $t7,12($sp)
 xrmove 0x2f,4,15
 lw $t7,16($sp)
 xrmove 0x2f,5,15
 lw $t7,20($sp)
 xrmove 0x2f,6,15
 lw $t7,24($sp)
 xrmove 0x2f,16,15
 lw $t7,28($sp)
 mtc0 $t7,$12
 nop
 nop
 nop
 jr $ra
 addiu $sp,$sp,32
.size h1_linear_vertical_mxu,.-h1_linear_vertical_mxu
.word 0x48314d59
