// Trace true polyline corners and share stroke submissions across a dense
// population. Distances are in the same cumulative units as lifecycle paint.
function segmentAt(route, distance) {
  let left=0, right=route.points.length-1;
  while (left+1<right) {
    const mid=(left+right)>>>1;
    if (route.cumulative[mid]<=distance) left=mid;
    else right=mid;
  }
  return left;
}

export function traceKeneRange(path,route,from,to) {
  const start=segmentAt(route,from), end=segmentAt(route,to);
  const point=(i,d,move)=>{
    const a=route.points[i], b=route.points[i+1];
    const t=(d-route.cumulative[i])/(route.cumulative[i+1]-route.cumulative[i] || 1);
    if (move) path.moveTo(a.x+(b.x-a.x)*t,a.y+(b.y-a.y)*t);
    else path.lineTo(a.x+(b.x-a.x)*t,a.y+(b.y-a.y)*t);
  };
  point(start,from,true);
  if (end>start) {
    for (let i=start+1;i<=end;i++) path.lineTo(route.points[i].x,route.points[i].y);
  } else {
    for (let i=start;i>end;i--) path.lineTo(route.points[i].x,route.points[i].y);
  }
  point(end,to,false);
}

export function createKeneTailBatch() {
  const tails=new Map(), heads=[];
  function bucket(color,alpha) {
    const level=Math.round(Math.sqrt(alpha)*32);
    if (!level) return null;
    const key=color*33+level;
    let record=tails.get(key);
    if (!record) { record={color,alpha:(level/32)**2,path:new Path2D()};tails.set(key,record); }
    return record.path;
  }
  return {
    add(route,progress,reverse,steps,color,fade,head) {
      const end=progress*route.total;
      const start=Math.max(0,Math.min(route.total,end+(reverse?1:-1)*steps*.0024*route.total));
      if (end===start || fade<=0) return;
      const bands=8;
      for (let i=0;i<bands;i++) {
        const path=bucket(color,fade*Math.pow((i+.5)/bands,2.25));
        if (path) traceKeneRange(path,route,start+(end-start)*i/bands,start+(end-start)*(i+1)/bands);
      }
      heads.push({head,color,fade});
    },
    draw(ctx,colors,outline,size,intensity) {
      // All outlines go below all colored cores at shared crossings.
      for (const [width,opacity,style] of [[9,.085,'color'],[7.5,.72,'outline'],[3,1,'color']]) {
        ctx.lineWidth=width*size;
        for (const {color,alpha,path} of tails.values()) {
          ctx.strokeStyle=style==='outline'?outline:colors[color];
          ctx.globalAlpha=intensity*alpha*opacity;
          ctx.stroke(path);
        }
      }
      for (const {head,color,fade} of heads) {
        for (const [radius,opacity,fill] of [[8,.12,colors[color]],[5.1,.8,outline],[3.8,1,colors[color]]]) {
          ctx.globalAlpha=intensity*fade*opacity; ctx.fillStyle=fill;
          ctx.beginPath(); ctx.arc(head.x,head.y,radius*size,0,Math.PI*2); ctx.fill();
        }
      }
    },
  };
}
