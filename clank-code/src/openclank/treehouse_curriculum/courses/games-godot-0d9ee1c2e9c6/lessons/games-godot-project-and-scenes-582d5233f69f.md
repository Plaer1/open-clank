# Create the small game scene

- Candidate: Godot 4.7.2 standard/GDScript with matching export templates; source-inspected, no author game/export run.
- Create a fresh project with Compatibility renderer where supported. Display→Window: viewport640×360. Create Node2D root Main; save main.tscn. Its direct children must be Label Status at(16,16), Button Restart at(500,16), text Restart, and Timer RoundTimer (one_shot=true, wait_time20).
- Input Map: add restart bound to R; arrows use built-in ui_left/right/up/down. Attach game.gd to Main. Run Project and select main.tscn as main scene. Inspect Output/Debugger for the first error.
- The movement unit supplies the first script; the complete-round unit supplies its full replacement. Do not nest the named children in an extra container.

**Self-check:** Create and run the scene. Report editor version, node tree and one observed UI element. Save source in your own project folder.
