# Evaluate a family of outputs

- Constructive generation builds directly; search-based generation proposes candidates and evaluates them; constraint solving asks for satisfying configurations. Hybrid pipelines can combine all three.
- Separate validity from quality. A disconnected map is invalid under a reachability rule even if a visual score is high.
- Compare several seeds for quality, diversity and controllability. Expressive range describes what kinds of outputs occur, including gaps and concentrations.
- Quality-diversity search preserves different useful candidates rather than only one highest score. Novelty depends on the chosen representation and distance measure.
- Runtime generation needs bounded time/memory and a fallback; offline generation can spend more time and support human curation.

**Self-check:** Evaluate these maps by (reachable, path length, branch count): A=(yes,8,1), B=(no,2,5), C=(yes,12,4). Choose for a short route goal, then for a branching goal. State one metric the table misses.
