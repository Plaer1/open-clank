// A bounded projection of SVG source. The parser supplies exact UTF-16 spans;
// only an allowlisted bounded document can become an interactive visual.
const NUMBER = '[-+]?(?:\\d+(?:\\.\\d*)?|\\.\\d+)(?:[eE][-+]?\\d+)?';
const number = new RegExp(`^${NUMBER}$`);
const numeric = (s, limit=1e7) => number.test(s) && Number.isFinite(Number(s)) && Math.abs(Number(s)) <= limit;
const transformPart = new RegExp(`(translate|scale)\\([ \t]*(${NUMBER})(?:(?:[ \t]*,[ \t]*|[ \t]+)(${NUMBER}))?[ \t]*\\)`, 'y');
function validTransform(value) {
  if (typeof value !== 'string' || value.length > 256) return false;
  let offset=0,count=0;
  while (offset < value.length) {
    const spaces=/^[ \t]*/.exec(value.slice(offset))[0].length;
    if (count && !spaces) return false;
    offset+=spaces;if(offset===value.length)break;
    transformPart.lastIndex=offset;const match=transformPart.exec(value);
    if (!match || ++count>2) return false;
    const limit=match[1]==='scale'?100:1e7;
    if (!numeric(match[2],limit) || (match[3]!==undefined && !numeric(match[3],limit))) return false;
    offset=transformPart.lastIndex;
  }
  return count>0;
}
const PAINT=/^(?:none|white|#(?:[a-f\d]{3}|[a-f\d]{6}))$/i;
const ROLE=/^var\((--oc-(?:accent|bg|fg|outline|panel|raised|well)),[ \t]*(#(?:[a-fA-F\d]{3}|[a-fA-F\d]{6}))\)$/;
export function svgPaintFallback(value) {
  if (typeof value!=='string') return null;
  if (PAINT.test(value)) return value;
  return ROLE.exec(value)?.[2] || null;
}
function validPath(value) {
  if (typeof value!=='string' || !value || value.length>8192) return false;
  const command=/^[MmLlHhVvQqAaZz]$/, token=new RegExp(`${NUMBER}`,'y'), tokens=[];
  let offset=0,previous='',comma=false;
  while(offset<value.length) {
    let spaces=0;while(offset<value.length && /[ \t\r\n]/.test(value[offset])){offset++;spaces++;}
    if(offset===value.length) return !comma && validate();
    if(value[offset]===',') {if(previous!=='number' || comma)return false;comma=true;offset++;continue;}
    const ch=value[offset];
    if(command.test(ch)) {if(comma)return false;tokens.push(ch);offset++;previous='command';continue;}
    token.lastIndex=offset;const match=token.exec(value);
    if(!match || (previous==='number' && !spaces && !comma && ch!=='+' && ch!=='-'))return false;
    if(!numeric(match[0]))return false;
    tokens.push(match[0]);offset=token.lastIndex;previous='number';comma=false;
    if(tokens.length>2048)return false;
  }
  return !comma && validate();
  function validate() {
    let i=0,active='',started=false,segments=0;
    const arity={M:2,L:2,H:1,V:1,Q:4,A:7};
    while(i<tokens.length) {
      if(command.test(tokens[i])) {
        active=tokens[i++];
        if(!started && active.toUpperCase()!=='M')return false;
        if(active.toUpperCase()==='Z'){if(!started)return false;active='';if(++segments>256)return false;continue;}
      }
      if(!active || i>=tokens.length || command.test(tokens[i]))return false;
      const kind=active.toUpperCase(),n=arity[kind];if(!n || i+n>tokens.length)return false;
      const args=tokens.slice(i,i+n);if(args.some(v=>command.test(v)))return false;
      if(kind==='A' && (Number(args[0])<0 || Number(args[1])<0 || !['0','1'].includes(args[3]) || !['0','1'].includes(args[4])))return false;
      i+=n;started=true;if(++segments>256)return false;
      if(kind==='M')active=active==='M'?'L':'l';
    }
    return started;
  }
}
const PRESENTATION=['fill','stroke','stroke-width','opacity','fill-opacity','stroke-opacity','stroke-linecap','stroke-linejoin','fill-rule'];
function validProperty(name,value) {
  if (name==='fill' || name==='stroke') return svgPaintFallback(value)!==null;
  if (name==='transform') return validTransform(value);
  if (name==='d') return validPath(value);
  if (name==='stroke-linecap') return ['butt','round','square'].includes(value);
  if (name==='stroke-linejoin') return ['miter','round','bevel'].includes(value);
  if (name==='fill-rule') return ['nonzero','evenodd'].includes(value);
  if (['opacity','fill-opacity','stroke-opacity'].includes(name)) return numeric(value,1) && Number(value)>=0;
  if (name==='stroke-width') return numeric(value,1e4) && Number(value)>=0;
  return numeric(value) && (!['width','height','r','rx','ry'].includes(name) || Number(value)>0 || (['rx','ry'].includes(name) && Number(value)===0));
}

export async function parseVisualSvg(source) {
  if (typeof source !== 'string' || source.length > 262144) throw Error('SVG source is unavailable or too large.');
  const { xmlSourceTree } = await import('../copal/codemirror.js');
  const tree = await xmlSourceTree(source);
  const fail = () => { throw Error('Unsupported SVG source. Edit this file in source mode.'); };
  const children = n => n.children || [];
  const doc = children(tree).filter(n=>!(n.type==='Text' && !source.slice(n.from,n.to).trim()));
  if (doc.length !== 1 || doc[0].type !== 'Element') fail();
  const element = doc[0], parts = children(element), open = parts[0], close = parts.at(-1);
  if (open?.type !== 'OpenTag' || close?.type !== 'CloseTag') fail();
  function tag(node, expected, allowed) {
    const nodes = children(node), name = nodes.find(n => n.type === 'TagName');
    if (!name || source.slice(name.from,name.to) !== expected) fail();
    const attrs = {};
    for (const n of nodes) {
      if (n.type !== 'Attribute') continue;
      const c = children(n), key = c.find(x => x.type === 'AttributeName'), value = c.find(x => x.type === 'AttributeValue');
      if (!key || !value || c.length !== 3 || c[1].type !== 'Is') fail();
      const k = source.slice(key.from,key.to), raw = source.slice(value.from,value.to);
      if (!allowed.includes(k) || attrs[k] || raw.length < 2 || !['"',"'"].includes(raw[0]) || raw.at(-1) !== raw[0] || /[<&]/.test(raw)) fail();
      attrs[k] = {value:raw.slice(1,-1),from:value.from+1,to:value.to-1};
    }
    return attrs;
  }
  const root = tag(open,'svg',['xmlns','viewBox','width','height']);
  if (root.xmlns && root.xmlns.value !== 'http://www.w3.org/2000/svg') fail();
  const vb = root.viewBox?.value.trim().split(/[\s,]+/);
  if (!vb || vb.length !== 4 || !vb.every(value=>numeric(value)) || Number(vb[2]) <= 0 || Number(vb[3]) <= 0) fail();
  for (const k of ['width','height']) if (root[k] && (!numeric(root[k].value) || Number(root[k].value) <= 0)) fail();
  if (source.slice(close.from,close.to) !== '</svg>') fail();
  const shapes = [], items = [];let title='';
  function visit(n, depth) {
    if (depth > 8 || n.type !== 'Element' || items.length >= 256) fail();
    const p = children(n), first = p[0], last = p.at(-1);
    const name = children(first).find(x => x.type === 'TagName');
    const kind = name && source.slice(name.from,name.to);
    if (kind === 'g') {
      if (depth >= 8) fail();
      if (first?.type !== 'OpenTag' || last?.type !== 'CloseTag' || source.slice(last.from,last.to) !== '</g>') fail();
      const attrs = tag(first,'g',['transform',...PRESENTATION]);
      for (const [key,value] of Object.entries(attrs)) if(!validProperty(key,value.value)) fail();
      const item = {kind,attrs,children:[]}; items.push(item);
      for (const child of p.slice(1,-1)) {
        if (child.type === 'Text' && !source.slice(child.from,child.to).trim()) continue;
        item.children.push(visit(child,depth+1));
      }
      return item;
    }
    if (p.length !== 1 || first?.type !== 'SelfClosingTag') fail();
    const geometry = {rect:['x','y','width','height','rx','ry'],circle:['cx','cy','r'],ellipse:['cx','cy','rx','ry'],path:['d']}[kind];
    if (!geometry) fail();
    const attrs = tag(first,kind,[...geometry,...PRESENTATION,'transform']);
    const required={rect:['width','height'],circle:['cx','cy','r'],ellipse:['cx','cy','rx','ry'],path:['d']}[kind];
    if (!required.every(k => attrs[k])) fail();
    for (const [key,value] of Object.entries(attrs)) if(!validProperty(key,value.value)) fail();
    const item = {kind,attrs,children:[]};items.push(item);shapes.push(item);return item;
  }
  const nodes=[];
  for (const n of parts.slice(1,-1)) {
    if (n.type === 'Text' && !source.slice(n.from,n.to).trim()) continue;
    if (n.type==='Element' && children(n)[0]?.type==='OpenTag' && children(children(n)[0]).some(x=>x.type==='TagName' && source.slice(x.from,x.to)==='title')) {
      const parts=children(n),open=parts[0],close=parts.at(-1);
      if(title || close?.type!=='CloseTag' || source.slice(close.from,close.to)!=='</title>' || children(open).some(x=>x.type==='Attribute') || parts.slice(1,-1).some(x=>x.type!=='Text'))fail();
      const raw=parts.slice(1,-1).map(x=>source.slice(x.from,x.to)).join('');if(raw.length>256 || /[<&]/.test(raw))fail();title=raw;continue;
    }
    nodes.push(visit(n,0));
  }
  if (!shapes.length) fail();
  return {source,viewBox:vb.join(' '),shapes,items,nodes,title};
}

export function patchVisualSvg(model, index, property, value) {
  const shape = model?.items?.[index], span = shape?.attrs?.[property];
  if (!span || typeof value !== 'string') throw Error('Property is unavailable.');
  if (!validProperty(property,value)) throw Error('Enter a supported literal value.');
  return model.source.slice(0,span.from)+value+model.source.slice(span.to);
}
