"""Canonical handbook authoring data, consumed by the official_docs facade.

Stable IDs address content; asset/example IDs address separately owned reusable
resources. They are references, not invented URLs or executable page content.
"""

ARTICLE_CONTENT = {
    "openclank-docs-home": """# Open Clank Handbook

This handbook ships with **Open Clank Beta 1, the first of several betas**. The macOS15+ Apple Silicon package has passed native and menu-bar Open/Quit qualification, with no Dock icon. It is ad-hoc signed, not notarized. Windows source support is working and tested, including Files, Editor save/reopen and History restore, and native image/video thumbnails. Native ARM64 and x64 Windows installers/packages still need final qualification. Application downloads are not yet announced; the verified offline artwork is available in v1.0.2-beta.1-artwork. Linux release support is coming soon and remains unqualified.

Official pages are read-only. Wiki opens a dedicated page-authoring applet; Editor and Wiki share the same document identity, drafts, saves, history and attachments.

## Start a session

- [[Setup and First Run]] — native installation, sign-in and service checks
- [[Getting Work Done]] — a small workflow from conversation to saved material
- [[Accounts and Models]] — connect a service and choose a model
- [[Chat and Workspaces]] — context, drafts, minimize and restore
- [[Agent Settings and Compaction]] — effective context and continuation policy

## Work with material

- [[App Windows and Navigation]] — app menus, focus, splits and the bottom dock
- [[Editor]] — tabs, splits, source comments, templates and typed documents
- [[Source Editing]] — languages, multicursors, find/replace and local spelling
- [[Wiki Recipes]] — links, section embeds, tables and personal-page examples
- [[Files and Imps]] — personal home, host resources and image projects
- [[Search and Media]] — evidence, video transcripts and retained media
- [[Desktop Capture and Host Opening]] — macOS permissions and native applications
- [[Graph]] and [[TreeHouse and Field Guide]] — connections and guided learning

## Keep work understandable and recoverable

- [[Tasks and Continuations]] — human tasks, task chats and scheduled follow-through
- [[Calendar and Email]] — events, reminders, mailbox connections and drafts
- [[Compare and Cookbook]] — compare connected models and manage local serving
- [[Usage and Stats]] — usage evidence, native Logs and Logging settings
- [[Memory and Lore]] — recall, transcripts and file recovery
- [[Workspace Hexes]] — the active workspace contract
- [[Settings and Themes]] — account, shared and browser-local controls
- [[Recovery]] and [[Limits and Platform Support]] — failures and honest limits
- [[Markdown Formatting Demo]] — rendered Markdown with read-only source reveal
- [[Credits and Included Components]] — lineage, code, artwork and notices

## Open a real screen

[Chat](clank://chat), [Editor](clank://editor), [Wiki](clank://wiki), [Files](clank://files), [Graph](clank://graph), [TreeHouse](clank://treehouse), [Tasks](clank://tasks), [Usage](clank://usage) and [Settings](clank://settings) open app destinations. Open **Settings → Help → Wiki documentation** for Wiki or **Copal handbook** for Editor. Both buttons open this same maintained handbook, even when the Wiki launcher is hidden. Reading a guide does not start a model request or change a preference.
""",
    "openclank-docs-getting-work-done": """# Getting Work Done

Open Clank combines conversations with durable files and Copal documents. Chat holds the discussion; Editor, Files and Tasks hold work you can return to.

## A first useful workflow

1. Open [Chat](clank://chat), choose a connected model and describe one outcome.
2. Attach the material the outcome needs. For host work, select the intended workspace folder rather than assuming the assistant can see every file.
3. Review the answer and any tool results. A proposed action and a completed action are different; look for the saved document, task or result.
4. Open [Files](clank://files) and choose **File → New file…**. Choose a writable destination, name the file, and choose **Create**. In **File created**, choose **Open** to continue in Editor. Save the useful decisions there; use a personal Wiki page for links such as `[[Page Name]]`.
5. Reopen the document from [Files](clank://files). This confirms where the durable result lives. Use a task when follow-through needs its own chat.

## Choose the right surface

| Work | Destination |
| --- | --- |
| Conversation, attachments and tools | Chat |
| Markdown, source files, typed tables and splits | Editor |
| Wiki pages, rich authoring and page navigation | Wiki |
| Folders, host files, images and personal home | Files |
| Linked documents and outlines | Graph |
| Guided lessons and app discovery | TreeHouse |
| Scheduled work and questions awaiting you | Tasks |
| Numeric usage and provider-limit evidence | Usage |

Official handbook pages belong to the installation and remain read-only. Personal documents belong to your account. A familiar title does not make a personal page an official page.

## When the workflow stops

If the model is unavailable, check [[Accounts and Models]]. If an attachment cannot resolve, reopen it in Files and check its access state. If a save is refused, preserve your draft and follow [[Recovery]]. Retrying a provider request can incur another charge; inspect the existing result first.
""",
    "openclank-docs-accounts-models": """# Accounts and Models

Sign-in selects the owner of your chats, Copal documents, tasks and memories. Changing accounts changes the workspace you can see. It does not merge data or grant access to another account's document links.

## Connect a model

1. Open [Settings](clank://settings) and choose **Add Models**.
2. Select the **Local**, **API**, or **Subscription** setup card. A local server must be running; hosted services need a supported account or API connection.
3. After connecting, check [Added Models](clank://settings/added-models), the inventory of models already added to this account.
4. Return to Chat and select the model before sending. Use the displayed provider/model identity when checking which route answered.

Add Models is the setup chooser; Added Models is the resulting inventory. Connecting a provider does not guarantee its search, vision, media or quota capabilities. Those depend on the selected route and account entitlement.

## Choose subscription login or API access

Open **Settings → Add Models** and choose the setup card that matches how you intend to connect:

- **Subscription** signs in to a provider account only when that provider exposes a supported login method. The provider decides which models and account entitlements are available.
- **API** connects a supported provider endpoint with its API key and endpoint settings. Requests follow that provider's API billing and quota rules. A consumer chat subscription does not automatically include API credits.
- **Local** connects to Ollama or another reachable model server. The server must be running, and its available model list is installation- dependent.

After setup, **Added Models** lists the models already connected to the account.

These connections are separate from the Open Clank sign-in that owns your workspace. Use only a provider method shown in the current form; provider login methods and model availability can change. Keep API keys in the credential field, never in chat prompts, documents or source control.

## Model choice and Compare

Use a connected model suited to the work and input type. Switching models in a conversation retains the conversation; it does not erase the cost or provenance of earlier turns. Compare requests answers from the selected models for the same prompt, so each request can have its own usage and cost.

If a model disappears or a connection fails, inspect Added Models and the reported error. Do not assume a similarly named model is the same route. [[Usage and Stats]] distinguishes a reported fact from an estimate.

## Settings ownership

Provider connections and Agent preferences are account-owned. Theme and Shortcuts preferences are browser-local; Appearance's **Copal navigation** launcher visibility is account-owned. History and File access include shared policy. Search, Tools, Users and System administration can affect the installation. The scope label on each Settings panel tells you who the change affects.

Application access requires a real authenticated account. Historical local and ownerless data remain preserved for explicit recovery; signing in never remaps them automatically.
""",
    "openclank-docs-chat-workspaces": """# Chat and Workspaces

Each conversation retains its transcript, selected context and attachments. Copal documents remain separate durable resources that a conversation can reference. Opening a document link goes to Editor.

## Start, minimize, close and restore

Select a chat from the conversation list to resume it. The chat's popout control makes it an app window; use its **Minimize** control to clear space, then select its item in the bottom dock to restore it. Close dismisses the window; it is distinct from deleting a conversation. Reopen the saved conversation to continue its transcript.

Before closing a running turn, use Stop if your intent is to cancel the work. Window visibility alone is not proof that a tool or background task stopped. Check the task chat or completed tool result before starting a duplicate run.

## Enable tools for the intended work

Chat's **Shell Access** control requests shell/tool access for the conversation. Use it only for the workspace and task you intend, then inspect the proposed action, any required approval and the returned tool result. A selected toggle is not a completed command or permission to every host path.

Administrators use **Settings → Agent Tools → Built-in Tools** to enable or disable the tools available to the agent. Shell, file, search and memory operations still depend on their implementation, account privileges, configured connections and OS access. Tool availability, the conversation's access choice and an individual approval are separate checks. See [[Workspace Hexes]] for workspace instructions and [[Settings and Themes]] for MCP connections.

## Select the workspace deliberately

A workspace folder supplies host context for file tools. Select the folder you intend to work in and check its displayed identity. Editor's **Open Folder** opens a host folder as a workspace through the supported folder selection flow. A folder opened on the host is different from a Copal folder in your account's document tree.

File access policy and OS access still apply. Selecting a workspace is not a grant to every path on the computer. If a request is rejected, check the selected workspace and [File access](clank://settings/file-access).

## Editor drafts

Editor retains unsaved drafts across navigation and window management. Typing, changing focus or arranging windows does not save the document. Choose **Save** or Command/Control+S and check the Saved state before changing accounts or clearing browser storage. A recovered local draft is not a stored revision or a Lore checkpoint.

If another writer changes the stored revision, the guarded save can refuse your stale revision. Keep the draft, reopen the current document and merge the intended changes. See [[Recovery]].

## Links and account boundaries

Resource identity keeps supported document links stable across renames. Access is still checked for the signed-in owner. A link from another account does not expose that account's document or automatically create a copy.
""",
    "openclank-docs-editor": """# Editor

Editor is the workspace for documents, typed tables, Bases, Canvas, source and media. [Wiki](clank://wiki) is a separate page-authoring applet over the same document core. Open a page in either presentation without making a second copy: identity, unsaved content, saves and history stay shared.

## Open and arrange work

Editor’s menus sit directly below its own title bar. **File → Open file…** opens the shared Files browser in a selection dialog, with its folder tree, breadcrumbs, search and view controls. Select an eligible file, then choose **Open** in the fixed footer; double-click or Enter can also confirm a file. **Cancel** closes the dialog. An unsupported selection or failed open shows a reason and leaves the dialog available for another choice. **Quick Open** remains a separate keyboard chooser.

Open a document from Files or a document link. Editor tabs retain parallel work; splits let you compare material. Command/Control-click requests a new tab, and the context menu offers split right and split below. Ordinary document navigation uses the current tab. Editor’s **Go** menu offers **Previous document**, **Next document** and **Document revision history…**; Explorer’s folder arrows navigate folders instead.

Use **Open Folder** for a host workspace. Folder selection and **Choose template folder** use the same Files dialog and fixed Open/Cancel footer. Check the selected folder before editing host files. This does not import the whole folder into Copal or grant access to other host paths.

[[App Windows and Navigation]] explains focus and the bottom dock; [[Source Editing]] covers language modes, multicursors and spelling.

## Save, Undo and close

Edits stay **Unsaved** until you choose **Save** or Command/Control+S. A completed save records the submitted revision; edits made while that save is running can remain Unsaved. Reopen the resource to verify the durable result. Unknown save outcomes or revision conflicts retain the draft for review.

Editor and Wiki share one draft and per-document Undo/Redo history. Undo from either presentation acts on that shared history. Local draft recovery helps resume interrupted work; it is not a durable save or an independent backup. Closing dirty work offers **Save**, **Discard** or **Cancel**: save the intended changes, discard the local draft, or keep editing. Changing tabs, focus or window size does not save.

## Explore folders and Favorites

Editor’s host Explorer shares Files’ expandable tree and collapsible **Favorites**, **Workspaces**, **Locations** and **Open Clank** categories. Only available resources appear. Expand a folder to browse its children; opening another folder keeps the tree rooted. The chosen workspace remains visible while you browse. A Favorite outside it does not change the workspace anchor. Removing a Favorite removes its shortcut, not its files.

Managed Copal folders form a nested path hierarchy. A folder view lists its direct children; expand or open a child to go deeper, and load more when results are paged. Folder and document identities remain independent. Navigation-only folders do not grant filesystem create, move or write permission.

Use Explorer’s Back, Forward, Up, Home and Refresh controls for folder navigation. Copal’s own document/folder tree remains alongside host resources; a host folder and a Copal folder have separate identities and access rules.

Choose **Customize Explorer…** from the explorer heading or Appearance’s Editor panels. Reorder sections, choose icon/text headers, hide or collapse sections, and set divider style. Files and Editor retain independent layouts; sharing their browser components does not share these preferences. Drag the navigation divider to resize its width; category height and collapse controls let long trees share the available space. Hiding a section changes its presentation, not the underlying files.

## Write a Wiki page

Open [Wiki](clank://wiki) and choose **New page**. Its navigation offers search, All pages, Recent and Pinned, with Backlinks for the current page. **Rich** uses the live editor; headings, emphasis, lists, checklists, links, tables and media controls make real source transactions. **Source** exposes the same Markdown; **Read** renders it. Unknown native fields and unsupported source remain preserved. The Library menu opens native page properties, links and relations, and `.memes` import/export.

Choose **Open in Editor** or Editor’s **Open in Wiki** command to view the same record in the other applet. Both windows can stay open; edits share one scoped buffer and save state. Existing adopted media can be selected from **Media**; uploading, pasting or a Files drop uses the normal attachment path. A missing or ambiguous link target stays unresolved rather than silently substituting an unrelated page.

[[Wiki Recipes]] gives examples, including section embeds and media. Create a personal page to practice them; the official guide stays read-only.

## Templates and typed tables

Use Editor’s **Choose template folder** command for a host template folder, or set a Copal note’s type property to `template`. Use **Insert template** with an editable destination open. Insertion copies the template into the current document; changing that result does not rewrite the template. Resource references must still be accessible to the owner.

Markdown tables render inline. Typed table/Bases views provide their own structured controls and properties. Saving a typed document retains its native representation; exported Markdown is a projection of that record.

## Source comments

Ordinary native comments and supported docstrings are source text. The active syntax mode detects them automatically and shows eligible prose as an inset. Click the prose, or use keyboard activation, to edit it in that same inset. Leaving the inset applies a safe local source edit and marks the document Unsaved; choose Save to persist it. Right-click reveals the raw comment directly.

Comment prose can include several images and normal `![[target]]` or `![alt](target)` references; an existing `!![[target]]` spelling remains preserved. Resources still need an accessible target. Unsafe, stale or ambiguous edits remain pending with an inline reason; correct or discard them, or edit the raw source. The surrounding source and native comment delimiters remain protected.

Syntax metadata covers the current language inventory, not exhaustive acceptance of every dialect. Commentless modes, strict JSON, incomplete syntax and some compound or escape-sensitive strings/macros remain raw or require source editing. Existing attached range comments remain document metadata where supported. Preserve a conflicted draft rather than blindly overwriting the stored version.

## Media and source reveal

Supported image, audio, video and PDF references render through the resource resolver. A missing or inaccessible resource shows a diagnostic. Attach or adopt the file through Files before relying on it as durable media.

[[Markdown Formatting Demo]] has read-only source reveal. Its inspector lets you copy the source of a rendered element and return to the rendered page; it never saves edits to the official article.
""",
    "openclank-docs-files-imps": """# Files and Imps

[Files](clank://files) brings account documents, folders, media and eligible host resources into one browser. The displayed resource and its capabilities determine what can be opened or edited.

## Create a file in Files

1. Open Files and choose **File → New file…** (also available under **New**).
2. If there is no writable destination selected, choose an authorized folder in the destination picker. The name prompt identifies that folder.
3. In **New File**, replace the proposed `Untitled.md` name and choose **Create**. **Cancel** leaves creation unfinished. A collision asks for another name; creation does not overwrite the existing file.
4. In **File created**, choose **Open** to open the created file in Editor, or **Later** to leave it saved in Files. Edit, save and reopen from that destination to check the result.

A host destination creates a host file; a managed destination uses its own provider/storage. A folder that cannot create files disables the action. Changing account or access during the flow can require choosing New again.

The inherited Odysseus document editor is no longer an improvement target. Existing Library documents, attachments and email compatibility paths are preserved. New authoring work belongs in Open Clank Editor and Wiki; this handbook does not ask you to migrate or delete older data.

## Browse and open

Files’ menus sit directly below each window’s title bar. Start at Home, open the intended folder and select a resource. Open a document in Editor; use image actions for an image. The same identity connects Files and Editor, so opening a document does not create another copy. **Download** is a separate action. Menus, item actions and right-click commands depend on the selected resource’s capabilities; check read-only or unavailable indicators before a write.

Managed folders show direct children in a nested hierarchy; open a child folder to descend and use loaded/paged results as needed. A navigation folder does not grant host write authority.

The expandable navigation tree groups **Favorites**, **Workspaces**, **Locations** and **Open Clank** collections. Collapse a category or expand a folder without replacing the tree with the current folder’s children. Only actual available locations appear. Favorite shortcuts can be removed without deleting files. Back, Forward, Up, Home, Refresh and breadcrumbs navigate the content pane. In a narrow window, **Navigation** opens or hides the tree.

Host locations are governed by [File access](clank://settings/file-access). An unavailable location may have moved, lost OS access or become ineligible. Refreshing Files cannot grant access that policy or the OS denies.

Use the content pane's search and source filter to narrow results, then the sort controls and **Folders first** to arrange them. Search status identifies its scope; a filtered/partially loaded listing is not every file on the host. Click to select, Shift to extend a range, and Command/Control to toggle items. Drag a selection rectangle in eligible views, or use **Select all** for the loaded selection. Right-click and **Actions** offer only the operations that the selected resources support. Space opens an eligible preview.

**View → Customize Explorer…** controls category visibility, ordering, headers and dividers. Use **Show**, **Collapsed**, **Move up/Move down**, **Height: Auto** or **Height: capped** and **Cap in pixels**; **Reset layout and sizes** restores this surface's layout. Collapse categories, adjust their available height and drag the sidebar divider to resize navigation. File thumbnails and native icons depend on eligible type, host support and loading; a fallback icon does not mean the file is missing. Files' layout and Editor Explorer's layout have independent preferences.

## Windows, split panes and views

Choose **New Files Window** from **File** or **Window** for an independent browser. **Window → Split right** or **Split below** adds a second pane in the same window; focus either pane to navigate it, or use **Focus other pane**. **Close split** returns to one pane. Files has no tabs. Each pane keeps its own folder navigation and selection; the shared sidebar follows the active pane.

Choose List, Grid, Details, Columns or Gallery from **View**. Columns browses a folder hierarchy; it is separate from a two-pane split. Fresh ordinary folders start in compact Details, while saved view preferences take priority. Use **Icon size** or View’s size presets: Grid/Gallery offer 32, 64, 96 and 128 px; compact modes offer 16, 20, 24 and 32 px. Changing size preserves your selection and adjusts the layout. View, size and sorting preferences are remembered for the signed-in owner.

## Move, copy and name collisions

Drag supported files between split panes or independent Files windows. An internal drag moves by default; hold **Option on macOS** or **Control on Windows/Linux** to copy. Shift explicitly requests Move. Command on macOS remains a selection modifier. Holding both the Copy modifier and Shift is rejected. The destination and source must support the requested operation; changed access, stale selections and unsupported transfers show a reason.

**Edit → Copy/Cut**, then **Paste** in the destination, uses the same file transfer operations. In a text field, editing commands act on text. Review the operation result before retrying. If a destination name already exists, **Keep Both** retries failed items with a new destination name instead of overwriting the existing file.

## Imps projects

Imps provides managed image editing. Open an eligible image from Files to work with its project, layers and tools. Save a project to retain editable state; exporting an image produces a flattened delivery file.

Try a small edit on a copy: select **Crop** or **Brush**, inspect the visible result, then open **Save**. **Save as copy** keeps a new image; **Save over original** replaces the eligible original after its guarded checks. **Download PNG** creates a delivery file, while **Save project (.json)** retains layers for later editing. **+ Import** adds an image as a layer. Selection/mask and model-assisted tools have their own prerequisites and result states. Reopen the saved image/project to verify the intended result; a rendered preview or downloaded PNG is not proof the layered project saved.

Managed saves use revision checks and recovery preimages. If the revision changed or preimage capture failed, the save is refused. Keep the working state, reopen the current project and inspect its history before retrying. See [[Recovery]] for restoring a saved state.

## Existing Gallery material

The separate Gallery applet has been retired. Image browsing now belongs to Files and image editing to Imps. Legacy Gallery links route to Files. Find existing image material in its Files folder; do not expect the old album management screen.

## Keep a media result

Review a generated or retrieved image, then save/adopt it using the offered Files action. A remote result URL can expire and is not a permanent local copy. Link the retained resource in a personal document and reopen the page to confirm resolution. [[Search and Media]] covers provenance and source evidence; [[Desktop Capture and Host Opening]] covers native opening.
""",
    "openclank-docs-graph": """# Graph

[Graph](clank://graph) shows relationships among documents: links, embeds, tags and supported typed relations. Select a node to open its document in Editor. Use it to find context around a page, then read the actual source.

## Find a useful view

Use the filter panel to narrow by folder, kind, tag or property. Clear a filter to broaden the result. Facets come from document metadata; large collections may take time to load. An empty view can mean the filters exclude everything rather than that your documents disappeared.

Galaxy mode combines supported document/calendar relationships. Structure mode exposes a page's headings and bullets as an outline. Inspect the selected page before using structural editing actions; a Graph view is not a backup of its content.

## Official pages

Official documents are hidden by default so personal work is easier to see. Use **Include official docs** or the OpenClank folder filter to include the handbook. Recognition follows official identity metadata, not just a folder name. Your personal note in a folder named OpenClank remains personal.

## Try a small graph

Create a personal page in Wiki, add one link to another personal page, save it and open Graph. [[Wiki Recipes]] shows the link syntax. Clear restrictive filters if needed. Click the linked node and check that it opens the intended document.

[[TreeHouse and Field Guide]] links guided exercises to these same app screens.
""",
    "openclank-docs-memory-lore": """# Memory and Lore

Conversation history, semantic memory, numeric usage and file recovery retain different kinds of evidence. Knowing which one you need makes recovery and review much clearer.

| Evidence | Purpose | Start here |
| --- | --- | --- |
| Chat transcript/archive | What was said and which tools ran | Original chat |
| Semantic memory | Facts the assistant can recall | Memery/Brain |
| Numeric usage | Reported tokens, estimates and limits | Usage |
| Lore/document history | Prior recoverable file state | Document/project history |

## Semantic memory

Open **Memery** from the sidebar or compact rail (the app link is [Memery](clank://memory)). **Memories**, **Inspect**, **Graph**, **Digest**, **Skills**, **Add** and **Settings** expose different memory tasks. Enabled memory features can retain recollections for later conversations. Review the actual saved entries when correcting recall; changing a chat message is not the same action as deleting a memory.

Memory belongs to the account and does not replace saved documents. Keep important instructions and decisions in an authoritative document or workspace contract rather than assuming recall will reproduce exact text.

## Skills, prompts and admission

Use **Add → Add Memory** for a fact or preference you intend to retain, then inspect its saved entry. **Inspect** distinguishes raw trajectory evidence, candidates awaiting admission, curated recallable material and quarantine. Quarantined material stays out of normal recall. Kind, provenance and trust filters help explain where an entry came from; recall can still be incomplete or wrong, so open the source before relying on it.

Use **Add Skill** or the import controls for reusable skill instructions; **Skills** exposes saved entries, drafts, audit and approval actions. Review an imported skill's instructions and sources before approval. **Settings → Inject Skills**, enablement, auto-extraction and auto-approval affect future use; a saved skill is not proof it was invoked successfully. **Audit** can use a model and has its own prerequisites and usage.

Saved prompts/personas belong to their prompt or **AI Defaults** controls. A prompt directs a turn; a skill packages reusable instructions; semantic memory supplies admitted recall. Keep exact source files and important instructions outside recall as well.

**Nuke my Brain data** is an account-owned preview/confirmation flow, not file restore. Its live-data selection does not delete retained source files, exports or backups. Read the selected categories and partial-result report.

## Timeline and Graph

[Timeline](clank://timeline) arranges supported document/calendar events in time; [Graph](clank://graph) shows document relationships. Memery's Graph is a memory view. Filters and selection help inspect each source, but none of these views is the entire memory database or a file backup.

## Lore and recovery

Lore retains recoverable preimages for supported managed operations. A guarded save that requires a preimage must refuse if capture fails. Open the document or project's history to inspect an earlier state before restoring.

[History settings](clank://settings/history) controls shared retention/storage policy for the recovery service. It is not a semantic-memory retention panel and changing it can affect the installation's recovery budget.

Current hosted Editor/Wiki stores documents in an ordinary Files-backed vault, including the associated identity, revision, history, trash and media metadata. The current source has one hosted storage backend. Older installed revisions can still have Redb data; preserve those stores with their matching revision and follow explicit recovery or conversion guidance. Updating source or changing a path does not convert old data.

## Transcript and usage limits

A compacted model context is not the whole conversation archive. Usage totals are numeric observations, not a copy of prompts or a memory search index. See [[Agent Settings and Compaction]], [[Usage and Stats]] and [[Recovery]].
""",
    "openclank-docs-tasks": """# Tasks and Continuations

[Tasks](clank://tasks) holds follow-through work and the questions that need your answer. A task retains its own chat so its outcome and continuation stay together.

## Human tasks and model tasks

**Meatbag Tasks** is the Copal list for your own work: save the item, inspect its source/date/status and mark completion when you have done it. **Clanker Tasks** schedules model work and retains run history/output. A checkbox or human-task status does not dispatch a model by itself.

In Clanker Tasks use the new-task control, review **New Task**, its prompt, model, schedule and output target, then save. **Run now**, **Pause**, **Resume** and **History** are available from the task actions when applicable. A saved output target needs its own access/connection; an email output can send mail when run, so inspect the target before enabling it.

## Create and inspect a task

Ask explicitly for a task when work should outlive the foreground conversation. Review its description and schedule before relying on it. Open the task from the list, badge or toast to inspect the original task chat. Answering that task's question resumes its chat rather than substituting the conversation currently in front.

## Scheduling and completion

Use the task's supported scheduling controls for recurring or later work. Check the next-run state and the task transcript after a run. A scheduled entry is not proof that a provider request or file action succeeded.

Stop/cancel work through its task controls when your intent is to stop the run. Closing its window only changes visibility. A continuation resumes the task's context; avoid creating a duplicate task just to answer a question.

## Checkboxes in documents

Markdown task lists render checked and unchecked items. Their rendering alone does not create scheduled work or make the official handbook editable. Use an explicit task action where offered, then confirm the task in Tasks.

For a failed run, inspect its error and saved results before retrying. A retry may repeat external actions. [[Memory and Lore]] explains what evidence is retained and [[Recovery]] explains guarded file restore.
""",
    "openclank-docs-settings-themes": """# Settings and Themes

[Settings](clank://settings) owns configuration. The Tools appearance/theme entry focuses **Settings → Appearance**. Panel scope labels identify who a change affects.

## Scope before saving

| Panel | Scope and purpose |
| --- | --- |
| Theme, typography, layout, Shortcuts | This browser's presentation and keyboard preferences |
| Appearance → Copal navigation | Account-owned launcher visibility; hiding retains data/access |
| Add Models, Added Models, AI Defaults, Account | Signed-in account connections and preferences |
| History | Shared Lore storage and retention policy |
| File access | Shared locations/access policy and separate agent approvals |
| Search, Tools, Users, System | Administrator policy for the installation |

Opening a panel focuses its controls. Account ownership of documents does not make every setting account-local. Ask the installation administrator when a shared/admin control is unavailable to your role.

## Accounts, integrations and administrator controls

**Account** changes your account credentials/preferences. **Integrations** connects configured external services; **Email** and **Reminders** have their own connection/delivery settings. **Users** and **System** require the appropriate administrator role. Review each form's scope and read back its saved state; opening a setup panel does not establish a successful connection. File access separates shared eligible locations from agent approvals and their lifetime. These app controls do not replace OS process confinement. Read reset/export previews carefully: account export, Brain reset, achievement reset and installation-wide data wipes affect different categories.

## Connect an MCP tool server

1. Open **Settings → Integrations** and choose **MCP Tool Server** from the add menu.
2. In **Add MCP Server**, enter its Name and Transport. **stdio** uses the configured Command, JSON Args and Env on the application host; **SSE** and **Streamable HTTP** use the server URL. Use the connection details for that server.
3. Choose **Save**. If authorization is required, complete the offered **Authorize** flow and return to the server status. Inspect **Connected**, the available tool count or the reported error; a saved entry alone does not prove connection.
4. Open the server's management view to **Enable/Disable**, **Reconnect** or choose which discovered tools are enabled. Check a real tool result in the task's chat before relying on it.

MCP exposes the configured server's tools; it does not guarantee every server, transport or login method works. Management requires the appropriate administrator privileges. A stdio command runs on the host and remote tools can receive the material supplied to them. Keep credentials in the connection's credential/configuration fields, and review the service's access and data handling. App tool controls do not replace OS process confinement or a workspace contract.

## Change appearance

1. Open [Appearance](clank://settings/appearance).
2. Select a theme and check readability in both Chat and Editor.
3. Adjust accent color, font, density and UI text scale as needed.
4. Choose a background/effect and adjust its **Intensity (%)**, **Size (%)** or color controls where available. Controls vary with the selected effect.
5. Use Solid or a quieter effect when motion or rendering load is distracting.

The shipped family includes Signal Routes, Kene-inspired weave, LCARS, Google Emoji Drift, Matrix/Emoji Rain and simpler decorative effects. The current picker is the authority for available choices. LCARS has a **Mode** dropdown that includes **Status Sweep**; Status Sweep is an LCARS mode, not a separate theme. Custom named themes can be saved and exchanged through the supported JSON controls; importing one changes appearance, not document content.

## Current fresh appearance defaults

With no saved override, **Open Clank Dark** selects **Google Emoji Drift** with locally bundled Google/Noto and Emoji Kitchen artwork, **Liga Comic Mono**, and Advanced settings enabled. A saved theme or control value takes priority. The current Dark defaults are:

| Control | Fresh Dark value |
| --- | --- |
| Drift speed | 5% |
| Drift speed variation | 12% |
| Size variation | 100% |
| Intensity variation | 0% |
| Middle intensity | 200% |
| Total quantity | 115% |
| Glow likelihood | 6% |
| Rotation likelihood | 71% |
| Rotation speed | 5% |
| Rotation speed variation | 18% |
| Use one emoji pool | On |

**Drift speed** covers 0–200%; zero stops translation. **Drift speed variation** varies individual speeds, and rotation has separate controls. Intensity, size and quantity affect different aspects of the scene; begin with small changes. Glow follows the artwork's silhouette. Artwork remains alive until its rotated image/glow footprint clears the visible bounds; resize updates the scene's geometry rather than asking you to reset the pattern. These effects describe appearance, not model activity.

**Edge hardness** changes rounded versus square UI edges; font, density, readable width and text scale control readability separately. LCARS's **Mode** includes **Status Sweep**. Matrix/Emoji Rain offers its own quantity, speed, spread, bounce and optional emoji controls. Controls belong to the selected effect; they are not one universal slider set.

## Google Emoji Drift artwork

Google artwork remains included whether the unified-pool option is on or off. **Use one emoji pool** is on by default. When enabled, each individual Google or EmojiKitchen artwork has equal probability within the combined pool. The EmojiKitchen catalogue is much larger, so it naturally contributes more entries; this is not a 50/50 split between the two sources.

**Emoji Kitchen likelihood** ranges from 0–100% and defaults to 50%. This control is inactive while **Use one emoji pool** is on and becomes active when the unified pool is turned off.

## Accessibility and recovery

Typography and density are independent readability controls. Check keyboard focus and text contrast after choosing an accent. Reduced-motion preferences are respected where supported; choose Solid for a fully still background.

If an effect performs poorly, reopen Appearance and reduce or disable it. If a preference differs on another browser, remember Appearance is browser-local. [[Accounts and Models]] and [[Agent Settings and Compaction]] cover model and runtime preferences.
""",
    "openclank-docs-recovery": """# Recovery

Preserve the current draft or working state before trying to repair a failed save. A retry that overwrites newer work is not recovery.

## A save was refused

Read the error. A revision conflict means another writer changed the stored resource; a recovery error means the required preimage could not be captured. Keep your draft, reopen the current stored version and merge intentionally. For a recovery-service failure, restore service health before retrying.

## A document cannot decode

If Editor shows a recovery panel, inspect the preserved source or restore a known prior version. Do not replace it with an empty document merely to dismiss the error. Failed decoding does not authorize silently rewriting the original bytes.

## Restore a document or image project

Open that resource's History, inspect the intended checkpoint and use its restore action. Reopen the resource and confirm the recovered state. Restore is itself a recorded operation where supported; the available retained history determines what can be recovered. History covers supported managed operations, not every external side effect or every file on the host. It is not a substitute for backing up the installation's data directory. Read the restore preview and verify the selected resource before confirming.

## Missing files or inaccessible media

Check the selected account, Files folder and active filters. For host resources, check location availability and File access policy. A moved or expired remote resource may require relinking or adoption into Files. Do not assume a URL or browser draft is a durable backup.

## Chat and task failures

Reopen the original chat/task and inspect the last tool result. Check the selected provider and whether the run stopped or only its window closed. Avoid repeating a possibly completed external action until its outcome is clear. [[Usage and Stats]] helps distinguish unavailable from reported usage.

## Official pages and preferences

Reopen an official page for the maintained documentation. Personal documents remain separate from official updates. If settings differ, inspect the panel's scope: theme appearance is browser-local, launcher visibility is account-owned, several controls are shared or admin-owned, and provider connections belong to your account.
""",
    "openclank-docs-limits": """# Limits and Platform Support

This handbook distinguishes shipped implementation from a capability observed on the current installation. Provider entitlement, OS permissions, runtime availability and storage backend can limit a supported workflow.

## Platforms and deployment

This is **Beta 1, the first of several betas**. The macOS15+ Apple Silicon package has passed native and menu-bar Open/Quit qualification. Its pyramid-and-eye menu owns the app lifecycle, without a Dock icon. It is ad-hoc signed, not notarized; Intel/universal packages are not claimed. Windows source Setup/Check, authenticated startup, Files, Editor save/reopen and History restore, native PNG/JPEG/MP4 thumbnails, and host application dispatch are working and tested. Shared fixes were also tested on macOS. The tested source setup used x64 Python with ARM Engine and native sidecars; native ARM64 and x64 frozen release packages still need qualification. Linux release support is coming soon and remains unqualified. Application downloads become available when the v1.0.2-beta.1 release is published; the verified offline artwork is available separately in v1.0.2-beta.1-artwork. Host compatibility still depends on the selected Python/runtime versions and optional operating system dependencies. GPU model serving depends on the runtime, drivers, model format and available memory. Docker is not officially supported or tested. Retained Docker files and instructions are unsupported legacy reference, not an official installation path or a Beta 1 release qualification requirement.

Desktop capture/OCR and host-opening adapters are macOS-focused. Their implementation does not prove that permission was granted or a particular application launched on your host. Use the availability/error state.

## Where your work goes

Self-hosting lets you choose where the application and its configured stores run. When you connect a remote model, search service, mailbox or MCP server, that service receives the requests and task material sent to it. Review its access, data handling and billing before use. A local app window does not make a remote request local or prove that the entire workflow can run offline.

Local-model execution still needs its installed runtime and supported model, and some workflows require network services. Keep durable work and backups in the appropriate configured stores; conversation recall, a provider account and a browser draft are not substitutes for those files.

## Data and rendering

Strict JSON cannot contain comments. Annotate it in a document or supported property. Native Copal notes are structured records; Markdown is an editing and interchange projection, not the entire native representation.

The renderer supports headings, emphasis, marks, code, lists, static task lists, quotes, tables, wikilinks, section embeds and resolved media. Arbitrary HTML/JavaScript, Mermaid execution and plugin code are not promised as active document content. Source reveal belongs to the formatting demo; it does not make every official page editable.

## Installation-dependent behavior

Official content has stable identities and read-only pages. Shared installed body references resolve to installed canonical content. Current hosted Copal uses its Files-backed vault; no runtime storage selector is offered. Updating the handbook or source does not migrate personal pages or an older installation’s retained store. Stable official IDs let an official page receive a new revision while personal documents keep their own content.

Native Logs and Settings → Logging are shipped. Logs provides owner-scoped session browsing, literal search, source inspection and available capture details. Advanced request capture remains off until explicitly enabled in Logging settings. [[Usage and Stats]] covers the delivered controls and distinguishes unavailable or unscored evidence. [[Workspace Hexes]] covers the shipped Workspace/General library controls and their separate authority.

No cross-account sharing is granted by a document link. Reading a recipe does not run a model, save a setting or launch a task.
""",
}

ADDITIONAL_ARTICLES = (
    ("openclank-docs-setup", "Setup and First Run", """# Setup and First Run

Use the packaged app or the source alternative below. Local model serving is optional; configure providers through Settings → Add Models.

## macOS app package

On Apple Silicon Macs running macOS15 or later, install Open-Clank-1.0.2-macos-arm64.dmg from the application release when published. Copy OpenClank.app to Applications. The package has passed native and menu-bar Open/Quit qualification; it includes private Python, Engine and native workers, so no checkout or system Python is required. It is ad-hoc signed and not notarized; no Intel/universal qualification is claimed.

Open the app and use its pyramid-and-eye menu-bar icon: **Open** shows the browser UI; **Quit** stops the server owned by that app. There is no Dock icon. Open uses the configured loopback address, normally http://127.0.0.1:7777. Reopening retains user data. Custom packaged launch settings use the per-user macOS launch profile described in the repository Setup Guide; a source checkout's terminal settings do not configure an unrelated Finder launch.

Download all five pinned artwork parts and emoji-assets.parts.json from the available [supporting artwork release](https://github.com/Plaer1/open-clank/releases/tag/v1.0.2-beta.1-artwork). Keep them together and run the packaged command:

```bash
"/Applications/OpenClank.app/Contents/Resources/runtime/openclank" assets assemble --parts /path/to/emoji-parts
"/Applications/OpenClank.app/Contents/Resources/runtime/openclank" assets verify
```

Artwork installs into writable user data outside the sealed app. Missing artwork gives an install hint; no runtime CDN fetch supplies it.

## Windows release package

The planned per-user installers are Open-Clank-1.0.2-windows-x64-Setup.exe and Open-Clank-1.0.2-windows-arm64-Setup.exe. Final native installer and artwork qualification remains pending; use only architectures actually offered when the application release is published. The beta is unsigned. Each package includes private Python and native workers, with no global Python/PATH changes. ARM64 payloads are native ARM64; the x64 installer bootstrap uses Windows ARM emulation.

Setup can download verified artwork or use the five existing parts and manifest. Start menu **Open Clank** opens the local browser app; **Stop Open Clank** stops its owned server. Uninstall preserves user data and artwork. ZIPs remain an alternative; keep the complete extracted folder together and use `openclank.exe assets assemble --parts <directory>` then `assets verify`. See the repository Setup Guide for exact release assets and commands.

## Native source setup

Use Python 3.11 or later, Rust/Cargo and the platform C/C++ build tools. macOS needs Xcode Command Line Tools; Windows needs Visual Studio C++ Build Tools and the Windows SDK. Before first launch, obtain the matching offline emoji parts and manifest following the repository Setup Guide. The verified artwork is available in the supporting release v1.0.2-beta.1-artwork; a source clone alone is incomplete.

From the checked-out project, with the matching part files already supplied:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python scripts/emoji_asset_bundle.py assemble --parts /path/to/emoji-parts
python setup.py
openclank server start
openclank server status
```

Setup builds/verifies the managed engine and builds the native memory and History workers from the locked source. macOS and Windows also build Files and the platform thumbnail helper. Python requirements alone do not provide these workers. Keep Cargo and the platform compiler available for setup; the first build may take several minutes. Linux remains unqualified and its native Files worker is not built.

On macOS the repository also provides `./start-macos.sh`, after the same prerequisites and artwork assembly. The default browser address is `http://127.0.0.1:7777`. Use the configured port if it differs. `openclank` opens the terminal interface and starts the local service only after a genuine loopback connection refusal; explicit server lifecycle commands make the service state easier to inspect.

This recipe is for a **fresh installation**. On an existing installation, read the repository setup/upgrade guide before running setup. The first-run script is not a universal migration or repair command. Windows source installation is tested; native ARM64 and x64 frozen packages still need qualification, and Linux remains unqualified. See [[Limits and Platform Support]].

## First sign-in

Packaged installations use mandatory browser first-run setup to create the initial administrator, then require normal sign-in. Source setup creates the configured administrator and prints a temporary password to the local terminal when credentials were generated for a non-interactive setup. Interactive setup asks you to create the initial administrator directly. Sign in to Open Clank and change the password through Account settings. This application account is separate from a provider subscription login or API key; configure model access afterward. Keep credentials out of documents and screenshots.

Open Settings → Add Models and choose a Local, API, or Subscription setup card, then select a model in Chat. Added Models is the inventory of connected models. The application can run without serving a local model; a connected API or remote runtime supplies model execution.

## Update a source installation

Use the same checkout and virtual environment that own the running service. Read the release's upgrade instructions and capture recoverable backups of the configured stores and host workspaces before changing source. Stop the service that owns that installation, then update source and dependencies:

```bash
openclank server stop
git pull
source venv/bin/activate
pip install -r requirements.txt
openclank server start
```

Run setup or an explicit migration only when the applicable release guidance calls for it. Read release-specific migration notes before starting a new version. Do not delete the data directory or repoint the vault to try to repair an upgrade. See [[Recovery]] and the repository's Backup & Restore guide for recovery boundaries.

## Docker (unsupported legacy reference)

Docker is not officially supported or tested. Docker/Compose files and the repository setup notes are retained only as unsupported legacy reference. Use the native/source installation paths; Docker is not an official installation option or a Beta 1 release qualification target.

## When startup fails

Check `openclank server status` and the launch error before starting another instance. A port collision, missing dependency or unreachable model server needs its own correction. Binding beyond loopback is an administrator deployment choice and requires the intended authentication/network policy.

The app can run without a local model server. Provider entitlement, network reachability, OS access and selected model determine which actions work. [[Limits and Platform Support]] records these boundaries and [[Accounts and Models]] covers model setup.
"""),
    ("openclank-docs-agent-settings", "Agent Settings and Compaction", """# Agent Settings and Compaction

Open [Settings](clank://settings) → **AI Defaults**. Use **Native context and checkpoint policy** for the native context and compaction settings. These preferences belong to your account and affect runtime policy, not appearance.

## Change a policy deliberately

1. Inspect whether the native settings show inherited defaults or a saved override. Read the effective-value status as well as the input fields.
2. Adjust the tool/round limits or native compaction/checkpoint controls that are relevant to your workload.
3. Save, then check the readback. Saved changes apply at the next admitted turn; the turn already running retains its admitted policy.
4. Use the native reset action to return to inherited native defaults.

## Native context and checkpoint policy

Automatic compaction reduces the model's active context when it grows. Pruning, retained tail turns, recent-token preservation and reserved context control what stays available for the next request. Use **Advanced checkpoint controls** for checkpoint thresholds, writer-failure limit, reserve and fork/push policy.

The effective display distinguishes hard context from usable context. Entering a larger number cannot grant a model a larger context window than its route supports. A saved override with pending/unavailable readback is not evidence that the runtime applied that value.

## What to keep outside model context

Save exact decisions and files in Copal or the workspace. The conversation archive retains history separately from the compacted request context. Compaction is not deleting semantic memory, changing Lore retention or resetting Usage totals. See [[Memory and Lore]].

If context pressure or checkpoint writing fails, inspect the reported state and preserve the useful work. Return to defaults before tuning several controls at once; a reset does not restore a deleted file.
"""),
    ("openclank-docs-search-media", "Search and Media", """# Search and Media

Search and media features depend on the selected provider, installed tools and administrator search policy. A fluent answer without returned sources is not a grounded search result.

## Ask for evidence

Give the assistant a concrete query and ask it to retain source links. Open the returned sources and check that they support the claim. Supplied search citations, extracted page material and model-written prose have different provenance; preserve that distinction in your document.

If search is unavailable, inspect the route/error and the administrator's Search settings. An explicit provider/account choice should not be treated as permission to silently rotate to another service. Some configured routes have no supported search capability or need separate entitlement.

## Deep Research

Open **Deep Research** from the sidebar or rail and describe a focused question. Review its configured model/search route in **Settings → AI Defaults** and **Search** before starting. Follow the run's progress, sources and saved report in the Research collection; reopen the report and source cards to distinguish retained evidence from model-written conclusions. Extraction timeouts, blocked sites, unavailable providers and bounded source coverage can leave gaps. A finished report is not proof every claim is true.

## YouTube transcripts

Supply a video URL and the passage or question you need. Transcript retrieval can select useful timed passages from the acquired captions and return timestamp links. Caption provenance can be manual, automatic or translated; check the language/provenance rather than presenting all text as the speaker's exact original wording.

A missing or inaccessible transcript is an explicit unavailable result. Search discovery and caption retrieval are different operations; a retrieved transcript does not prove browser-based video discovery succeeded.

## Save reusable material

Review returned media, then use the supported save/adopt action to retain it in Files. Keep its source URL and relevant provenance beside the retained resource. Remote media can expire; a retained Files resource is the durable reference for a Wiki embed.

In Editor, insert the resolved image/audio/video/PDF reference and reopen the page to confirm it loads. Missing media shows a diagnostic. [[Wiki Recipes]] demonstrates the syntax using your own accessible resource, and [[Usage and Stats]] explains why a tool request and model tokens are separate evidence.
"""),
    ("openclank-docs-desktop-host", "Desktop Capture and Host Opening", """# Desktop Capture and Host Opening

These features bridge the browser app and the macOS host. Their availability depends on OS permissions and eligible resources; they are not unrestricted access to the desktop.

## Capture and OCR

Request desktop capture explicitly for the task that needs it. The supported flow requires a distinct opt-in and macOS screen-recording permission. It is task-triggered capture, not background recording.

Review the captured image and retained Files artifact. OCR can attach text, boxes and transforms/crops to that capture; check the recognized text against the image before using coordinates or correcting a document. OCR output is an interpretation of pixels, not guaranteed source text.

If permission is denied or capture is unavailable, inspect macOS privacy settings for the process running Open Clank and retry only after the intended permission is granted. This handbook has not itself captured your desktop.

## Open a resource on the host

In Files, select an eligible host-backed resource and use its host-open action. The app resolves the resource through Files access policy and the available native application choices. Canceling the picker leaves the resource unchanged.

A Copal-only record or unsupported resource may have no host-open action. Missing/stale resources and missing applications should be reported instead of treated as a successful launch. Opening in Editor and opening in a host application are distinct choices.

If native opening fails, confirm the host resource still exists and is permitted, then inspect application availability. Browser actions do not grant arbitrary filesystem access. See [[Files and Imps]] and [[Limits and Platform Support]] for the capability boundaries.
"""),
    ("openclank-docs-usage", "Usage and Stats", """# Usage and Stats

Open **Usage** from the sidebar or compact rail, or [Usage](clank://usage). Its pages are **Sessions**, **Usage**, **Activity**, **Trends** and **Quality**. **More** adds **Account quota**, **Pinned conversations**, **Archived conversations** and **Advanced inspector**. Opening it does not send a model request; content analysis has a separate opt-in.

## Find a conversation and its evidence

1. Choose **Sessions**. Set the date range and **Usage timezone**, then use available provider, model, account and workspace filters.
2. Select a saved session to inspect its messages, tools, source parts and available transport attempts. Search matches literal saved text; **Open matching conversation** returns to the source conversation.
3. Check the displayed coverage, source identity and timestamps. Pinned and archived views are list scopes, not another owner's archive.
4. Use the offered session/export action when you need a retained copy. **Export CSV** exports numeric data for the selected Usage scope; inspect the separate session export options for content.

Back/forward navigation preserves useful scopes. Chips show active filters; clear them before treating an empty result as missing data. Timezone changes which display day a timestamp belongs to. Partial, bounded or unavailable sources remain labeled; an empty collection is not proof of no activity.

## Usage and Activity

**Usage** switches between **Cost** and **Tokens**, with supported grouping, chart style, attribution and comparison controls. Input/output, cache and reasoning categories remain distinct where their producer provides them. Reported values, estimates and unknown values are different evidence. Missing usage is unknown, not zero; estimated price is not an invoice.

**Activity** shows contributions and activity over time with Top Sessions. Choose messages, sessions or output tokens and a day/week/month resolution. Selecting a day narrows Sessions to that display day. A heatmap visualizes retained observations; it does not measure every action on your computer.

**More → Account quota** shows supported account-limit evidence and coverage. Unlike limit windows need not yield one meaningful overall percentage. Provider stats/usage buttons open that provider's official account page; they do not import a complete external account history into Open Clank.

## Trends and Quality have different scopes

**Trends** analyzes opted-in content across the owner's archive. Its notice explains that date and identity filters do not apply. Review the opt-in and bounded scan state before interpreting grouped phrases as complete history.

**Quality** uses selected dates across all identities, rather than the normal provider/model/account filter scope. Inspect each metric's evidence state; unscored or unavailable evidence is not a bad score or a successful test. Open an owned saved source session when the UI provides it. A chart is not a substitute for reading the task result.

## Always-on baseline and optional advanced capture

Open **Settings → Logging**. Ordinary Open Clank activity, saved conversations, tool evidence and numeric usage logging are **always on**. Advanced provider logging starts **off** and adds optional proxy/transport details for **Open Clank operations only**. It does not capture other apps or import an external provider's full history.

When enabled, supported formats can expose measured attempt milestones, headers and sanitized request/response content. Credentials, authentication fields and cookies are excluded, but sanitized bodies can contain your work. Viewing a body requires an explicit action. Export omits bodies unless you choose them. Capture gaps and unsupported format details stay visible; enabling advanced logging does not recreate earlier missing bodies.

Review retention previews before saving policy or pruning. Logging policy, Stats totals, semantic memory and Lore retention are separate. Owner scope still applies to session search and exports. See [[Memory and Lore]] and [[Recovery]] before repeating a request whose outcome is uncertain.
"""),
    ("openclank-docs-wiki-recipes", "Wiki Recipes", """# Wiki Recipes

These recipes use the Wiki page workspace, shared document core and native resource resolver. The official guide is read-only. To practice, choose **New page** in Wiki and use that personal page's normal save controls.

## Open Wiki and preserve native data

Wiki is a distinct applet with page navigation and rich authoring over the shared Copal document core. Its launcher is **hidden by default**. To show it, open **Settings → Appearance → Copal navigation** and enable **Wiki**. This account-owned visibility control does not remove Wiki data or agent access. **Settings → Help → Wiki documentation** still works with the launcher hidden.

Choose **New page**, write in **Rich**, inspect **Source**, then use **Read** to check the result. **Open in Editor** keeps the same record, shared draft, save state, history and attachments. Saving in one presentation can update the other; opening both is not creating two independent copies.

The Library menu's native `.memes` import/export preserves the structured page representation. Markdown is a useful projection/interchange format; it cannot represent every native property, typed field or relationship. Unknown native fields remain preserved. Verify the exported result instead of assuming Markdown round-trips the entire record.

## Link a page and embed a section

This live link opens [[Editor]]. This section embed reuses the corresponding canonical handbook text:

![[OpenClank/Editor#Source comments]]

Insert this example in your new personal page:

```markdown
[[OpenClank/Editor|Read the Editor guide]]
![[OpenClank/Editor#Source comments]]
```

Save and reopen your personal page. A link navigates to its target; an embed shows the selected section. Renamed/missing targets or headings can become unresolved. Inspect the diagnostic and repair the target instead of duplicating a whole article just to hide a broken reference.

## A small decision table

| Question | Decision | Evidence |
| --- | --- | --- |
| Where does the page open? | Wiki; Open in Editor keeps the same record | [[Editor]] |
| Is the official guide editable? | Read-only; practice in a new personal page | [[Recovery]] |
| Where are totals? | Usage | [[Usage and Stats]] |

Add this table to your personal page, edit a cell, then save and reopen it. To practice a typed table or Wiki conversion, use Editor's document-type controls and inspect the resulting structured view. Markdown rendering alone is not conversion.

## Media with an accessible resource

Attach a real resource from Files and insert its reference. For example, replace the illustrative name below with the resource you actually own:

```markdown
![[My photo.png|640]]
![[My recording.webm]]
![[My reference.pdf]]
```

These names are examples, not supplied assets. The resolver chooses image, audio, video or PDF presentation from the resolved resource. Check loading and error states; a raw remote URL is not automatically retained in Files.

## Source comments and a reusable template

For source-comment practice, create an editable source file with an ordinary supported native comment. Click its rendered prose, edit in the inset, and leave it to apply the local change. Check Unsaved, choose Save, then reopen to verify the source. Right-click reveals the raw comment directly. See [[Editor]] for pending unsafe edits and syntax limits. Use **Choose template folder** for host templates, or mark a Copal note’s type property `template`. Inserting that template should create content in the current document rather than modifying the template itself.

For read-only source reveal, open [[Markdown Formatting Demo]], select a rendered element, copy its source and use **Back to rendered**. That inspector does not edit this page or run embedded code.

## Follow the connections

Open [Graph](clank://graph) after saving personal links, or [TreeHouse](clank://treehouse) for guided discovery. Official pages stay immutable. Task-list boxes are rendered examples; arbitrary HTML/JavaScript and Mermaid are not active plugins. Opening any recipe does not dispatch a model, create a task or change Settings.
"""),
    ("openclank-docs-treehouse", "TreeHouse and Field Guide", """# TreeHouse and Field Guide

Open [TreeHouse](clank://treehouse) for guided app discovery. The existing Field Guide and contextual Help point into the same supported app surfaces as this handbook.

## Follow one lesson

Choose the feature you want to learn, follow its destination and try the action on personal material. Wiki owns page navigation and rich authoring; Editor offers document tabs, splits, tables and source. They share canonical identity and buffers. Files owns image browsing and Imps work.

Return to the guide to continue. Lessons and achievements are learning state, not proof that a provider request, backup or save completed. Inspect the actual resource or task outcome for that evidence.

## Achievements

Open TreeHouse's **Achievements** section and use **Earned**, **All** or **Locked**. Its count and detail describe recorded awards, while Courses, Skills and Assignments describe learning progress. First use can earn **open your clanker**; the shell's account-aware listener can deliver a newly earned notice even when the TreeHouse window is closed. Award messages open their original achievement detail. A visit award proves that visit, not every feature's backend or provider operation.

The catalog records a limited set of account-wide achievements from specific committed work or an acknowledged UI event. Open the achievement's description to see what evidence it requires. A lesson page, screen visit or pending operation does not count as a completed save or successful provider request; only the recorded earned state confirms an award. Some entries are shown as unrevealed mystery items until the account earns them.

Open **Settings → Advanced → Achievements** for **Enable system notifications** and **Test system notification**. In-app notices remain available; system popups need this browser's permission and can still be suppressed by browser/OS settings.

**Reset achievements** clears your account's achievement progress, receipts and pending unlock notices after confirmation; new activity can earn them again. Courses, source activity and other accounts are preserved. TreeHouse's **Reset my progress** is separate: it resets the visible course progress, submissions, evidence and course completion awards, while account-wide House achievements remain. Read the confirmation's scope before resetting.

## Help beside the work

Use contextual Help for the active surface, then follow the corresponding handbook page for a longer explanation. [[Wiki Recipes]] and [[Markdown Formatting Demo]] provide reusable examples without a second curriculum engine or a separate official document corpus.

If a guide destination is unavailable, open the intended surface through the launcher and report the stale link. Do not recreate a retired applet to follow old instructions. [[Getting Work Done]] provides a current map.
"""),
    ("openclank-docs-workspace-hexes", "Workspace Hexes", """# Workspace Hexes

Workspace Hexes describe the active contract for a selected project. The workspace's `.clanker/hexes/contract.yaml` is canonical; its generated AGENTS.md digest is a readable view of that contract.

## Inspect before changing a file

From the intended workspace, use:

```bash
openclank hex explain path/to/file
```

Read the rules that match the path before creating or changing it. The exact activated contract hash is the authority for runtime mutations; a familiar rule title or stale digest is not a substitute for that active contract.

## Maintain the contract

Edit the canonical contract using the workspace's required recovery and approval rules, then use `openclank hex sync` to regenerate its digest. Project-owned checks live under `.clanker/hexes/checks/` and supplement built-in checks. They do not replace OS process confinement with an in-app broker.

Keep recoverable preimages before changing uncommitted or untracked work when the contract requires them. [[Memory and Lore]] explains why file recovery and active instructions are distinct authorities.

## Keep the workspace understandable

Use the selected workspace's contract and documented layout rather than inventing a second authority. Open Clank's own canonical layout is:

| Path | Intended material |
| --- | --- |
| `.clanker/futures/` | Markdown plans and their execution slices |
| `.clanker/futures/differred/` | Deferred work when that workspace uses this convention |
| `.clanker/robonotes/` | Focused notes/evidence with concise topic indexes |
| `.clanker/hexes/` | Canonical contract and optional project-owned checks |
| `.clanker/tools/` | Explicit local operator/conversion tools |
| `.clanker/references/` | Temporary study material, never tracked in Git |
| `.clanker/archive/` | Deliberately retired material preserving relative structure/provenance |

Every singular `.clanker` directory in this repository is private and Git-ignored, including contracts, tools, plans and notes. It is also excluded from image/release exports. These names are a workspace convention, not automatically created resources or permission to archive/delete. Follow the actual contract before writes. Reference payloads must not become first-party runtime/build dependencies; keep durable provenance outside the reference payload. New Clanker writes use singular `.clanker/`; historical plural paths remain compatibility input.

## General instructions

In Files, select the intended workspace and choose **Hexes** from that workspace's contextual menu. This opens the selected project's active contract; the retired global Hexes toolbar button is not the entry point. Open **Settings → Hexes** and select **General** for your reusable instruction library. An empty account starts with an empty General bin.

Use **New General Hex** to write a title and declarative instruction. With no tags it applies globally across your account's contexts. Positive tags are **Experimental**: any matching Workspace or Task tag makes the instruction applicable. Tags filter applicability; they never grant file or process authority. Clearing the last tag shows a Global summary before you save.

The preview explains which revisions will apply at the next turn or operation boundary. An already running operation retains its pinned revision snapshot. Use the **Authorized workspaces (n)** chooser to select workspace scope. Review the target contract hash before choosing **Activate**. After activation, use **Promote to General** when the instruction should also be kept in the General library, which can retain multiple independent entries. Search/tag filters only filter the library list.

Review explicit workspace promotion/application choices before confirming. Exports/imports carry source and scope information; review an agent-assisted instruction and accept its revision before it becomes eligible. A General instruction cannot override mandatory workspace rules or OS confinement.

"""),
    ("openclank-docs-app-windows", "App Windows and Navigation", """# App Windows and Navigation

Applets use thin frames and keep the main navigation available. Maximize fills the workspace beside the navigation; minimize/restore and resizing retain the applet’s working state. Window arrangement does not save a dirty document.

The shell's hamburger opens/closes the main sidebar; the compact rail offers the same available destinations. Appearance can hide launcher shortcuts. A hidden shortcut does not remove its data or tool access.

## Arrange the work

Open Files or Editor and use the menu directly below that app window's title bar. **File**, **Edit**, **Selection** where available, **View**, **Go**, **Tools**, **Window** and **Help** describe commands for that surface. Focus the intended window, pane or text field before choosing an action; enabled commands and their unavailable reasons depend on that captured target.

Drag an app window's title bar to move it and its supported edge/corner handles to resize it. **Minimize** puts the window in the bottom dock; select its dock item to restore and focus it. Chat can pop out into its own window and return through these controls. Closing a window dismisses the view; stopping a model/task uses its Stop control. Inspect saved state before closing when a save is still pending.

## Choose a split or another window

In Files, **File/Window → New Files Window** makes an independent browser. **Window → Split right**, **Split below**, **Focus other pane** and **Close split** manage two panes. Each pane retains its navigation/selection; Files has no tabs. Drag the split divider to change the space they share.

Editor has tabs and tab groups. **Window → Split editor right…** or **Split editor below…** asks which document to show. Ordinary navigation uses the current tab; Command/Control-click or a supported new-tab action keeps parallel work. **Go → Previous document** and **Next document** navigate Editor history; Explorer Back/Forward navigates folders.

Open a page through **Open in Wiki** or **Open in Editor** to keep its shared identity. Two views of that record share its buffer and save state. Window arrangement is not duplicating or backing up the document. See [[Editor]], [[Wiki Recipes]] and [[Files and Imps]] for the actual saved-resource actions.
"""),
    ("openclank-docs-source-editing", "Source Editing", """# Source Editing

Open a writable text file from **Files → Open** in Editor, or use Editor's **File → Open file…** and select it in the Files dialog. A host workspace stays a host workspace; opening it does not import every file into Copal.

## Identify the language

The status bar's **Language mode** chooser identifies the active document. Automatic detection uses names/extensions and supported text signatures; choose an explicit mode when an ambiguous file needs one. Check the loading or fallback status before assuming a grammar loaded. A mode override changes presentation; it does not convert the file or rename its extension.

The available modes have different syntax support: grammar-based parsers, grammar dialects, stream modes, lexical highlighting and plain text. Web, scripting, data and configuration languages sit alongside game/text/shader families: Godot GDScript/resources/shaders, Unity YAML/UXML/USS/ShaderLab, HLSL/GLSL/WGSL and Unreal Shader, Lua/Luau, GameMaker, Ren'Py, PICO-8, Papyrus, Valve formats, Ink, Yarn Spinner and Twine/Twee. The current chooser is the exact installed inventory. Some ambiguous asset extensions need recognizable text; binary/compiled game assets are not text source.

Highlighting does not provide language-server completion, semantic errors, debugging, compilation or engine execution. Strict JSON remains strict JSON; choose JSONC only for a file that permits comments.

## Edit several places

1. Focus an editable source view and select the text to repeat.
2. Use **Selection → Select next match** to add another occurrence or **Select all matches** for matches of the primary selection.
3. Use **Add cursor above** or **Add cursor below** for adjacent lines. Typing applies to the active selections; inspect all ranges before saving.
4. Choose **Keep primary selection** to return to one selection.

The right-click menu offers these same actions. Disabled items explain their requirements; a rendered reading view or read-only official article cannot be used to mutate source. Selection operations do not by themselves save.

Use **Edit → Find and replace…**: enter Find/Replace, then **Find next**, **Replace** or **Replace all**. Review the reported count and resulting text. If the captured target changes, reopen the dialog for the intended document.

## Local spelling and comments

Use **Tools → Check spelling locally** or the text context menu's **Check selected spelling locally**. Suggestions and **Add word to dictionary** / **Remove word from dictionary** use the local spelling service and the account dictionary. **Retry spelling** appears for a recoverable unavailable service. Dictionary suggestions are not grammar checking or model review; code names and unsupported languages can be false positives.

[[Editor]] covers rich source comments and templates. Comment controls are qualified by the active source format; not every language supports native rich comments. Save, reopen and inspect the file to verify persisted edits. For a revision conflict preserve the draft and follow [[Recovery]].
"""),
    ("openclank-docs-calendar-email", "Calendar and Email", """# Calendar and Email

Open **Calendar** from the **Copal** launcher group and **Email** from its available sidebar/compact-rail destination. Calendar’s launcher placement does not change its saved events or configured CalDAV connection. Mailbox login, model subscription login and Open Clank account login serve different purposes. Connections and external actions depend on the configured service.

## Keep an event

In Calendar choose **New** (New event), select the intended calendar and enter the title, start/end and relevant details. Save, then open the event again in the chosen day/week/month view. **New calendar** and **Import .ics** serve local/imported material; CalDAV needs a configured integration. Refresh reads current data; an enabled connection is not proof of a completed sync.

For a recurring event, inspect its recurrence and exceptions. Editing or deleting can distinguish **This event only** from **All recurring events**. Check that scope before confirming. A Calendar event and a scheduled model task are distinct saved records.

## Reminders

Use the reminder controls offered by the event or ask explicitly for a reminder with its date/time. Review the saved reminder and notification channel under **Settings → Reminders**. Browser delivery needs browser/OS permission; configured email or webhook delivery has separate prerequisites. A saved reminder is not proof a notification reached its destination.

## Read and prepare mail

Open **Settings → Integrations** to add, edit or test a supported mailbox. You can also reach it through **Settings → Email → Open Integrations**. Select the account in Email and inspect the inbox/error state. Google OAuth is a supported path when its configuration is available. **Microsoft OAuth and Graph Mail are not implemented here**; a mailbox that requires them cannot use the IMAP/SMTP password form instead. See the [mail setup guide](https://github.com/Plaer1/open-clank/blob/main/docs/email-outlook.md) for connection requirements and Microsoft limits. Model-provider sign-in does not authenticate a mailbox.

Open a message, use Reply/Forward or Compose, review recipients, text and attachments, and keep the draft before sending. Existing Odysseus email compose/document compatibility remains available; preserved drafts and attachments are not moved by handbook updates. A draft is not a sent message. After a deliberate send inspect its result/sent state before retrying, since another send can duplicate delivery.

## Email AI, labels and account settings

Open a message and choose **Summary** to inspect an available cached summary or request one, or **AI reply** to prepare a suggested reply. Read the source message and check the generated text, recipients and attachments in the draft before a deliberate send. A cached result is previous analysis; a new model-assisted request needs the configured model and can use its quota or incur cost. A summary or draft is not proof of delivery.

Use **Settings → Email → Open Email Settings**, or Email's own settings, for the selected mailbox. **Email Reply Writing Style → Writing style → Save** retains the style instructions used for AI replies. **Extract** analyzes sent mail from that account and saves a style prompt; review the extracted instructions before relying on them. This requires accessible sent mail and the configured analysis route, and a style prompt does not guarantee an accurate reply.

Email can display analysis-supplied tags and spam verdicts. Use the tag filters to inspect categories such as Urgent, Action needed or Spam, then read the actual message. A spam label is a verdict to review; **Not spam**, where offered, corrects that flag. Automatic labels are not guarantees about a sender or the contents of a message.

**Auto Reply** is separate from an AI reply draft: it can send holiday/away replies to incoming mail. Review **This account**, Start date, End date, Subject, Message, **Send to same sender** and **Skip automated and no-reply senders** before enabling and saving it. Its mailbox connection and notification settings must be available; inspect results rather than assuming every incoming message received a reply. **Newsletter Unsubscribe → Clean** lists candidate newsletter/ad mail; review the candidate and chosen unsubscribe, spam or delete action before applying it.

**Settings → Integrations** holds configured external connections. Confirm the service, access and availability before relying on calendar/mail/tool results. Connection forms and setup screenshots prove configuration state, not successful remote access or delivery. See [[Tasks and Continuations]] and [[Recovery]] for scheduled work and uncertain external outcomes.
"""),
    ("openclank-docs-compare-cookbook", "Compare and Cookbook", """# Compare and Cookbook

These tools help choose connected models and manage local execution. A provider connection, downloaded model and running server are separate states.

## Compare connected answers

Open **Compare** from the sidebar or rail. Choose the connected models, enter the same useful prompt and start the comparison. Read each response and its provider/model identity before selecting or voting on a preferred answer. The retained conversation/results are the evidence; a selected card or an empty setup view is not an answered comparison.

Each participating request can use quota and incur cost. Unsupported input or tool capabilities can differ across routes. Check [[Usage and Stats]] for numeric evidence; a preference vote is not a general benchmark verdict.

## Manage local serving in Cookbook

Open **Cookbook** from the sidebar or rail. **What Fits?** uses the selected host/hardware information to suggest configurations. Review the host, model format, estimated memory and engine before using a download or serve action. A hardware fit estimate is not proof of successful execution.

Use the model's download/serve controls, inspect progress/errors and check the running endpoint before connecting it through **Settings → Add Models → Local**. Save a useful preset through the provided preset controls to reuse its configuration; reopening it does not mean its server is still running. Cookbook can show commands/dependency recipes: review them for the selected host before running them. Diagnosis describes the reported failure rather than guaranteeing an automatic repair.

Apple Silicon/Metal, MLX/Apfel, Ollama, llama.cpp and other listed engine paths have different dependencies and supported model formats. Availability is installation-dependent. Windows source support includes tested Files, Editor and native thumbnails. Native ARM64 and x64 frozen release packages and Linux remain unqualified. The browser app can operate with a remote/API model, while local execution needs its runtime; the whole application is not promised to work offline.
"""),
    ("openclank-docs-credits", "Credits and Included Components", """# Credits and Included Components

Open Clank builds on [Odysseus](https://github.com/odysseus-dev/odysseus), integrates Copal's document workspace, [MiMo Code](https://github.com/XiaomiMiMo/mimo-code) and [opencode](https://github.com/anomalyco/opencode), and [Epic Games' Lore](https://github.com/EpicGames/lore) history technology. Open Clank is distributed under AGPL-3.0-or-later; included components retain their own licenses and copyright notices. Copal's public upstream/author and separate package license are not established by the current source records, so no additional Copal license is claimed.

Thanks to [AgentsView](https://github.com/kenn-io/agentsview) for Usage/activity interface inspiration. [llm_intercept](https://github.com/mlech26l/llm_intercept) and [llm.log](https://github.com/lanesket/llm.log) are logging design-study sources, not distributed proxy products.

## Google artwork and Kitchen catalogue

Google supplies Noto Emoji and Emoji Kitchen artwork; [Xavier Salazar's catalogue](https://github.com/xsalazar/emoji-kitchen) supplies Kitchen combination metadata. Noto SVG artwork uses [Apache-2.0](https://github.com/googlefonts/noto-emoji/blob/e20cbc2bbec1926686be9f9bee7d1d2cfa1fea0e/svg/LICENSE). Its separate OFL font notice does not apply to Kitchen mashups. The accepted Google/Xavier Kitchen attribution is retained without assigning a new mashup license. The local pack contains 146,983 available Kitchen combinations and 17 recorded upstream 404 exceptions. The existing Settings Help emoji credits card remains available.

## Editors, renderers, icons and fonts

Included editors/renderers use CodeMirror/Lezer, Shiki, Mermaid, spreadsheet/document/PDF/QR libraries, and nspell with dictionary-en/SCOWL. The dictionary has compound notices, not a single MIT label. Original shared Open Clank SVG icons are distinct from Lucide/Feather assets.

Font credits cover Fira Code, Inter, Comic Neue, OpenDyslexic and Fredoka. Liga Comic Mono has Comic Mono and Fira Code ligature lineage with an unresolved exact source/release chain. The file named `GohuFont.ttf` internally identifies as Untitled1, copyright 2025 Unknown; the older Gohu/WTFPL claim is unverified.

Detailed included paths, versions, asset fingerprints, retained full texts and provenance qualifications are in the repository's [Acknowledgments](https://github.com/Plaer1/open-clank/blob/main/ACKNOWLEDGMENTS.md) and [Bundled components](https://github.com/Plaer1/open-clank/blob/main/licenses/BUNDLED-COMPONENTS.md). These links identify the repository notice destinations; its public branch may lag the installed Beta 1 bundle until publication. Study references stay outside first-party runtime/build dependencies. This read-only article is part of the shared handbook opened by the existing two Help buttons.
"""),
)

# D02 binds these semantic identities to verified routable renditions. Do not
# turn an unbound identity into a fabricated static URL or a broken embed.
ARTICLE_ASSETS = {'openclank-docs-home': ('beta1-welcome-20261005', 'beta1-help-20261005'),
 'openclank-docs-getting-work-done': ('beta1-files-20261005', 'beta1-chat-retained-20261004'),
 'openclank-docs-accounts-models': ('beta1-account-20261004', 'beta1-models-20261004'),
 'openclank-docs-chat-workspaces': ('beta1-chat-retained-20261004',
                                    'beta1-shell-menu-20261004',
                                    'beta1-shell-dock-20261004'),
 'openclank-docs-editor': ('beta1-editor-20261005',
                           'beta1-editor-picker-20261004',
                           'explorer-customize-20261004'),
 'openclank-docs-files-imps': ('beta1-files-20261005',
                               'beta1-files-images-20261004',
                               'beta1-image-editor-20261004',
                               'beta1-image-result-20261004'),
 'openclank-docs-graph': ('beta1-graph-20261004', 'beta1-timeline-20261004'),
 'openclank-docs-memory-lore': ('beta1-memery-20261004', 'beta1-skills-20261004'),
 'openclank-docs-tasks': ('beta1-meatbag-tasks-20261004', 'beta1-clanker-tasks-20261004'),
 'openclank-docs-settings-themes': ('beta1-appearance-20261004', 'beta1-appearance-controls-20261004'),
 'openclank-docs-recovery': ('beta1-history-settings-20261004', 'beta1-history-detail-20261004'),
 'openclank-docs-limits': ('beta1-hexes-20261004',),
 'openclank-docs-formatting-demo': ('beta1-wiki-read-20261004',),
 'openclank-docs-setup': ('beta1-account-20261004', 'beta1-models-20261004'),
 'openclank-docs-agent-settings': ('beta1-permissions-20261004', 'beta1-advanced-20261004'),
 'openclank-docs-search-media': ('beta1-search-20261004', 'beta1-research-20261004'),
 'openclank-docs-desktop-host': ('desktop-host',),
 'openclank-docs-usage': ('beta1-usage-20261004',
                          'beta1-usage-detail-20261004',
                          'beta1-usage-overview-20261004',
                          'beta1-usage-quality-20261004',
                          'beta1-logging-20261004',
                          'beta1-usage-activity-20261004',
                          'beta1-usage-trends-20261004'),
 'openclank-docs-wiki-recipes': ('beta1-wiki-20261005',
                                 'beta1-wiki-source-20261004',
                                 'beta1-wiki-reopen-20261004',
                                 'beta1-templates-20261004'),
 'openclank-docs-treehouse': ('beta1-treehouse-20261004', 'beta1-achievements-20261004'),
 'openclank-docs-workspace-hexes': ('beta1-hexes-20261004',),
 'openclank-docs-app-windows': ('beta1-shell-menu-20261004',
                                'beta1-shell-dock-20261004',
                                'beta1-files-20261005'),
 'openclank-docs-source-editing': ('beta1-comments-20261005',
                                   'beta1-multicursors-20261004',
                                   'beta1-find-replace-20261004'),
 'openclank-docs-calendar-email': ('beta1-calendar-20261004',
                                   'beta1-mail-draft-20261004',
                                   'beta1-email-settings-20261004',
                                   'beta1-reminders-settings-20261004',
                                   'beta1-integrations-20261004'),
 'openclank-docs-compare-cookbook': ('beta1-compare-20261004', 'beta1-cookbook-20261004'),
 'openclank-docs-credits': ('beta1-help-20261005',)}

ARTICLE_EXAMPLES = {
    "openclank-docs-formatting-demo": ("formatting-source-reveal",),
    "openclank-docs-wiki-recipes": (
        "wiki-link-section", "wiki-table", "wiki-media",
        "wiki-source-comment-template", "wiki-graph-navigation",
    ),
}

ARTICLE_CONTENT["openclank-docs-formatting-demo"] = """# Markdown Formatting Demo

This page is the formatting demonstration. Click any rendered element below to
reveal the exact Markdown source that produced it. The source inspector is
read-only — you can select and copy from it, but it never edits this page and
never starts a save. Use **Back to rendered** to return.

## Headings

### A level-three heading

#### A level-four heading

## Emphasis and marks

Some **bold text**, some *italic text*, some ~~struck-through text~~, and some
==highlighted text==.

Inline `code spans` look like this.

## Lists

- First bullet
- Second bullet
  - A nested bullet
- Third bullet

1. First step
2. Second step
3. Third step

## Task list

- [x] A finished item
- [ ] An open item

## Quote

> A quoted line.
> It can span more than one line.

## Code

```
function hello() {
  return 'world';
}
```

## Rule

---

## Table

| Column | Meaning |
| --- | --- |
| Name | What it is called |
| Value | What it holds |

## Links and references

A [standard link](clank://settings) opens a real app screen.

A wikilink target such as [[Editor]] opens the canonical linked document.
Wiki and Editor offer different presentations of the same record.

## Footnote-style note

Everything on this page is ordinary Markdown. The only special behavior is
the read-only source reveal above, and it exists for this demonstration alone
— normal documentation pages simply stay rendered.
"""

EXISTING_ARTICLES = (('openclank-docs-home', 'OpenClank/Home', 'Open Clank Handbook', ('OpenClank/Start Here', 'OpenClank Handbook'), 0), ('openclank-docs-getting-work-done', 'OpenClank/Getting Work Done', 'Getting Work Done', (), 1), ('openclank-docs-accounts-models', 'OpenClank/Accounts and Models', 'Accounts and Models', (), 2), ('openclank-docs-chat-workspaces', 'OpenClank/Chat and Workspaces', 'Chat and Workspaces', (), 3), ('openclank-docs-editor', 'OpenClank/Editor', 'Editor', (), 4), ('openclank-docs-formatting-demo', 'OpenClank/Markdown Formatting Demo', 'Markdown Formatting Demo', ('OpenClank/Formatting Demo',), 5), ('openclank-docs-files-imps', 'OpenClank/Files and Imps', 'Files and Imps', (), 6), ('openclank-docs-graph', 'OpenClank/Graph', 'Graph', (), 7), ('openclank-docs-memory-lore', 'OpenClank/Memory and Lore', 'Memory and Lore', (), 8), ('openclank-docs-tasks', 'OpenClank/Tasks and Continuations', 'Tasks and Continuations', (), 9), ('openclank-docs-settings-themes', 'OpenClank/Settings and Themes', 'Settings and Themes', (), 10), ('openclank-docs-recovery', 'OpenClank/Recovery', 'Recovery', (), 11), ('openclank-docs-limits', 'OpenClank/Limits and Platform Support', 'Limits and Platform Support', (), 12))
