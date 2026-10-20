After the tiny program, try this independent contract: exactly two strings, each one ASCII digit0…9. Wrong count, signs, spaces, multidigit or fullwidth input is invalid;0 is valid, not absence.

**SumDigits.java**

```java
public class SumDigits {
    static int add(int a, int b) {
        return a + b;
    }

    static Integer solve(String[] args) {
        if (args.length != 2) return null;
        for (String text : args) {
            if (text.length() != 1 || text.charAt(0) < '0' || text.charAt(0) > '9') {
                return null;
            }
        }
        return add(Integer.parseInt(args[0]), Integer.parseInt(args[1]));
    }

    public static void main(String[] args) {
        if (args.length == 1 && args[0].equals("--test")) {
            String[][] inputs = {{"2", "3"}, {"0", "9"}, {"pear", "3"}, {}};
            Integer[] expected = {5, 9, null, null};
            for (int i = 0; i < inputs.length; i++) {
                Integer actual = solve(inputs[i]);
                if (actual == null ? expected[i] != null : !actual.equals(expected[i])) {
                    System.out.println("test failure");
                    System.exit(1);
                }
            }
            System.out.println("tests=4 passed");
            return;
        }
        Integer result = solve(args);
        if (result == null) {
            System.out.println("error: expected two digits 0..9");
            System.exit(2);
        }
        System.out.println("sum=" + result);
    }
}
```

First compile with `javac SumDigits.java`; run only after success. Use `java -cp . SumDigits` with arguments `2 3`, `0 9`, `0 0`, `--test`: expect sum=5, sum=9, sum=0, tests=4 passed, each exit0. Invalid arguments `pear 3`, `10 3`, `-1 3`, `" 2" 3`, `2`, `2 3 4`, `"２" 3` each print error: expected two digits 0..9 and exit2. In a POSIX terminal use `echo $?` immediately after an invocation to see its status.

Keep the baseline; separately change add to subtraction and rerun the four-case test: expect test failure with process/returned status1. Restore addition and try4,4 for sum=8. Distinguish text concatenation from numeric addition and absence from zero. A hand table is predicted work; only an actual run provides execution evidence. This deeper practice is not added to the first-program run criterion.