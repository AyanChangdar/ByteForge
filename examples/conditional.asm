_start:
    mov rcx, 0
    mov rdi, 99
    cmp rcx, 0
    jne done             ; not taken
    je equal             ; taken
    jmp done
equal:
    mov rdi, 7
done:
    mov rax, 60
    syscall
