# A frame is a dependency graph

- A forward path shades visible objects as they are drawn; a deferred path stores surface data for later lighting. Different transparency, bandwidth and lighting constraints motivate the choice.
- Alpha blending is generally order-dependent. Two half-transparent layers need not give the same result in reverse order.
- Post-processing consumes earlier images: bloom spreads bright contributions; tone mapping compresses display range; antialiasing addresses sampling artifacts. These operations cannot recover information that was never captured.
- A render graph records pass/resource dependencies. A shadow pass may produce a map that a lighting pass reads.

**Self-check:** Draw a three-pass dependency list for shadow map → lit scene → tone-mapped display. For source-over blending with black background, compare red(50%) then blue(50%) against the reverse order.
