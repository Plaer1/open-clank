# Layout across viewport sizes

- The complete starter below uses one grid column by default and three equal columns at `min-width:48rem`. A breakpoint tests viewport width; cards keep source order.
- `1fr` shares available grid space. `minmax(0,1fr)` allows a track to shrink; wrapping and `min-width:0` help content fit.
- Assume16px initial font and100%zoom:48rem corresponds to768 CSSpx. Zoom modes can change CSS viewport differently; record the actual width.

**Try:** Propose narrow/wide behavior for the three cards using the supplied rule: predict320 and1024 CSSpx. Describe checks for overflow, reading order and200%zoom, keeping observed measurements separate from derived predictions.
