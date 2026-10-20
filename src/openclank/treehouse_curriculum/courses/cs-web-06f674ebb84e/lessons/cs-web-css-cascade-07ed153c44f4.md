# CSS rules that compete

- The cascade considers origin, importance, layers and specificity before source order. “Last rule wins” applies only after earlier priorities tie.
- In the same normal author layer, compare `p {color:red}` with `.note {color:blue}` on `<p class="note">Text</p>`.
- In the complete starter below, `link href="style.css"` loads the sibling file. `.card p` selects paragraphs inside a card; `.card` selects class membership.

**Try:** Predict the note paragraph's color and explain when equal-specificity declarations are decided by later order. For the same-priority case, append `.card p { color:#333; }` then `.card p { color:#111; }` to the supplied stylesheet and predict which declaration wins.
