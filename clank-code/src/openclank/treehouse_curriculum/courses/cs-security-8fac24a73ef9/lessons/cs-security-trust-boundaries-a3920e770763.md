# Identity and permission

- Authentication establishes an identity claim; authorization decides what that identity may do to a particular resource.
- A server must check permission when a request crosses its trust boundary. A hidden button or supplied owner ID is not that check.
- Treat external data as untrusted until its structure, meaning, and authority are checked for the action.

**Try:** UserA sends a request naming UserB's document. State the check needed and why a valid login alone is insufficient.
