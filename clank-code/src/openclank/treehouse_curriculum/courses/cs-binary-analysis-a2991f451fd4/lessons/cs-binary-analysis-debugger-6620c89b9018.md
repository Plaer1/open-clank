# Pause and inspect a call

- A debugger pauses at breakpoints and displays active frames/locals. Optimized variables may be transformed or unavailable; debugging can change timing.
- Canonical source, save-as `toy.c`:

```c
int bump(int x) { return x + 1; }
int main(void) { return bump(2) == 3 ? 0 : 1; }
```

This main checks the helper result; it does not return that result directly. The complete build/LLDB recipe is below.

**Try:** Sketch the active frames at bump entry and predict its argument/return. Separately predict main's process exit status and stdout. Explain one observation limit. Label this source trace as predicted unless you actually paused this exact build.
