/**
 * Assign inclusive date ranges to the minimum number of deterministic lanes.
 *
 * Events whose end day equals another event's start day overlap because both
 * occupy that planning day. Lane state is display-only; callers keep source
 * records unchanged.
 */
export function assignEventLanes(events) {
  const normalized = (events || []).map((event, sourceIndex) => {
    const startDay = Number.isFinite(event.startDay) ? event.startDay : 0;
    const rawEnd = Number.isFinite(event.endDay) ? event.endDay : startDay;
    return {
      ...event,
      startDay,
      endDay: Math.max(startDay, rawEnd),
      stableId: String(event.stableId ?? sourceIndex),
      sourceIndex,
    };
  });

  normalized.sort((a, b) => (
    a.startDay - b.startDay
    || a.endDay - b.endDay
    || a.stableId.localeCompare(b.stableId)
    || a.sourceIndex - b.sourceIndex
  ));

  const laneEnds = [];
  for (const event of normalized) {
    let lane = laneEnds.findIndex((endDay) => endDay < event.startDay);
    if (lane < 0) lane = laneEnds.length;
    laneEnds[lane] = event.endDay;
    event.lane = lane;
  }

  return { items: normalized, laneCount: laneEnds.length };
}
