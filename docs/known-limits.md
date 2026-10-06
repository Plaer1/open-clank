# Beta 1 known limits

Beta 1 is the first of several betas. macOS is the current build/dogfood focus.
Windows source support is working and tested, including authenticated startup,
Files, Editor save/reopen and History restore, native PNG/JPEG/MP4 thumbnails,
and host application dispatch. Shared fixes were also tested on macOS. Native
ARM64 and x64 frozen release packages remain unqualified; Linux remains
unqualified. Docker is not officially supported or tested; retained Docker
files and instructions are unsupported legacy reference.

| Surface | macOS | Windows | Linux |
| --- | --- | --- | --- |
| Source launcher / service | Current development/dogfood path | Setup/Check and authenticated startup tested with x64 Python plus ARM Engine/sidecars | Source setup available; unqualified |
| Files and Editor | Approved Locations, save/reopen and recovery tested | Files, workspace binding, Editor save/reopen and History restore tested | Unqualified |
| Native previews / host apps | Native helpers and host dispatch tested | Native PNG/JPEG/MP4 thumbnails, icons and Paint dispatch tested; PDF/DOCX use the native fallback when no handler is available | Unqualified |
| Release packages | See the specific artifact's provenance | Native ARM64 and x64 frozen packages still require qualification | Unqualified |
| Local models | Metal/Apfel or compatible endpoints with backend-specific formats | Use a compatible endpoint or supported local runtime; GPU serving requirements depend on the backend | Runtime/driver requirements depend on the backend; native app unqualified |
| Docker | Unsupported; no official testing | Unsupported; no official testing | Unsupported; no official testing |

Files uses cross-platform framed transport. Opt-in Tonic transport is
macOS-specific. Native previews depend on installed handlers; an unavailable
handler can produce a fallback rather than document-content pixels.

## Connections and execution

App login, model connections/subscription login and mailbox authorization are
separate. Subscription quotas and API billing depend on the provider; a model
name or connected card does not prove every modality/tool is supported. Provider
statistics links open the provider's account page. Optional browser, speech,
document extraction and local-serving tools require their own dependencies.

Google OAuth is available for mail when configured. Microsoft OAuth and Graph
Mail are not implemented; Microsoft accounts requiring OAuth cannot use the
password form. See [mail setup](email-outlook.md).

Agent processes inherit the server account's OS access. Account privileges,
agent scopes and typed approvals are application controls, not a process
sandbox. [SECURITY.md](../SECURITY.md) explains deployment boundaries.

Declarative Hexes activation does not authorize custom executable checks. Beta 1
does not yet provide a supported fresh-install workflow to grant or revoke that
execution trust. Missing, expired or revoked trust remains refused.

## Data and evidence

- Fresh setup is not a universal migration. Use [upgrading](upgrading.md) for
  existing installations; do not bypass startup validation to admit old stores.
- The backup helper snapshots repository `data/` only. Configured stores,
  Copal's full loose vault/metadata/media (or retained Redb database/assets),
  host workspaces and default-excluded material may need independent copies.
  The default loose vault is within the snapshot only when its resolved root
  is inside repository `data/`; independent `COPAL_LOOSE_ROOT` overrides need
  separate preservation. See [backup coverage](backup-restore.md).
- Memory recall, Timeline/Graph, History and Lore are distinct from complete
  installation backup. Recovery depends on captured versions, supported stores
  and retention; it cannot recover everything the process can access.
- Source highlighting/language selection does not imply an LSP, debugger,
  language engine or execution support. Wiki native `.memes` material and
  Markdown/export have different fidelity boundaries.
- Usage can include known, estimated or unknown token/cost fields. Basic logging
  is always on; advanced logging/proxy capture is optional and off by default,
  scoped to Open Clank's supported traffic. Missing evidence is not zero usage.
- Screenshots show visible UI states. Empty/setup views are not evidence of
  completed provider responses, exports, restore or OS integration.

## Current bounded Editor evidence — October 5, 2026

A disposable signed-in candidate exercised manual Save/reopen, click-in rich
syntax comments, safe local edit without an implicit disk save, shared Undo/Redo,
close/Discard, native-document save and nested managed-folder pagination.
Files creation replaces the removed Library-plus launcher. These checks qualify
those candidate journeys; they do not establish every backend, platform,
production login, language dialect or provider workflow. Wiki and Copal Help
still open the shared read-only handbook; personal pages remain personal.

Windows Files, Editor and native image/video thumbnails are implemented and
tested. Native ARM64 and x64 frozen release artifacts remain unqualified; Linux
remains unqualified.

## Fresh source-install evidence — October 6, 2026

A fresh macOS arm64 source install passed normal onboarding and authenticated
Copal New file creation/opening, Editor manual Save/reopen and Discard, native
PNG/SVG display, one real provider Chat and Usage observation, and both read-only
Help destinations. A native note with `type=template` appeared in the picker;
creating a note from it and closing/reopening preserved the exact saved body.
Authenticated Hexes contexts, General entries and preview reads returned 200
after current native-schema alignment.
These bounded checks supersede the older New file, save and native-note template
failures and Hexes server error below for this fresh install. They do not qualify every template source,
configured folder, provider or platform. The executable Hexes trust-workflow gap
above remains open.

## Historical documentation preview

During documentation capture on October 4, 2026, the isolated, signed-in Testing
preview (port `7794`) showed the following results. These have not been
established as production-wide or universal failures:

- Library **+** → Files **New file**, using the default Copal destination,
  reported that the import destination was unavailable. Host Home file creation
  and opening worked, but saving in Editor failed and reopening showed a
  zero-byte file.
- A demo page saved with the template type did not appear in the template picker.
- **Settings → Hexes** showed “Server error.”
- **Go → Document revision history** for the demo Beta 1 handbook copy settled
  on “Copal operation failed.”

In the same preview, a saved synthetic Wiki page and a personal editable
handbook copy persisted and reopened successfully. These observations cover
specific workflows; they do not establish complete product or delivery
acceptance. Record the installed revision, selected backend and reproduction
steps when reporting a similar result.

Machine version remains `1.0.2`. [Beta 1 notes](beta1.md) describe the label and
reporting details. Public release assets/tags must be verified at publication;
these guides do not invent a download or release date.
