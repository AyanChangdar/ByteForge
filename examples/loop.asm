_start:
    mov rcx, 3

loop:
    sub rcx, 1
    cmp rcx, 0
    jne loop

    mov rax, 60
    mov rdi, 7
    syscall
