# Choose how structure grows

- Binary space partitioning divides a region and can connect rooms across splits. A digger agent grows paths by local actions; a cellular automaton updates a grid from neighborhood rules.
- A grammar rewrites symbols. With A→AB and B→A, simultaneous steps from A give AB then ABA. Interpret symbols as rooms, branches, notes or actions after defining their meaning.
- L-systems use repeated rewriting for growth; a graph grammar can alter connectivity. Keep a size/iteration bound so a small rule cannot produce unlimited work.
- Content may be terrain, plants, items, quests, music or motion. Each needs its own validity test.

**Self-check:** Perform one more rewrite after ABA. For a dungeon, choose partitioning, a digger or a cellular rule and identify one structure it naturally controls and one it does not guarantee.
