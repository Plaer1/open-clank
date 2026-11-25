"""Canonical handbook authoring data, consumed by the official_docs facade.

Author rule: -quiet, but with all required info. See docs/writing-the-manual.md.
Stable IDs address content; asset/example IDs address separately owned reusable
resources. They are references, not invented URLs or executable page content.
"""

ARTICLE_CONTENT = {
    'openclank-docs-home': """# Open Clank

Open Clank is a self-hosted AI workspace for turning ideas into finished work. Your personal agent works with your notes and files from day one.

[[Journal|Copal]] is an open-source alternative to Obsidian. Its built-in [[Editor|file editor]] handles text, code and images. [[Graph]] maps links between journal pages, dependencies in your workspace and the files your notes refer to.

Learn in [[TreeHouse]], a pre-alpha learning management suite. Track milestones on [Timeline](clank://timeline), [[Tasks and Continuations|schedule tasks]] and [[Calendar and Email|sync your calendar]].

{{manual-media:manual-graph-source-scope}}

[[Accounts and Models|Connect your first model]] · [[Chat and Workspaces|Start a project chat]] · [[Manual Index|Browse the manual]]
""",
    'openclank-docs-getting-work-done': """# Getting Work Done

Suppose an agent drew the wrong part of a reference image. Instead of describing “the thing in the corner,” open the saved image in [[Editor]], select the region and choose **File → Feedback on image region…**. The original chat receives a draft with the file revision and the region. Add the correction, review the recipient and send. [[Deliverables]] shows the same workflow for a chart, slide or moment in a recording.

Working on source? Give [[Chat and Workspaces|the chat a working folder]]. Open saved changes in [[Graph]] to follow a call back to its definition. If the next step belongs to you, keep it in [[Tasks and Continuations|Meatbag Tasks]]; if it should run later, schedule a Clanker Task.
""",
    'openclank-docs-accounts-models': """# Accounts and Models

Sign in to the Open Clank account that owns your chats and journal pages. Connect a model for that account in [Settings](clank://settings) → **Provider/Model Management → Add Provider**.

## Connect a model

- **Local** connects to a model server running at an address Open Clank can reach. See [[Compare and Cookbook]] for serving a downloaded model.
- **API** takes a supported endpoint and its credential. The provider’s API quota and billing apply.
- **Subscription** uses a sign-in method offered by the provider. Model availability follows that account’s entitlements.

If **Provider login** is waiting, choose **Open current sign-in**. Enter the **Sign-in code** if offered. For manual callbacks, use the authorization-code field and **Complete login** on the original flow. Return to **Add Provider** or **Model Management** to resume an unfinished sign-in. Use **Cancel login** to abandon it. If the result is unclear, check Model Management before starting again.

## Check the inventory and use the route

In [Connected models](clank://settings/added-models), expand the connection and check **Models**. An empty list can mean the local server is unreachable, the API credential or endpoint needs correction, or the subscription still needs sign-in under **Account & advanced**. Use **Refresh models** after correcting it. A failed refresh can leave the last known list visible.

Select an available model in Chat. Its response label and [[Usage and Stats|Usage]] show which model answered. Switching models keeps the chat intact.

On the connection, **Disable** pauses the route and **Delete** removes the connection and its accounts. Use **Remove** on an individual account to detach it. On eligible models, **Share** lets you choose or remove named recipients. Models shared with you appear in Model Management.
""",
    'openclank-docs-chat-workspaces': """# Chat and Workspaces

Open [Chat](clank://chat), choose a saved chat or start a new one, and select a model. Attach files in the composer. For work in a Host folder, select the workspace and check its name before enabling **Shell Access**. Workspace rules, file permissions and individual approvals still apply. See [[Workspace Hexes]] and [[App Windows and Navigation]].

## Send or steer a follow-up

Enter sends and Shift+Enter starts a new line. During a reply, Enter normally steers the turn and Ctrl+Enter queues a new message. Change this in **Settings → AI Defaults → Chat input → Queue follow-ups by default**. Pending messages above the composer offer **Send now** and **Cancel** where available. Choose **Stop** to stop the active turn.

Hover over a colored mark beside the chat to preview a turn, or click it to jump there and enable its sound. Drag the grip above the composer to resize it; double-click to reset.

## See what the chat is using

In a saved workspace chat, open **Workspace**. **Changes** lists observed workspace edits. **Sources** shows attached files and sources from loaded messages. Use **+** to attach a workspace file or select a source card to open it.

Open **Sources → Model context** or the context meter. The meter can show an estimate for current context and a reported size for the last request. **Inspect provider frames** shows retained sources and model details by attempt, with gaps labeled. If offered, **Preview** a context selection before choosing **Apply selection**. See [[Agent Settings and Compaction]] and [[Usage and Stats]].

## Inspect a worker or a saved plan

Open **Workspace → Subagents** and select a worker to see its session, observations and available transcript. Use **Send follow-up** to address that worker when enabled. **Chat work** lists recorded commitments, executions and routines. Open a pending question or approval in the originating chat or worker.

In a chat with a saved plan, open the plan panel’s **Feedback**. Check the revision, save your comment, then choose **Send** or **Verify and send** to deliver it. See [[Tasks and Continuations]] for scheduled follow-through.

## Resume, fork or recover

Use **Chats** or `/sessions` to find a conversation and `/resume name` to open it. For a supported native chat, use `/timeline` to find a mapped user message. `/fork <host-message-id>` opens a separate continuation before it. `/recover` lists supported interrupted replies and gives the command to recover one. Reconcile a pending fork receipt before retrying. Type `/commands` for the browser command list.

## Preview or move work

Select **Preview saved HTML** on an eligible tool result. The preview lists dependency gaps and offers **Open in Editor**, **Export source bundle (partial static)** and **Widget state…** where available. Use **More → Copy Chat** for chat text or [[Search and Media#Keep or export an output|Review selected outputs]] for saved files.

In Files, select an eligible standalone Codex thread-history SQLite snapshot and choose **Preview selected history…**. Choose an **Owned workspace**, then **Preview threads → Open thread**. Select supported items starting with a user message. Enter a **New chat name**, then **Review import → Import selected**. **Open imported chat** opens the new chat for model selection. If the result is unclear, use **Reconcile import** or save the recovery selector for **File → Recover previous history import…** under the same account.

## Continue from a terminal

With the Open Clank CLI installed, open a saved browser chat and copy its ID from `/chats info`. Substitute your server URL and chat ID below.

```bash
openclank profile add work https://clank.example --use
openclank login --profile work
openclank run --profile work --session CHAT_ID --prompt "Summarize the decisions so far."
openclank tui --profile work
openclank logout --profile work
```

Approve the terminal in the browser. Use `--no-browser` to open the displayed URL yourself. The profile selects the server and `--session` selects its chat. In TUI, type `open` and a number from **Sessions**. Use `help` for commands and `quit` before `logout` revokes the credential. Without an OS credential vault, open TUI directly and authorize it for that process.
""",
    'openclank-docs-editor': """# Editor

Keep a source file, its reference image and your notes open in the same Editor. An image tab brings its own canvas, layers and history. Text tabs bring back your writing tools. Use **Text**, **Wiki** and **Graph** on Editor’s left rail to return to your files, read linked pages or follow their connections.

## Explore the workspace

**Explorer** is Editor’s file sidebar. With no folder selected it shows the places you can browse. Open a file to focus its workspace, or use **File → Open folder…** to pin a particular folder. Opening another tab keeps that folder in view. **Close Folder** returns to available places. The [[Journal]] shortcut opens your own writing workspace; the Instruction Manual opens its read-only examples.

## Source comments

In supported source files, native comments render Markdown, including images. Click a comment to edit the prose in place. Open its context menu and choose **See source** to edit the raw comment. Leaving the inset updates the draft. **Save** writes the change to the source file. See [[Source Editing]] for syntax selection and conflicts when another program edits the same file.

## Open and arrange work

Split a tab right or below from its menu to keep the reference beside the work. While **Text** is active, select its button to hide or reopen Explorer. **Details** follows the active document.

## Save, Undo and close

Editor and Wiki share a page’s draft and Undo history. If the disk copy changes while your draft is dirty, compare the revisions before reloading. See [[Recovery]] for an uncertain save.

## Media and source reveal

[[OpenClank/Files and Imps#Image projects in Editor|Keep an image’s layers]], [[Deliverables|review a recording or export a deck]], or select a rendered example in [[Markdown Formatting Demo]] to see its underlying Markdown.
""",
    'openclank-docs-formatting-demo': """# Markdown Formatting Demo

Click a rendered block on this page to reveal or copy its Markdown. **Back to rendered** shows the result again.

## Task list

- [x] Mark the region in the reference image.
- [ ] Send the selected frame to the original chat.

## Quote

> Put the diagram beside the code that uses it.

## Emphasis and marks

Keep the **source**, mark the *draft*, cross out ~~superseded values~~ and ==highlight the change==.

## Table

| Keep editing | Share a copy |
| --- | --- |
| Layered image project | PNG |
| Workbook or slide deck | PDF |

## Links and references

[[Editor]] opens a manual page. [Settings](clank://settings) opens a control in the app. [[Wiki Recipes]] includes a live section embed.

## Code

```markdown
![[OpenClank/Editor#Source comments]]
```

## Footnote-style note

A source can stay beside its claim.[^source]

[^source]: Keep the version you used with the page.
""",
    'openclank-docs-files-imps': """# Files and image editing

Open an image from [Files](clank://files) to work on it alongside your text tabs in [[Editor]]. Files keeps its own location while Editor’s file sidebar follows the workspace you opened.

## Image projects in Editor

Crop or paint on the canvas, or choose **+ Import** to add another image as a layer. Layers and History belong to the active image. Pick the output by what you’ll need later:

| What you want | File menu |
| --- | --- |
| Reopen the layers for another edit | **Export layered project…**; reopen with **Import image project…** |
| Give someone the finished picture | **Download PNG** |
| Save a second image in Files | **Save as copy…** |
| Update the writable original | **Save file** |

**Open image draft…** resumes a retained edit. If the original has changed, keep your working copy and see [[Recovery]].

## Browse and open

Select saved HTML and choose **Open in Browser** for its preview. On a source folder, **Open in Code Graph** follows the saved files; on a file it starts with the containing folder. Missing host folders can be registered under [File access](clank://settings/file-access).

**Invite / manage access…** grants access inside the app. **Share download…** creates a download link. Choose a snapshot for a fixed version or a current-file link for subsequent changes. Revoke a link under **Manage shared downloads…**.

## Windows, split panes and views

Split a Files window right or below to keep the source and destination visible. Each pane has independent navigation. In a drag between panes, hold Option on macOS or Control on Windows/Linux to copy instead of moving. **Keep Both** retries a name collision with a new name.

## Create a file in Files

**File → New file…** creates the file in a writable destination. Choose **Open** in the receipt to go straight to Editor. For a personal [[Journal|journal page]], start in the journal workspace.
""",
    'openclank-docs-graph': """# Graph

Open [Graph](clank://graph) from the Editor rail. In **Graph view**, choose **Journal & Wiki** to follow page links or **Code** to follow relationships in saved source code.

{{manual-media:manual-graph-source-scope}}

## Find a useful view

Filter by folder, kind, tag or property. Clear filters if the view is empty. Use **Structure** for a page outline and **Galaxy** for document and calendar relationships. For dated events, open [Timeline](clank://timeline).

## Try a small graph

Create **Field Notes** and **Reading List** in the same [[Journal]]. Add `[[Reading List]]` to Field Notes and save it. In **Journal & Wiki**, select the Reading List node linked to Field Notes to open that page. See [[Wiki Recipes]] for more link and embed examples.

## Code Graph

1. In Graph, choose **File → Open folder…** and select an authorized Host folder. Graph switches to Code. From Files or Editor, **Open in Code Graph** takes one supported file or folder. Confirm the containing folder if you started from a file.
2. In an Open Clank checkout, wait for indexing and open **View → Search & evidence**. Under **Indexed metadata**, find `atomic_write_text` in `core/atomic_io.py`. Select the declaration and choose **Show in graph**. Use **Source contents · literal** to find case-sensitive text in saved, indexed files.
3. Choose **Graph → Connections for selected item**. With **Calls** visible, choose **Trace from selection** in Inspector and select `atomic_write_bytes` as the endpoint. Choose **Open witness** to read the saved call site. Use **View → Graph table** for a text view.

### Expand a large folder

**Gravity** is the default for a new view. **Web** shows a network and **Map** a compact strip. In Editor’s Explorer sidebar, select a folder and use **Graph → Expand selected branch**. Choose **Load more for selected branch** if more results are available. Use **Hide selected branch children** to clear space.

In **View → Graph settings…**, choose **Gravity override** for a change specific to Gravity or **Common defaults** for all layouts.

| What you need | Where to adjust it |
| --- | --- |
| Load more of a branch | **Paging & expansion** controls read size and depth. |
| Keep or show more of what is loaded | **Map cache** bounds what Graph retains. **Display capacity** bounds what it draws. Check **Effective capacity** for the limits in use. |
| Include more source files | **Settings → System → Code indexing** sets file and byte limits. |
| Make a crowded view readable | Use **Labels & relationships** to isolate calls or imports and **Motion & camera** to reduce animation. Save grouping and spacing under **Named physics settings**. |

### Resolve a partial or stale graph

Folder structure can appear before symbol evidence is ready. If a branch is missing, check **View → Coverage and diagnostics** for index coverage, then clear filters or load more. After a source save, use **Graph → Refresh now** if the old view is still showing. Use **Retry indexing** or **Readmit erased index** when offered.

### Review and share evidence

Open a call witness for the saved source behind a static edge. Inferred edges reflect source rules. **Recorded changes** shows observed file activity, and eligible static-function views can open **Imported trace evidence**. Open a trace there to inspect the observations it contains.

Choose **Add selected evidence to chat** to put it in the draft or **Ask about selected evidence** to send a question. In **Search & evidence**, use **Summarize selected evidence → Preview** to review the source scope and model before generating. Check the finished summary against its source links. To save a fact for future recall, see [[Memory and Lore]].

Use **File → Export current graph…** to preview the displayed graph and omissions. Choose a format and **Save**, or **Create new wiki document…** when offered. Reopen the preview if the graph changes.

## Official pages

Use **Include official documentation** to show handbook pages.
""",
    'openclank-docs-memory-lore': """# Memory and Lore

| If you need… | Open… |
| --- | --- |
| What was said or which tools ran | The original chat or transcript |
| A fact or preference the assistant can recall | [Memery](clank://memory) |
| Reported or estimated token counts | [[Usage and Stats]] |
| An earlier state of a file | The project's or document's History |

## Semantic memory

To save a preference, open **Memery → Add → Add Memory**, type it, select a category and press Enter. If it goes to review, open **Inspect → Candidates → Pending**. Check the wording and source, then **Edit**, **Promote**, or **Reject**. Promoted entries appear under **Curated**. Other profiles save the entry directly in **Memories**.

In **Inspect**, **Raw trajectory** shows captured evidence, **History** shows revisions, and **Quarantined** shows items withheld from recall. For eligible pending additions, use **Select for atomic batch** to preview and approve several together. If the result is unknown, choose **Check original status**.

To correct an outdated claim, open the newer item in **Memories → … → Details → Review relationship**. Choose the earlier item or **Find suggested pairs**. Use **Compare evidence** to read both claims and their sources. Choose **Preview supersession → Confirm supersession** or **Decline pair**. **Detach** removes a confirmed link while keeping both histories.

## Turn a memory into lasting guidance

Use `/remember` followed by a fact in chat to save it to Memery. A fact can inform a reply without changing the assistant’s personality. To make it a lasting preference, choose **Review as a persona change** on the memory, read the source and edit the proposed instructions before saving. Under **Settings → AI Defaults**, you can export a persona with a selected knowledge bundle. Import lets you review the persona and the included memories separately.

## Review an import batch

Use **Add → Add Memory → Import** to select files. When processing finishes, open **review** on the banner in **Memories**. Check suggestions against their sources and edit, save or reject each one. Use **check import status** for files that need attention.

Use **Export as Markdown** for one file or **Export as Markdown files (zip)** for separate files in a ZIP. When you import an Open Clank Markdown export, the text goes through review.

## Move reviewed knowledge

1. From a memory's **…** menu, choose **Export knowledge snapshot…**. Preview the full text and category before **Confirm and download**. For several memories, use **Select → Export selected as snapshots…** and check the included count.
2. In the receiving account, choose **Add → Add Memory → Import snapshot…** or **Import snapshot bundle…**. Use **Preview** for a single file or **Start import** for a bundle. Read the text and category and choose **Create review draft** for each item you want.
3. In **Memories → review**, decide which drafts to retain. Inspect skipped or refused bundle items and preview again if the bytes change. Check the original status for an unknown outcome.

For a saved folder analysis, choose **Check original files** on the memory. Select the original Host folder under its owned workspace. **Compare saved snapshot** reports the result for each component, including any it could not verify.

## Skills, prompts and admission

1. Open **Add → Add Skill → Marketplace…** and choose an offered source. Select **Preview catalog → View components → Review skill** for the skill you want. Review the publisher, release and files before **Create draft**.
2. In **Skills**, open the draft, read the instructions and companion files, then **Publish** to make it available. **Settings → Inject Skills** controls how many relevant skills can join a request. Zero turns off automatic injection.
3. In chat, expand **Skill used: …** to see the loaded revision and files when a receipt is available. To withdraw a skill, choose **Unpublish** on its card.

To move a published skill, use **Export snapshot…**, preview its contents and download it. Use **Add Skill → Import snapshot…** in the receiving account to review and create an independent draft.

## Timeline and Graph

**Memery → Graph** shows memory relationships. [[Graph]] follows links between journal pages or saved source. For an open chat, **Inspect → Journal coverage** shows chat consolidation progress. On a synthesis candidate, use **Review Journal sources → Open source chat** to review its citations. Open [[Journal]] for personal pages.

## Lore and recovery

Use the document's or project's **History** to inspect earlier file states before restoring. See [[Recovery]] for file restoration and pending operations.

**Settings → Nuke my Brain data** previews removal of selected live Memery data. Review the chosen categories and result. Source files, exports and backups are retained separately.
""",
    'openclank-docs-tasks': """# Tasks and Continuations

Open [Tasks](clank://tasks). **Meatbag Tasks** holds work you follow through on. **Clanker Tasks** runs model work on a schedule or on demand. **Workload** shows the commitments, executions and routines the current scope recorded.

## Track your own work

In **Meatbag Tasks**, create an item, check its title, source and due date, and mark it complete when done. A page checklist stays in the page until you use an explicit task action that creates an item in Tasks.

## Schedule or continue model work

In **Clanker Tasks → New Task**, set the prompt, model, schedule and output destination. Verify access to that destination. A task with email delivery can send to the saved address. Save and check the next-run time, or use **Run now**.

Use the task log to inspect or stop the current run. **History** shows prior output or errors. Check the previous outcome before retrying a task that can send or modify something. **Pause** and **Resume** control its schedule.

In Chat, ask explicitly for a task when the work needs its own schedule or record. Open the resulting task chat to answer its question. Follow an existing worker from **Workload** or [[Chat and Workspaces#Inspect a worker or a saved plan|the chat’s worker view]].

## Review repository changes

Open the repository root in Files and select its managed chat. Choose **Files → Go → Git workbench…** to see working and staged changes. Choose a file if required, select a **Git operation**, then use **Review** to inspect the saved bytes and target before confirming. If the result is pending, use **Check status**. See [[Recovery]] for file history.
""",
    'openclank-docs-settings-themes': """# Settings and Themes

Use **Find a setting…** to jump to a control. Theme and keyboard bindings are browser-local. Provider connections and launcher visibility belong to your account. [Settings](clank://settings).

## Files, Editor and agent access

**Permissions → Your file access** shows what you can open in Files and Editor. Administrators share a **Location** through **People**, with **Browse / download** or **Modify** access. **Operation approvals** shows saved agent approvals and when they expire.

The owner and administrators can set **Permissions → Full filesystem access** for their own accounts. **Files and Editor** and **Agents for this account** are separate switches. On uses the host OS account’s permissions. Off uses existing folder grants. **Account → Full access** enables models and optional tools where offered.

## Account and administration

Set up your authenticator under **Account → Two-Factor Authentication**. For a provider login, see [[Accounts and Models]]. Administrators manage scoped API tokens at `/api/tokens`.

Check **Logging → Storage inventory**, or use the administrator’s **System → Stack health**, **Terminal Logs** and **Data Backup**. For what backups cover, see [[Recovery#Restore an installation|Restore an installation]].

**Tokenomics** lets you opt into changes such as reusing exact repeated Browser snapshot text. Check the effective settings there and observed usage under [[Usage and Stats]].

## Connect an MCP tool server

As an administrator, open **Settings → Integrations → MCP Tool Server**. Select **stdio** for a command, arguments and environment on the host, or **SSE** or **Streamable HTTP** for a server URL. Put credentials in the connection settings. Choose **Save**, complete **Authorize** if prompted and check the tool list. A local command uses the host’s access. A remote tool receives the data you pass to it.

## Change appearance

Open [Appearance](clank://settings/appearance). Choose **Solid** to stop background motion. **Copal navigation** restores a hidden Journal shortcut. [Shortcuts](clank://settings/shortcuts) shows your bindings.

Choose **Interface language** here to switch the controls and the built-in handbook. Your own messages and documents keep their original text.

New supported workbooks and decks use the current theme. To change an existing one, see [[Deliverables#Edit a slide deck|Apply the current theme]]. **Reduce motion in generated deliverables** is an account preference.

## Google Emoji Drift artwork

Select **Emoji Drift** to review consent. Regular emoji appear immediately and give way to downloaded artwork as it arrives. **Allow** permits acquisition. Choose **Refresh artwork status** under Emoji Drift to see download and update progress, pause or resume weekly updates, or **Revoke emoji artwork consent**. See [[Credits and Included Components]].
""",
    'openclank-docs-recovery': """# Recovery

Keep your draft until you know what was saved.

| Problem | What to do |
| --- | --- |
| Save conflict | Reopen the stored revision and merge your draft. |
| Save refused because a recovery version is unavailable | Restore the recovery service before retrying. |
| Editor cannot read a document | Inspect the preserved source or restore an earlier version. Preserve the original bytes. |
| Missing file or media | Check the account and **Permissions → Your file access**. Relink a moved resource or replace an expired link with a saved file. |
| Unsure whether an external action finished | Check the original result or receipt before repeating the action. |
| Handbook page changed | Reopen it from Help for the current edition. Your personal notes stay in their own documents. |
| Missing sidebar shortcut | Restore it in **Settings → Appearance → Copal navigation**. |

## Restore a file or project

Select the file, then choose **Actions → History** to review its versions or checkpoints. For an open Host folder, choose **Go → Folder History…** and open a file’s versions from the recorded changes or **Removed files**. Review the selected version before confirming a restore. See [[Memory and Lore]].

## Restore an installation

The repository backup helper covers its `data/` directory. Check for Journal storage, host workspaces and other stores outside that directory. **Settings → System → Data Backup** transfers the listed user-data categories as JSON. For full restore steps and coverage, use the [backup guide](https://github.com/Plaer1/open-clank/blob/main/clank-code/docs/backup-restore.md).
""",
    'openclank-docs-limits': """# Limits and Platform Support

Check your exact package and checksum. [Beta 2 release and artifact records](https://github.com/Plaer1/open-clank/releases/tag/v1.0.3-beta.2).

| Platform | Support |
| --- | --- |
| macOS | Apple Silicon package for macOS 15 or later. Ad-hoc signed. |
| Windows | Use an architecture offered by the release. Installed-package checks are recorded per artifact. |
| Linux | Source setup is documented. App and package qualification remain open. |
| Docker | Legacy files. Unsupported. |

For installation steps see [[Setup and First Run]]. For source and package evidence see the [known limits](https://github.com/Plaer1/open-clank/blob/main/clank-code/docs/known-limits.md).

## What depends on my installation?

- **Host actions** depend on OS access and available handlers. See [[Desktop Capture and Host Opening]].
- [[Graph|Code Graph]] shows the evidence for each indexed relationship. Check coverage when a call or dependency is missing.
- **Remote services** receive the material you send and may charge for requests. Model and provider capabilities vary. See [[Accounts and Models]].
- **Journal** uses Files-backed storage in the current hosted app. Preserve older stores using the upgrade and backup guides before switching. See [[Recovery]].

The service runs within its host OS account’s permissions. A link follows the reader’s existing grants. [Deployment security](https://github.com/Plaer1/open-clank/blob/main/.github/SECURITY.md).
""",
}

ADDITIONAL_ARTICLES = (
    ('openclank-docs-setup', 'Setup and First Run', """# Setup and First Run

Check [[Limits and Platform Support]] for your target.

## Start a fresh installation

- **macOS** · Copy the packaged **OpenClank.app** to Applications. Use the menu-bar icon’s **Open** to show the app and **Quit** to stop its service.
- **Windows** · Use the matching release installer. Start menu **Open Clank** opens the app and **Stop Open Clank** stops its service.
- **Source** · Follow the platform steps for Python, Rust, native workers and matching offline artwork. Start the service with `openclank server start`. [Setup guide and artwork instructions](https://github.com/Plaer1/open-clank/blob/main/clank-code/docs/setup.md).

The default local address is `http://127.0.0.1:7777`.

## Sign in

A fresh package asks you to create the first administrator in the browser. A source installation uses the administrator configured during setup. Change any generated temporary password in **Settings → Account**.

To use Chat, connect a model in **Settings → Provider/Model Management → Add Provider**. Signing into Open Clank and signing into a model provider are separate. See [[Accounts and Models]].

## Update an existing installation

Back up the configured stores and workspaces and stop the owning service. Follow the migration steps for your starting version before restarting. [Upgrade guide](https://github.com/Plaer1/open-clank/blob/main/clank-code/docs/upgrading.md).

## When startup fails

Check the package error or run `openclank server status` for your source installation. If startup refuses an existing store, preserve it and follow the upgrade guide. See [[Recovery]].
"""),
    ('openclank-docs-agent-settings', 'Agent Settings and Compaction', """# Agent Settings and Compaction

Open [Settings](clank://settings) → **AI Defaults** to set the model for new chats and manage reusable personas. In Chat, use the model and persona pickers to change those choices for one conversation. The model picker can offer saved **Presets** and route-specific **Thinking effort**. Use `/model` to see the current selection, `/agent-profile` to list eligible native profiles, and the response label to see which model answered.

## Change a policy deliberately

For a long native chat, open **AI Defaults → Native agent settings**. To compact earlier, lower **Maximum context**, for example to `50%`. Select **Save for next turn** and check **Effective native values**. The model route sets the hard window, and the running turn keeps its original policy.

Use **Automatic compaction** and the recent-turn or recent-token settings to control the working context. **Advanced checkpoint settings** holds thresholds, reserve and writer controls. For an eligible chat, choose **Compact** from the context meter or type `/compact`. Reopen the meter for the result. Use **Reset overrides** to restore inherited values.

## Context, cost and access

See [[Chat and Workspaces#See what the chat is using|Inspect the context of a chat]], [[Memory and Lore|Review retained memory]] or [[Usage and Stats#Optional cost and capture controls|Change cost and logging options]]. [[Workspace Hexes]] covers project rules and approvals.
"""),
    ('openclank-docs-search-media', 'Search and Media', """# Search and Media

## Find a chat or search the web

Use **Search conversations** on the sidebar or rail, or press Ctrl+K, to find text in saved chats. For web retrieval, enable **Web search** in the composer and open the returned source cards. If retrieval is unavailable, check the reported route. The administrator sets search providers, credentials and fallback order in **Settings → Provider/Model Management**.

## Run Deep Research

Open **Deep Research** from the sidebar or rail, enter a question and choose **Start**. Follow progress in Research, then reopen the saved report and sources. The model comes from **Settings → AI Defaults** and the research search route from **Settings → AI Defaults → Deep Research**. Unavailable pages and timeouts can leave gaps in the source list.

For YouTube captions, give the chat a video URL and ask for the passage with timestamps. Check the language and whether the captions are manual, automatic or translated. If captions are unavailable, provide an accessible source.

## Keep or export an output

In Files, open **Media jobs…** for an eligible resource or folder. Choose the operation and profile, review the source and destination, then start. If the result is unclear, check or reconcile the existing job. When ready, collect it if requested and reopen the saved file. [[Desktop Capture and Host Opening]] covers recording and [[Wiki Recipes]] covers embedding saved media.

For a ZIP, select one to sixteen downloadable Host files and choose **File → Review selected outputs…**. Check the files for private data, confirm the review, and record known license, attribution and omissions. Choose **Preview exact outputs**, then check the file list and findings before **Download reviewed ZIP**. If a file changed, review the selection again. For HTML, see [[Chat and Workspaces#Preview or move work|Preview a saved result]].

## Use speech or a connected chat channel

Where available, allow microphone access and review the transcription before sending. **Read aloud** on a response uses the supported voice settings under **AI Defaults → Text to Speech**.

In **Settings → Integrations → Chat channels**, turn on **Allow chat channels**. Enter the channel details, choose its endpoint and model, then select **Switch this channel on → Save**. Approve the intended pairings and check **Allowed people**. Use **Allow group** where supported and check delivery. See [[Calendar and Email]] for those connections.
"""),
    ('openclank-docs-desktop-host', 'Desktop Capture and Host Opening', """# Desktop Capture and Host Opening

Capture a screen or window into Files, then point feedback at a **moment in the recording**. The assistant can receive the selected frame or time range in the original task. See [[OpenClank/Deliverables#Review a recording|Review a recording]].

## Capture and OCR

Enable **Settings → AI Defaults → Desktop → Allow screen capture and recording**. Ask for the display, window or region you need and select it in the system chooser. If OCR is available, check its text and bounding boxes against the retained image.

Linux uses the desktop portal and capture helper. macOS needs Screen Recording permission. Windows capture requires its native helper and an interactive desktop.

## Record a screen or window

On a supported Linux desktop, choose **Record screen…** in Files, set the destination, then select the source. Audio starts off. **Media jobs…** lets you stop early or recover the original job’s status.

## Start and stop desktop control

For a supported managed chat, an administrator can enable **Allow the assistant to control this desktop (experimental)** in Desktop settings. Choose the target and review any approval. Linux pointer control needs Wayland and the desktop portal. macOS input needs the native helper and Accessibility permission; Windows x64 needs its helper and an interactive desktop.

Chat shows the active control session. Use its takeover shortcut when offered, or **Stop desktop control on this host**.

## Open a resource on the host

**Files → Open with** hands an eligible file to the host’s default app or an installed app you choose. For an HTML report inside Open Clank, use **Open in Browser**; see [[OpenClank/Deliverables#Keep report or widget progress|report progress]].
"""),
    ('openclank-docs-usage', 'Usage and Stats', """# Usage and Stats

Open **Usage** from the sidebar or rail. Visit **Sessions** for one run or **Usage** to compare cost and tokens across runs.

## Find the evidence for a run

In **Sessions**, choose a date range and **Usage timezone**, then filter by provider, model, account or workspace as available. Open a session to see its saved messages, tools, sources and transport attempts. **Open matching conversation** returns to the chat. Use the session export for content and **Usage → Export CSV** for numeric data in scope.

**Reported** values came from the producer. **Estimated** values use available inputs and pricing. **Unknown** or **Partial** means some evidence is missing. Reported zero is a measurement. For context and worker evidence, see [[Chat and Workspaces#See what the chat is using|Model context]] or [[Chat and Workspaces#Inspect a worker or a saved plan|Subagents]].

## Compare usage and activity

Choose **Cost** or **Tokens** for supported groupings and token categories. In **Activity**, select a day to narrow Sessions in the chosen timezone. **More → Account quota** shows supported limit windows. **Trends** analyzes opted-in archive content across dates and identities. **Quality** follows selected dates across identities. Read the coverage label before interpreting either.

## Optional cost and capture controls

**Settings → Tokenomics** starts off. If enabled, eligible tool results can use a compact form on the next turn. Check the effective readback.

**Settings → Logging** keeps baseline activity and usage. **Advanced provider logging** starts off and adds future transport details for supported requests. Sanitized bodies can still contain your work. Opening or exporting bodies requires an explicit choice. Review retention previews before pruning.
"""),
    ('openclank-docs-wiki-recipes', 'Wiki Recipes', """# Wiki Recipes

**Wiki** on Editor’s left rail can display the same page you were editing, complete with links, attachments and the current draft. Switch to **Source** to use the examples below in a personal [[Journal|journal page]].

## Link a page and embed a section

Link to a page by name. Add a heading and a leading exclamation mark to display that section in place:

```markdown
[[OpenClank/Editor|Editor guide]]
![[OpenClank/Editor#Source comments]]
```

Here is the live section from the manual:

![[OpenClank/Editor#Source comments]]

## Media with an accessible resource

Use **Media** to select an adopted picture, recording or PDF. For an image, `![[My photo.png|640]]` sets a display width. If an embed goes missing, check the resource name and your access.

## Source comments and a reusable template

Set a page’s `type` property to `template`, or select a host Markdown folder with **Tools → Choose template folder…**. In a writable page, choose **Insert template** and review any property collisions. The result is a copy you can change independently.

## Typed tables, Bases and Canvas

- **Typed table.** Copy **Typed table (dates, currency, totals)** from **Editor commands → Syntax gallery** into a page for editable quantities and calculated totals.
- **Base.** A `.base` view filters pages by their properties. To correct a value, open the source page from its row and edit the property in **Details**.
- **Canvas.** **Note actions → View raw source** exposes a canvas’s node data. Save valid JSON and choose **Back to typed view** to see the result.

## Open Wiki and preserve native data

The Library’s native `.memes` export carries structured page data. Use Markdown export when the text is what you want to take elsewhere. To trace relationships between saved pages, open [[Graph]].
"""),
    ('openclank-docs-treehouse', 'TreeHouse', """# TreeHouse

Open [TreeHouse](clank://treehouse) to continue a Main Quest in Programming, Games, AI or Open Clank. Your resume point follows your account across workspaces.

## Explore a Quest

Choose **Explore Quests** and open a Quest. **Connected lessons** shows where the idea is used again. A prerequisite for practice still leaves the lesson and conceptual response open.

{{manual-media:manual-treehouse-linked-lesson}}

Use **Your work** to check the task and its evidence. **Save draft** lets you resume. **Submit response** records your answer. For practical work, choose the outcome and **Save attempt**. A blocked or failed attempt stays pending. To record success, confirm **I achieved the stated outcome** and attach the required evidence. Self-reported work keeps that status until reviewed or verified.

{{manual-media:manual-treehouse-practice-evidence}}

## Progress and resets

**Skills** shows required-work progress. **Library → Search learning** finds a lesson. After an update, review **Previous answer** before copying it into a new response.

Choose the narrowest reset scope you want. A Main Quest reset includes its electives. Resets clear work, drafts and resume points in that scope. Historical awards and private courses are separate.

## Achievements

Open **Copal → Achievements** and select an award. **How to earn it** gives its requirement. For earned awards, **Completion evidence** shows saved evidence counts and references where available. For a learning award, use **Library → Search learning** to find the lesson and check its submissions. **Settings → Advanced → Achievements** manages notifications and account-wide award reset.

## Private courses and authoring

Open **Private courses** for your own courses and those shared with you. Eligible courses offer completion records under **Records**. **Author → Open source** uses your Files grant. Save in Editor and reload the lesson. Course sharing uses separate grants from sharing the source files.

If shared quests need activation, private learning stays available. Ask the operator to back up the store and follow the [upgrade guide](https://github.com/Plaer1/open-clank/blob/main/clank-code/docs/upgrading.md). Then choose **Recheck shared quests**.
"""),
    ('openclank-docs-workspace-hexes', 'Workspace Hexes', """# Workspace Hexes

In Files, select a workspace and choose **Open Workspace Hexes** from its context menu. For reusable account instructions, open **Settings → Hexes → General**.

## Inspect before changing a file

The workspace contract lives in `.clanker/hexes/contract.yaml`. From the workspace, run `openclank hex explain path/to/file` to see which rules apply. Check the active revision before changing the file. Installation policy and the operating system still govern access.

## Maintain the contract

With owner authorization, edit the contract and run `openclank hex sync` to regenerate the digest in `AGENTS.md`. Executable project checks require separate trust.

In **Authorized workspaces**, choose the project and **Review changed contract**. Check its hash and scope before choosing **Activate reviewed contract**, if permitted. Use **Promote to General** to reuse a workspace rule. See [[Recovery]] for file recovery and the [contributor reference](https://github.com/Plaer1/open-clank/blob/main/.github/CONTRIBUTING.md#private-workspaces-and-release-inputs) for private workspace conventions.

## General instructions

Choose **New General Hex**. Enter a title and instruction. With no tags it applies across your account. Experimental Workspace or Task tags apply on any positive match. Preview the revisions for the next turn or operation. Running work keeps its pinned revision.

If a rule has gone stale, select it and choose **Create draft**. Cite the saved memory that shows what changed. Pick **Edit** for replacement wording or **Archive** to retire the rule. A draft can refer to several parent Hexes.

Open **Review** to compare the current rule, proposed wording and evidence. An agent can submit a draft here. The current rule applies until you accept the change. The evidence scan checks previously linked sources for changes. A rule with no linked source needs a manual review. In the rule’s **History**, **Save checkpoint** records the revision in Lore when your Memery backup settings allow it.

Review the candidate and affected paths before applying a General Hex to a workspace. Review imported or agent-assisted revisions before making them eligible. Mandatory workspace rules still apply.
"""),
    ('openclank-docs-app-windows', 'App Windows and Navigation', """# App Windows and Navigation

The hamburger switches between the full sidebar and the compact rail. Use [[Buddy]] to ask about a control or **Tools → Instruction Manual** to look it up.

## Copal

Open your [[Journal]] from **Copal**. **Manage journals** beside the shortcut lets you choose which Journal opens. Restore a hidden shortcut under **Settings → Appearance → Copal navigation**.

## Arrange the work

Drag a title bar to an edge and release when the snap preview appears. Left and right snap side by side. Top and bottom stack. Drag the divider to resize either arrangement, or drag a snapped window away to free it.

**Maximize** fills the workspace and **Restore** returns to the prior size. **Minimize** puts the window in the bottom dock. Pop out Chat for a standalone window and use **Dock chat** to return it. To end a run, use **Stop** in Chat.

## Choose a split or another window

- **Files** · **Window → New Files Window** opens an independent browser. **Split right** and **Split below** let you browse two places in one window.
- **Editor** · **Window → Split editor right…** or **Split editor below…** lets you choose a document for the new group. Two views of the same document share one draft. **Go → Previous document** follows document history. Back in Editor’s file sidebar follows folder history.
"""),
    ('openclank-docs-source-editing', 'Source Editing', """# Source Editing

A comment can carry its own diagram. In a supported source language, Open Clank renders Markdown inside native comments: `![alt](target)` or `![[target]]` can display an accessible image next to the code it describes.

{{manual-media:manual-editor-rich-comments}}

## Local spelling and comments

Click the rendered comment to edit it. To edit the raw comment, open its context menu and choose **See source**. Leaving the inset puts the edit in your draft. **Save** writes the file. If the range became stale while you were typing, return to raw source before applying changes. **Tools → Check spelling locally** checks prose using the signed-in account’s dictionary.

## Identify the language

For an ambiguous extension, use **Language mode** on the status bar. Check the selected grammar if you see raw comment syntax where you expected formatted prose.

## Edit several places

**Selection → Select next match** adds another occurrence to your selection. **Keep primary selection** returns to one. For a replacement across the active file, preview the next match in **Edit → Find and replace…**.

{{manual-media:manual-editor-find-and-replace}}

## Outside edits

A clean host tab can pick up a saved change from another program or agent. With an unsaved draft, **File → Refresh from disk** checks what changed; inspect the conflict before choosing **Reload latest from disk**. [[Graph]] follows saved code, so save first if you want it to see your edit.
"""),
    ('openclank-docs-calendar-email', 'Calendar and Email', """# Calendar and Email

In **Copal**, use **Calendar** for appointments and reminders, **Timeline** for dated working notes, and **Email** for mail. To give work a deadline or schedule an assistant run, see [[Tasks and Continuations]].

## Calendar

Choose **New** and check the destination calendar before saving. For CalDAV, connect the account in **Settings → Integrations** and check that the event syncs. To change a repeating event, choose **This event only** or **All recurring events** when Calendar asks for the scope.

Add a **Reminder** in the event form. Set delivery under **Settings → Reminders**. Browser notifications need browser and OS permission. Email and webhooks need their own connections.

## Timeline

Use **+ Event** in [Timeline](clank://timeline) to put a note or date range on a track. Name the first track if asked. Select the event and choose **Open note** to work in its source.

## Email

Connect a mailbox through **Settings → Integrations**, then choose that account in **Email**. Google OAuth requires configuration. Use a different client if the mailbox requires Microsoft OAuth or Graph Mail. [Mail setup guide](https://github.com/Plaer1/open-clank/blob/main/clank-code/docs/email-outlook.md).

**Summary** can request analysis. **AI reply** drafts a reply using your configured model and may use its quota. Review the draft before **Send** and check the send result before retrying.

## Email settings

For a particular mailbox, use **Settings → Integrations → Open Email Settings**. **Email Reply Writing Style → Extract** uses accessible sent mail to suggest style instructions. Review the result.

**Auto Reply** can send mail automatically. Check the mailbox, dates, message and repeat-sender exclusions before enabling it. **Newsletter Unsubscribe → Clean** shows candidates for review before applying an action.
"""),
    ('openclank-docs-compare-cookbook', 'Compare and Cookbook', """# Compare and Cookbook

## Compare connected answers

Open **Compare** from the sidebar or rail. Select at least two available models and send the same prompt to both. Read the answered cards and check their provider and model labels before choosing a preference. If one card fails, inspect its route before rerunning. Each model uses its own request and quota. See [[Usage and Stats]].

To append the literal words `no mistakes.` to a model’s prompt, enable the option on that lane before starting.

## Serve a local model

1. Open **Cookbook** and choose the host that will serve the model. **What Fits?** estimates memory and shows suggested engines and formats.
2. Choose an artifact in **Download → Scan / Download**, or enter the repository under **Direct Download**. Check its destination and follow progress in **Running**.
3. Open the downloaded model in **Launch**. Review the engine, model and port before starting the server. Wait for readiness in **Running** and check the endpoint.
4. Select the model in Chat if it is connected. Otherwise, add its reachable address under **Settings → Provider/Model Management → Add Provider → Local**, then check **Model Management**. See [[Accounts and Models]].

For a failed launch or download, open the task output in **Running**. Resolve the reported dependency, artifact, host or port issue before retrying. Use **Running** to see whether a saved preset’s server is live. See [[Limits and Platform Support]] for host requirements.
"""),
    ('openclank-docs-credits', 'Credits and Included Components', """# Credits and Included Components

Open Clank ships under AGPL-3.0-or-later. Each included component keeps its own notices.

- Open Clank builds on [Odysseus](https://github.com/odysseus-dev/odysseus) and integrates Copal’s document workspace, the [MiMo Code](https://github.com/XiaomiMiMo/mimo-code) and [opencode](https://github.com/anomalyco/opencode) agent foundations, and [Epic Games’ Lore](https://github.com/EpicGames/lore) for History.
- Google supplies Noto Emoji and Emoji Kitchen artwork. [Xavier Salazar’s catalogue](https://github.com/xsalazar/emoji-kitchen) supplies the Kitchen combination data. The Apache-2.0 notice applies to the Noto SVG artwork. See **Settings → Help → Emoji credits**.
- The Comic Clank Project Authors provide **Comic Clank** and **Comic Clank Mono** under the SIL Open Font License 1.1. Choose either family in Appearance. The wordmark uses Comic Clank Mono Bold. See `static/fonts/ComicClank-OFL.txt`.

The repository records editors, renderers, fonts, design studies and their notices. [Acknowledgments](https://github.com/Plaer1/open-clank/blob/main/clank-code/ACKNOWLEDGMENTS.md) · [Bundled components](https://github.com/Plaer1/open-clank/blob/main/clank-code/licenses/BUNDLED-COMPONENTS.md)
"""),
    ('openclank-docs-journal', 'Journal', """# Journal

Your journal is a workspace for pages, pictures and sources you want to find again. **Copal → Journal** opens it in Editor. The name at the top of **Editor’s file sidebar (Explorer)** is the journal you’re in. Rename it *Field Notes* and that is what the sidebar shows.

{{manual-media:manual-editor-journal-navigation}}

## Write a journal page

The starter journal opens on **Welcome to your journal**, which you can edit. Choose **New journal page** in Editor’s sidebar to add a page of your own. Type `[[Field observations]]` in another page to link to one named *Field observations*. Open the linked page in Wiki to see which pages point back, or follow the links in [[Graph]]. [[Wiki Recipes]] shows how to embed just one section.

## Manage Journals

Use the menu beside **Journal** in Copal, or open **Settings → Appearance → Copal navigation → Manage journals**. **Add Journal…** registers an eligible account folder. **Open** switches to it for this session; **Make default** chooses where the sidebar shortcut takes you next time. **Remove** unregisters a journal and leaves its files in place.

**File → Close Journal** in Editor releases the workspace and keeps open tabs. If you hid the Journal shortcut, restore it in **Copal navigation**.

Converting an older journal? **Review Journal conversion…** previews conflicts. Applying the conversion makes the chosen folder the only registered journal and the default. Keep the operation ID so **Check Journal conversion…** can confirm the outcome before you retry.
"""),
    ('openclank-docs-deliverables', 'Deliverables', """# Deliverables

You can hand back a chart with its source, a PDF ready to send, or feedback attached to one frame of a recording. Start a workbook or slide deck in a writable folder in [[Editor]].

## Build a workbook and a chart

**File → New workbook…** creates a `.clankbook`. Under **Ranges and charts**, use **Add range** to choose the source cells by ID, then **Add chart** to set the range and chart type. The chart and its data table share that range.

## Edit a slide deck

**File → New slide deck…** creates a `.clankslides` file. For a chart already embedded in a slide, **Open source** takes you to its workbook. The reference names the workbook revision and chart ID. If the chart is unavailable, check access and the saved reference before exporting. A new workbook or deck takes its colors from your theme. **Tools → Apply current theme** updates an existing one.

## Export to PDF

Save the editable original, then choose **File → Export PDF to Files…**. Keep both if someone will revise the chart or slides later. A pending export can be checked from the same command.

{{manual-media:manual-workbook-to-pdf}}

## Keep report or widget progress

Open a saved HTML report with **Files → Open in Browser**. Reports that support progress can **Export JSON backup** or **Export Markdown backup**. **Import progress backup → Apply compatible items** restores answers against the current report.

For an interactive widget, **Widget state… → Capture current state** lets you save selected fields to Files. **Load from Files…** previews which fields can be restored. Both workflows preserve the report’s source HTML.

## Hand off an edit

Open the saved file and the original chat’s feedback panel. Pick the smallest useful target: **Feedback on image region…**, **Feedback on file or selection…** for a workbook cell or slide object, or **HTML source Properties → Feedback on source element…**. For saved HTML or SVG, **Design handoff to an actor…** includes the destination, appearance and assets. Review the revision and recipient before sending.

## Review a recording

From the saved recording in Files, choose **Review in Editor…**. Under **Recording actions**, mark a start and end. **Annotate selection…** prepares feedback in the original task. **Save displayed frame…** saves a PNG; **Trim selected range…** produces a separate clip when supported. Check the trim’s actual coverage before sharing it.

{{manual-media:manual-recording-review}}
"""),
    ('openclank-docs-buddy', 'Buddy', """# Buddy

Show **Buddy** from the main sidebar. Click the eye, choose **Explain this to me** and select a highlighted control or the eye itself. **Escape** cancels selection.

Buddy sends a question in a new chat using the Instruction Manual. Use a regular chat with a working model and finish your draft and attachments first. To read the guide without a model, open **Tools → Instruction Manual**.

Change Buddy’s **Look** or turn off **Show buddy** under **Settings → Buddy**.
"""),
    ('openclank-docs-clankshop', 'Clankshop', """# Clankshop

Open **Copal → Clankshop** to watch chat activity or try it with simulated workers.

## Follow live work

Turn on **Live** to observe eligible chats. Select a worker to see its latest action. **Open chat**, **Open question** or **Open chat permission** returns you to the conversation. **Open chat with this text** fills the composer for review.

- **Workshop** shows each worker’s station and reported action.
- **Drift** turns tool activity into speed and traffic. Under **Route**, **Mega Highway** uses a shared road and **Highway Spaghetti** uses branches that merge.
- **Idle** shows activity meters and the robot eye.

Counters describe the activity of loaded chats. For the outcome, open the chat or check [[Tasks and Continuations]]. **View settings → Pause in background** pauses the scene when you leave its window.

## Simulate each worker

Open **Debug** and set **Workers**, then give each a tool category and calls per second. Choose a **Cue worker** and **Preview cue** and select **Show cue**. In Drift, **Drift course** adjusts the selected route and playback speed. **Return to live view** leaves the simulation without sending any actions to chat.

## Adjust audio

Choose **Enable audio**. Under **Audio settings**, adjust **Music volume**, **Effects volume** or an individual cue. **Background audio** offers full, quarter or silent volume.
"""),
    ('openclank-docs-index', 'Manual Index', """# Manual Index

## Get started
- [[Home|What is Open Clank?]]
- [[Setup and First Run]]
- [[Accounts and Models|Connect a provider and choose a model]]
- [[Getting Work Done|From a request to a deliverable]]
- [[App Windows and Navigation|Find and arrange applets]]
- [[Buddy|Ask Buddy about a control]]

## Work with your agent
- [[Chat and Workspaces|Give a chat project files and steer a run]]
- [[Agent Settings and Compaction|Manage context and model defaults]]
- [[Memory and Lore|Keep memories and recover earlier work]]
- [[Compare and Cookbook|Compare answers and run local models]]
- [[Usage and Stats|Inspect usage]]
- [[Clankshop|Watch worker activity]]

## Research and learn
- [[Search and Media|Find sources and media]]
- [[Journal|Keep a linked journal]]
- [[Wiki Recipes|Link and embed pages]]
- [[Markdown Formatting Demo|See Markdown examples]]
- [[Graph|Explore page links and code relationships]]
- [[TreeHouse|Follow a course or build one]]

## Create and review files
- [[Editor|Work in the Editor]]
- [[Source Editing|Edit code and rich comments]]
- [[Files and Imps|Browse files and edit images]]
- [[Desktop Capture and Host Opening|Capture the screen and open host files]]
- [[Deliverables|Review workbooks, slides and recordings]]

## Organize and schedule
- [[Tasks and Continuations|Assign work and schedule agent runs]]
- [[Calendar and Email|Manage calendars, Timeline and email]]

## Configure and troubleshoot
- [[Settings and Themes|Change settings and appearance]]
- [[Workspace Hexes|Set workspace rules]]
- [[Recovery|Recover after an interruption]]
- [[Limits and Platform Support|Check platform support]]
- [[Credits and Included Components|Look up components and credits]]
"""),
)

# Only these first-party media IDs may be expanded in each article.
ARTICLE_ASSETS = {'openclank-docs-home': ('manual-graph-source-scope',),
 'openclank-docs-graph': ('manual-graph-source-scope',),
 'openclank-docs-treehouse': ('manual-treehouse-linked-lesson', 'manual-treehouse-practice-evidence'),
 'openclank-docs-source-editing': ('manual-editor-rich-comments', 'manual-editor-find-and-replace'),
 'openclank-docs-journal': ('manual-editor-journal-navigation',),
 'openclank-docs-deliverables': ('manual-workbook-to-pdf', 'manual-recording-review')}

ARTICLE_EXAMPLES = {
    "openclank-docs-formatting-demo": ("formatting-source-reveal",),
    "openclank-docs-wiki-recipes": (
        "wiki-link-section", "wiki-table", "wiki-media",
        "wiki-source-comment-template", "wiki-graph-navigation",
    ),
}

EXISTING_ARTICLES = (('openclank-docs-home', 'OpenClank/Home', 'Instruction Manual', ('OpenClank/Start Here', 'OpenClank Handbook', 'Open Clank Handbook'), 0), ('openclank-docs-getting-work-done', 'OpenClank/Getting Work Done', 'Getting Work Done', (), 1), ('openclank-docs-accounts-models', 'OpenClank/Accounts and Models', 'Accounts and Models', (), 2), ('openclank-docs-chat-workspaces', 'OpenClank/Chat and Workspaces', 'Chat and Workspaces', (), 3), ('openclank-docs-editor', 'OpenClank/Editor', 'Editor', (), 4), ('openclank-docs-formatting-demo', 'OpenClank/Markdown Formatting Demo', 'Markdown Formatting Demo', ('OpenClank/Formatting Demo',), 5), ('openclank-docs-files-imps', 'OpenClank/Files and Imps', 'Files and image editing', ('OpenClank/Files and image editing',), 6), ('openclank-docs-graph', 'OpenClank/Graph', 'Graph', (), 7), ('openclank-docs-memory-lore', 'OpenClank/Memory and Lore', 'Memory and Lore', (), 8), ('openclank-docs-tasks', 'OpenClank/Tasks and Continuations', 'Tasks and Continuations', (), 9), ('openclank-docs-settings-themes', 'OpenClank/Settings and Themes', 'Settings and Themes', (), 10), ('openclank-docs-recovery', 'OpenClank/Recovery', 'Recovery', (), 11), ('openclank-docs-limits', 'OpenClank/Limits and Platform Support', 'Limits and Platform Support', (), 12))


def manual_pages():
    """Yield canonical source copy before delivery adds demonstration media."""
    for article_id, name, title, aliases, order in EXISTING_ARTICLES:
        yield article_id, name, title, ARTICLE_CONTENT[article_id], aliases, order
    for order, (article_id, title, body) in enumerate(ADDITIONAL_ARTICLES, start=len(EXISTING_ARTICLES)):
        aliases = ("TreeHouse and Field Guide", "OpenClank/TreeHouse and Field Guide") if article_id == "openclank-docs-treehouse" else ()
        yield article_id, f"OpenClank/{title}", title, body, aliases, order
