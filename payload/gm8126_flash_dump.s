    .syntax unified
    .arm

start:
    push    {r4-r11, lr}
    sub     sp, sp, #12

    ldr     r5, =0x00100000
    ldr     r8, =0x060035c0
    ldr     r9, =0x060044e8
    mov     r10, #0x100
    mov     r11, #0x00800000

    mov     r0, #'G'
    blx     r8
    mov     r0, #'D'
    blx     r8
    mov     r0, #'M'
    blx     r8
    mov     r0, #'P'
    blx     r8
    mov     r0, #'1'
    blx     r8
    mov     r0, #10
    blx     r8

    mov     r4, #0

read_chunk:
    mov     r0, #0
    str     r0, [sp, #4]
    mov     r0, r4
    mov     r1, r10
    add     r2, sp, #4
    mov     r3, r5
    blx     r9
    cmp     r0, #0
    blt     error
    ldr     r0, [sp, #4]
    cmp     r0, r10
    bne     error

    mov     r6, #0

send_chunk:
    ldrb    r0, [r5, r6]
    blx     r8
    add     r6, r6, #1
    cmp     r6, r10
    blo     send_chunk

    add     r4, r4, r10
    cmp     r4, r11
    blo     read_chunk

    mov     r0, #10
    blx     r8
    mov     r0, #'E'
    blx     r8
    mov     r0, #'N'
    blx     r8
    mov     r0, #'D'
    blx     r8
    mov     r0, #'1'
    blx     r8
    mov     r0, #10
    blx     r8
    b       halt

error:
    mov     r0, #'E'
    blx     r8
    mov     r0, #'R'
    blx     r8
    mov     r0, #'R'
    blx     r8
    mov     r0, #10
    blx     r8

halt:
    b       halt
