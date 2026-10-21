# Check the index revision

- A code graph is an index of source evidence. Compare its revision with the current file before using an edge as a current fact.

## Code Graph (Alpha): find the source behind a relationship

Open Graph and choose **Code · scoped evidence (Alpha)**, or use **Open in Code Graph** on one supported Host file/folder in Files or Editor. Confirm the authorized code folder. It is separate from your chat workspace and Copal documents. The graph reads saved/on-disk source; opening it does not save an unsaved Editor buffer.

**Map** keeps the dense strip, **Web** provides a traditional network and **Gravity** uses forces to arrange the same loaded evidence. Use **Graph → Expand selected branch**, **Expand next level** or **Load more for selected branch** to continue bounded exploration. **Hide children** / **Show children** controls a selected branch while retaining its parent and siblings. Hiding tests does not delete or exclude those files from indexing. **View → Explorer** and **Inspector** explain the selected structure and evidence.

The Alpha parser supports Rust, Python, TypeScript/TSX and JavaScript/JSX, including supported module extensions. Expand a file to see its declarations, then inspect calls, imports, references and supported inheritance relationships. Named rules also expose simple overrides, JSX component rendering and FastAPI route registration. Coverage depends on the language and syntax; these are static facts or inferences, not a complete runtime/framework model.

**Indexed metadata** search covers declaration names, qualified names, signatures, documentation and relative paths. Use kind/language/path filters and **Show in graph** to focus a returned symbol. **Source contents · literal** searches current saved text in files admitted to the current code index, using case-sensitive literal matching. It returns file paths with **Open file** and **Read current window** actions. Narrow by path for larger scopes; a partial scan does not prove that other files have no match. Newly added files need index refresh before this search includes them. Content matches remain separate from indexed symbols and relationship evidence.

**View → Graph settings…** separates displayed code nodes from note/document items, connections, loaded data and indexed-file capacity. Layout, labels, motion and rendering preferences change presentation, not the facts represented. Preferences are browser-local for this account/workspace. **Settings → System → Code indexing** controls indexing threads, selected-file capacity, total source bytes, per-file bytes and the derived-generation disk budget (2 GiB default, configurable up to 8 GiB). Saved/requested and effective values can differ while active indexing drains. The disk budget bounds derived index storage; it does not reserve free disk space or increase source/display limits. Use **View → Maximize beside chat** for a split view; on narrow screens **Show chat** keeps the graph available through the minimized-window dock.

Use the **File**, **View** and **Graph** menus rather than looking for a row of persistent action buttons. **Search & evidence** offers detailed queries. Inspect a selected source location and distinguish exact from inferred static relationships; neither is a record of actual execution. **Add selected evidence to chat** prepares context; **Ask about selected evidence** is an explicit question action. Automatic code context is a separate preference.

Before trusting an edge, read the code-folder scope, index state, last verification time and source revision. Automatic source verification checks saved changes while a ready graph is visible; an external edit can wait until the configured interval. After terminal failure, choose **Retry indexing** unless a newer validated in-scope saved-file or root-watch signal permits one recovery attempt. Focus, visibility and periodic timers do not repeatedly restart failed work. Without such a scoped signal, including for nested external edits after failure, retry explicitly. **Refresh now** requests verification. The previous graph can remain visible during rebuilding, but its picture is not evidence that current bytes still match. Valid camera, selection and hidden branches survive a successful same-folder refresh within the restoration budget; unavailable items or restoration limits are reported. A folder preview can exist before dependency evidence is published. Partial coverage, failed/cancelled indexing, stale data and an unavailable backend are not evidence that the project has no relationships. Alpha has bounded exploration and language/platform-dependent coverage; it does not promise complete calls, semantic/vector quality or performance on every repository. Compare with the current saved source.

The following offline scenario needs no live backend or model request. Its optional response is a learner explanation, not proof that you ran Code Graph or verified an execution.

## Actual Alpha previews

These October19 Alpha captures show the same first-party repository during indexing. They document the earlier containment preview and do not illustrate every current control or completed dependency result.

![Code Graph Alpha in Web presentation with nested folder and file containment preview.](/static/treehouse/fixtures/openclank/code-graph-web-alpha-20261019.png)

Web arranges the retained multilevel folder/file preview. Alpha capture during source extraction: containment preview only; dependencies are not yet published. Import/call visibility choices do not prove import/call edges exist.

![Code Graph Alpha in Gravity presentation with the retained folder and file containment preview.](/static/treehouse/fixtures/openclank/code-graph-gravity-alpha-20261019.png)

Gravity rearranges the retained folder/file preview; layout is not additional evidence. Alpha capture during source extraction: containment preview only; dependencies are not yet published. Import/call visibility choices do not prove import/call edges exist.

![Code Graph Alpha View menu showing graph canvas, Explorer, Inspector and relationship visibility commands.](/static/treehouse/fixtures/openclank/code-graph-menus-alpha-20261019.png)

The shared View menu offers graph navigation and visibility commands; Search & evidence is unavailable in this capture. Alpha capture during source extraction: containment preview only; dependencies are not yet published. Import/call visibility choices do not prove import/call edges exist.

## Compare the two revisions

**Offline revision 7:**
```python
def outer():
    return inner()
def inner():
    return 1
```
Index revision 7 records outer → inner at line 2. Current revision 9 is:
```python
def outer():
    return 1
```
That edge is stale for revision 9. An unavailable indexing backend is a separate state from an index with no matches.

A Mind outline of `# Review`, `## Decision`, `## Evidence` reflects headings; those branches are not function calls.
