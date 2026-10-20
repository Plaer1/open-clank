# Repair a native navigation link

- An anchor is a native hyperlink when it has `href`; its fragment destination must exist.
- Start from the complete two-file card page in “Complete local starter” below. Replace only its Observe article with this defect:

```html
<article class="card"><h3>Observe</h3><p>Record what actually happened.</p><a>Read about this page</a></article>
```

The page has `<section id="about">` after the cards. Earlier controls are two navigation links, then the Name input. CSS styles `a:focus-visible`; cards stay in source order.

**Try:** Predict the missing behavior. Give the smallest native navigation repair, then predict Tab/Shift+Tab order and Enter behavior. State rechecks at320/1024 CSSpx and200%zoom for clipping, source order and focus. Keep predicted checks distinct from actual browser observations.
