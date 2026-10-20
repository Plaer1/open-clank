# Make state visible without relying on color

- Use labels, focus indicators and readable contrast to understand a control's state. Red/green color alone is insufficient.
- Respect reduced-motion preferences. Reapplying an unchanged animated background should retain one running owner; repeated flicker is evidence to report.

**Offline review:** status uses only red/green; keyboard focus has no visible outline; motion is enabled despite reduced-motion preference. Reapplying the same background starts a second animation loop and visibly flickers.

Propose text status, visible focus and reduced motion. Record the repeatable flicker behavior; this exercise does not ask you to edit theme code.
