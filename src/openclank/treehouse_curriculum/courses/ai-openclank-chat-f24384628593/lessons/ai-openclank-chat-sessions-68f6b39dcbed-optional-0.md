- Ordinary chat toolbar Edit, Regenerate and Fork affect different histories. Inspecting a transcript is not a model call; resubmitting or regenerating starts new inference.

**Four-turn transcript in session C4:**
1. User: `Name the draft Blue.`
2. Assistant: `Draft name: Blue.`
3. User: `Write a one-line title.`
4. Assistant: `Blue review.`

Goal: try the name Green while retaining C4 unchanged. Select the boundary after turn 2 and use ordinary Fork. The supplied successful fork receipt returns new session C5 with turns 1–2; C4 retains all four. Continue the alternative in C5.

Editing a selected user message and resubmitting truncates the affected current branch and sends the changed request. Regenerate truncates the affected answer path and sends a new request. Neither is merely a view of old output. A native session fork is a separate workflow.

**Try on paper:** choose a control for the stated goal, list C4/C5's supplied prefixes, and identify which later action invokes inference. **Check:** Fork preserves C4; C5 receives its own identity/prefix; a subsequent send invokes the model. A failed/unavailable fork has no proven new session.