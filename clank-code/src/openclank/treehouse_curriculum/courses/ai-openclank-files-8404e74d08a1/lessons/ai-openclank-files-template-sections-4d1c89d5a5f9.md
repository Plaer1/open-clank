# Insert and revise one template section

- Open Review in Editor and place the cursor deliberately. Choose Tools → Insert template, then select the intended template.

**Complete offline inputs:**
```markdown
# Review
Status: draft

See the details note.
```
Insert after the last line:
```markdown
## Decision
Choice: undecided
Reason: add one sentence.
```
The requested inserted result is:
```markdown
## Decision
Choice: keep
Reason: clear
```
The supplied save receipt names Review revision 2 with this section. The template source still says undecided. A reopen of Review revision 2 contains keep/clear.

If no template insertion control is available, record that limitation; a manually pasted section does not prove the template workflow ran.
