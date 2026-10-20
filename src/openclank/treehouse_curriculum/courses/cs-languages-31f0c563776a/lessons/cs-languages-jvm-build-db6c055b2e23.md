# Java source, bytecode, runtime

- `javac` compiles source to class files; `java` launches an entry point on the JVM. A JDK supplies development tools.
- Source level, class version, runtime and library APIs must agree. A newer general JDK does not establish a game's loader compatibility.
- Save as `FirstSum.java` (public class and filename match):

```java
public class FirstSum {
    static int add(int a, int b) { return a + b; }
    public static void main(String[] args) {
        System.out.println(add(2, 3));
    }
}
```

Here static methods belong to the class; main receives command-line strings and prints the helper's result.

**Try:** Label compiler and runtime in this file's source→class file→running-process path. Explain why a newer JDK alone does not prove a mod's loader/runtime/API compatibility. Optionally predict this output and change the second argument to0.
