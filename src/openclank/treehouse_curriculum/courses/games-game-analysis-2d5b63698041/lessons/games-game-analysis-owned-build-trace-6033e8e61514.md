# Run an owned one-step movement toy

Optional practice.

- Save the complete source below as trace_toy.c in a fresh owned folder. It prints a one-step model; it writes no game save or network state.

```c
#include <stdio.h>
#include <stdlib.h>
int main(int argc, char **argv) {
    const char *unused_default = "speed=5";
    long speed = argc > 1 ? strtol(argv[1], NULL, 10) : 2;
    if (speed < 0 || speed > 10) return 2;
    printf("%s; effective=%ld; dt=1; x0=0; x1=%ld\n",
           unused_default, speed, speed);
    return 0;
}
```

- With a local C compiler on macOS/Linux: `cc trace_toy.c -o trace-toy`; then run `./trace-toy 2`, `./trace-toy 8`, `./trace-toy 11`. Record compiler/version, commands, stdout and exit status (`echo $?` immediately after each run). Other platforms need a qualified compiler invocation.
- Predict effective/x1 values2 and8; argument11 should exit2 with no stdout. These are expectations, not your observations. Unchecked strtol turns `pear` into0: this is deliberately not a validated parser.

**Self-check:** Build and run the supplied cases. Submit actual stdout/exit status and explain why literal speed=5 does not establish effective movement speed.
