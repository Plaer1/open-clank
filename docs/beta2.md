# Open Clank Beta 2

Beta 2 advances the current models, agents, Files, Editor/Wiki and TreeHouse
workspace. **The release label is Beta 2; the machine version is `1.0.3`.**
The application release tag is `v1.0.3-beta.2`. Use the checksums and
source/target provenance supplied with each release artifact.
[Beta 1 notes](beta1.md) remain the historical record for the previous release.

## Code Graph is Alpha

Code Graph remains **Alpha**, separately from the Beta 2 application label.
Choose an approved workspace code folder to inspect the file map and lazily
expanded stored symbols. Supported grammar-backed declarations, occurrences
and static relationship rules retain their source bindings and evidence states.
The graph does not claim a complete type system, runtime call trace, whole-program
resolution or universal framework coverage.

**Map** keeps the dense folder layout; **Web** presents a traditional network;
**Gravity** uses a force layout. Hide or reveal a folder's children to focus on
one part of the project. Graph and Chat can tile together. Same-folder changes
refresh the graph while preserving the view; the previous published graph stays
visible with its freshness state during indexing. Switching accounts or code
folders still changes the graph's scope.

Indexed metadata search and **Source contents · literal** are distinct:
metadata covers stored declaration fields; source contents searches saved text
in the current policy-admitted code-index files and returns file paths. It is
bounded, case sensitive and not a second whole-source FTS database. New paths
need normal refresh. Open and current-source reads retain workspace and source
checks; a stale symbol is not proof that today's file still matches.

Graph settings separate Code Graph file, symbol, node and edge limits from
note/document view limits. Presentation controls and System indexing resource
preferences have different owners and bounds. Desired resource changes drain existing work before becoming
effective. Generation storage defaults to 2 GiB and supports up to 8 GiB; native
hard ceilings and explicit partial coverage still apply. Indexing large source
folders can take time. Unsupported, malformed or oversized input can be reported
unavailable rather than presented as complete evidence.

## Commands and Settings integration

Chat command help, autocomplete and dispatch share one catalog. Use `/commands`
to inspect purpose, arguments and availability; exact Settings destinations open
the existing control rather than turning a menu choice into a model prompt.
`/literal` deliberately sends leading slash text. Native session commands and
eligible skills keep their own readiness, authority and trust requirements;
help alone does not start a worker. Unavailable actions are reported explicitly.
This is not full parity with every native terminal command or preference.

Settings discovery, Graph settings and System indexing resources open their
existing owners. Browser display preferences, graph presentation limits and
native desired/effective resource settings have distinct scopes; a display
slider does not raise an index hard ceiling. Use the maintained handbook through
[Settings → Help](setup.md#find-the-handbook) for the current command and control
workflows.

## Workspace continuity and portable resources

Chat continuity and model selection preserve the acknowledged session route;
question replies distinguish invalid input from an expired request. Browser
resources use application data/cache resolution and supported executable
discovery instead of depending on one developer's home directory. Existing
explicit resource overrides remain meaningful. Provider connections, tool
availability and successful model execution still depend on actual setup.

Files, Editor and Wiki remain the authoring owners. TreeHouse retains independent
subject progress, shared Main Quests, private courses and the distinction between
self-report and admitted practical evidence. Existing user data and historical
progress must be preserved during upgrade; consult [upgrading](upgrading.md) and
[backup and restore](backup-restore.md) before replacing an installation.

## Packages and supporting artwork

The macOS app targets Apple Silicon and macOS 15 or later. It includes private
Python, Engine and native helpers; its menu-bar owner controls startup and Quit.
The app is ad-hoc signed, not Developer ID signed or notarized. Install only
macOS or Windows architectures actually offered by the release; each artifact's
source, target and qualification receipt is authoritative. Linux release support
remains unqualified. Docker remains unsupported legacy material.

The verified compact artwork remains in the separate
[`v1.0.2-beta.1-artwork-compact` supporting release](https://github.com/Plaer1/open-clank/releases/tag/v1.0.2-beta.1-artwork-compact).
That historical asset identity is intentional: the Beta 2 application tag does
not rename or silently replace the pinned artwork. Keep artwork and personal
data outside a sealed application bundle.

See [setup](setup.md), [known limits](known-limits.md), [security](../SECURITY.md)
and the artifact-specific release receipts. A source feature description is not
a claim that every installed package or OS journey has passed qualification.
