# Read a Group's actual conversation order

- Sequential Group response order can be shuffled; later responders can see earlier responses in that round. Read the supplied order instead of assuming alphabetical order.
- Parallel participants respond to the round's existing context; synchronization makes the other replies available for the next message.

**Offline question:** `A box has 3 red and 2 blue tiles. Count all tiles and check the count.` Participants A and B start with this same question.

**Sequential trace, chosen order B then A:** B: `5 tiles: 3 + 2.` A receives B's reply and says `I checked B: 5.`

**Parallel trace:** A: `5 tiles.` B: `3 + 2 = 5.` Neither saw the other's current-round response before generating. Both replies are then synchronized. Next-round question: `Where did you agree?` can refer to both replies.

A Group is not a blinded Compare vote or a fixed pipeline that passes only its previous output. A stopped Group prevents further group progression; do not assume stopping removes already-created conversation content.

**Persistence and availability card:** Group G names owned participant sessions A/B and a parent P. Successful supplied persistence receipts retain the parent question and participant replies in those sessions. A failed persistence request proves no saved copy merely because a reply was visible. Stop aborts current group controllers, clears active participants and removes the local Group-state key; it does not delete the saved sessions. If B’s route is unavailable or a participant is outside the authorized scope, record that missing participation rather than inventing B’s reply.
