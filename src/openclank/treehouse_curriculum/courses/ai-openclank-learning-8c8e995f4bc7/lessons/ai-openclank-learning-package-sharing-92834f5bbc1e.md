# Check package contents and access separately

- Export definitions and inspect their included resources. Import can create new private identities; it is not a grant to every linked file.
- Keep user responses, profile data and credentials out of a definitions-only package.

**Offline inventory:** P owned by A, revision 1; unit U, assignment Q, asset M; linked private Files resource R. Supplied system export contains definitions, no learner profiles, submissions or tokens. Private import remaps P/U/Q and topic anchors to new private IDs while retaining provenance. Recipient B has a learner grant to the imported course but no access to R, so that image is unavailable.

Owner A may use owner sharing/export controls; editor E has only the operations explicitly granted; B reads/submits. A nongranted user is denied. This inventory is a reasoning card, not an invented import wire format.
