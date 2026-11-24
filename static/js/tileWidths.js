// Reshape the existing tile grid around movable column boundaries. All panes
// on a boundary share its position, including applets stacked in quadrants.
const COMPACT_CHAT_WIDTH = 260;

function fitColumns(layout, bounds, zones, preferences, appletMinimum, chatMinimum) {
  if (!zones.length || bounds.width <= 0) return {...layout,seams:[]};
  const pixelKey = value => Math.round(value * 1000);
  const edges = [...new Set([bounds.left,bounds.right,...layout.tiles.flatMap(pane => [pane.left,pane.right]),
    ...(layout.chat ? [layout.chat.left,layout.chat.right] : [])].map(pixelKey))].sort((a,b) => a-b);
  const panes = [...layout.tiles,...(layout.chat ? [layout.chat] : [])].map((rect,index) => ({
    rect, left:edges.indexOf(pixelKey(rect.left)), right:edges.indexOf(pixelKey(rect.right)),
    min:Math.min(rect.width,index===layout.tiles.length ? chatMinimum : appletMinimum),
  }));
  const layoutKey = JSON.stringify(panes.map((pane,index) => [zones[index] || 'chat',pane.left,pane.right])
    .sort((a,b) => a[0].localeCompare(b[0])));
  const columns = edges.map(edge => edge/1000);
  columns[0] = bounds.left;
  columns[columns.length-1] = bounds.right;

  const limits = edge => {
    let min = columns[edge-1], max = columns[edge+1];
    for (const pane of panes) {
      if (pane.right === edge) min = Math.max(min,columns[pane.left]+pane.min);
      if (pane.left === edge) max = Math.min(max,columns[pane.right]-pane.min);
    }
    return {min,max};
  };
  const desired = preferences.get(layoutKey);
  if (desired) {
    // A neighboring boundary can make room for a previously constrained one.
    for (let pass=0; pass<2; pass++) {
      for (const [edge,fraction] of desired) {
        const {min,max} = limits(edge);
        if (max >= min) columns[edge] = Math.max(min,Math.min(max,bounds.left+fraction*bounds.width));
      }
    }
  }
  const placed = panes.map(({rect,left,right}) => ({
    ...rect,left:columns[left],right:columns[right],width:columns[right]-columns[left],
  }));
  const seams = [];
  for (let edge=1; edge<columns.length-1; edge++) {
    const {min,max} = limits(edge);
    // Keep a focused/dragged handle mounted while another divider temporarily
    // constrains its range.
    const touching = panes.filter(pane => pane.left===edge || pane.right===edge);
    if (!touching.length) continue;
    const top = Math.min(...touching.map(pane => pane.rect.top));
    const bottom = Math.max(...touching.map(pane => pane.rect.bottom));
    seams.push({layout:layoutKey,edge,x:columns[edge],top,height:bottom-top,
      min,max:Math.max(min,max),left:bounds.left,width:bounds.width});
  }
  return {tiles:placed.slice(0,zones.length),chat:layout.chat ? placed.at(-1) : null,seams};
}

function transpose(rect) {
  if (!rect) return null;
  return {left:rect.top, right:rect.bottom, width:rect.height,
    top:rect.left, bottom:rect.right, height:rect.width};
}

export function fitTileSplits(layout, bounds, zones, preferences, {width, height, chatHeight}) {
  const columns = fitColumns(layout, bounds, zones, preferences.x, width, COMPACT_CHAT_WIDTH);
  const rows = fitColumns({
    tiles:columns.tiles.map(transpose), chat:transpose(columns.chat),
  }, transpose(bounds), zones, preferences.y, height, chatHeight);
  const mapSeams = (seams, axis) => seams.map(seam => ({
    axis, layout:seam.layout, edge:seam.edge, position:seam.x, across:seam.top,
    span:seam.height, origin:seam.left, extent:seam.width, min:seam.min, max:seam.max,
  }));
  // Row boundaries can extend a column handle after the first layout pass.
  const columnSeams = fitColumns({
    tiles:rows.tiles.map(transpose), chat:transpose(rows.chat),
  }, bounds, zones, preferences.x, width, COMPACT_CHAT_WIDTH).seams;
  return {tiles:rows.tiles.map(transpose), chat:transpose(rows.chat),
    seams:[...mapSeams(columnSeams, 'x'), ...mapSeams(rows.seams, 'y')]};
}

export function fitTileWidths(layout, bounds, zones, preferences, appletMinimum) {
  return fitColumns(layout, bounds, zones, preferences, appletMinimum, COMPACT_CHAT_WIDTH);
}
