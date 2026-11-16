_start:
    xor rdi, rdi
    add rdi, 10
    sub rdi, 3
    cmp rdi, 7
    mov rax, 60
    syscall
