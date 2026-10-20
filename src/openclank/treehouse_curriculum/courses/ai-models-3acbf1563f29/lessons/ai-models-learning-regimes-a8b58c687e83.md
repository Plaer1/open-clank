# Identify where the learning signal comes from

Supplied cards:
- Labeled examples 1→A, 2→B.
- Unlabeled points [0,1,9,10]; assign each to the nearer fixed center 0 or 10.
- Feedback: action L has reward 0; action R has reward 1.
- Sequence [a,b,c]: use prefix [a,b] to predict c.
- Deployment context adds “answer in French”; no weight update is recorded.
- Separate fine-tune receipt explicitly records changed weights saved as checkpoint Q2.

Labels support supervised learning; grouping seeks structure; rewards give action feedback; the sequence supplies its own target. Context and training are separate. Group membership is not known class truth.

Changed cases: move point 1 to 6; remove the saved-weight receipt but observe a changed answer. Use the declared nearest-center rule, and keep the missing training evidence unknown.
