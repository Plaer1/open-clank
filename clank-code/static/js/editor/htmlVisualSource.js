// Source-only outline: parser spans describe captured text, never live DOM identity.
const TAGS=new Set(['main','section','div','h1','h2','h3','p','span','button','ul','li']);
const PROPS=new Set(['color','background-color','font-size','font-weight','line-height','letter-spacing',
  'padding','margin',...['top','right','bottom','left'].flatMap(side=>[`padding-${side}`,`margin-${side}`])]);
const color=/^#(?:[a-f\d]{3}|[a-f\d]{6})$/i;
const positivePx=/^(?:0|(?:\d+(?:\.\d+)?|\.\d+)px)$/i;
const signedPx=/^(?:0|[-+]?(?:\d+(?:\.\d+)?|\.\d+)px)$/i;
const hasError=node=>Boolean(node.error || node.children?.some(hasError));
// Source declarations only: shorthand/longhand cascade and computed values are not inferred.
const validValue=(name,value)=>{
  if(name==='color'||name==='background-color')return color.test(value);
  if(name==='font-weight')return /^(?:normal|bold|[1-9]00)$/i.test(value);
  const signed=name==='letter-spacing'||name.startsWith('margin-');
  if(!(signed?signedPx:positivePx).test(value))return false;
  const number=Number.parseFloat(value);
  return Number.isFinite(number) && Math.abs(number)<=512 && (name!=='line-height'||number>0);
};

export async function parseHtmlOutline(source) {
  if(typeof source!=='string' || source.length>262144 || /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(source)) throw Error('Source is unavailable or too large.');
  const {htmlSourceTree,cssInlineSourceTree}=await import('../copal/codemirror.js');
  const [tree,digest]=await Promise.all([htmlSourceTree(source),crypto.subtle.digest('SHA-256',new TextEncoder().encode(source))]);
  const sourceSHA256=[...new Uint8Array(digest)].map(x=>x.toString(16).padStart(2,'0')).join('');
  const nodes=[];
  async function visit(node,ancestryValid=true) {
    if(node.type!=='Element') {if(node.type==='Document')for(const child of node.children)await visit(child,ancestryValid);return;}
    const open=node.children.find(c=>c.type==='OpenTag'), close=node.children.find(c=>c.type==='CloseTag');
    const nameNode=open?.children.find(c=>c.type==='TagName'), closeName=close?.children.find(c=>c.type==='TagName');
    const name=nameNode && source.slice(nameNode.from,nameNode.to).toLowerCase();
    const balanced=Boolean(ancestryValid && open && close && !hasError(open) && !hasError(close) && name && closeName && source.slice(closeName.from,closeName.to).toLowerCase()===name);
    if(!balanced)return;
    if(TAGS.has(name)) {
      const attributes=open.children.filter(c=>c.type==='Attribute'), styles=attributes.filter(a=>{const n=a.children.find(c=>c.type==='AttributeName');return n && source.slice(n.from,n.to).toLowerCase()==='style';});
      if(styles.length===1) {
        const attr=styles[0], value=attr.children.find(c=>c.type==='AttributeValue'), raw=value && source.slice(value.from,value.to);
        if(value && raw?.length>=2 && (raw[0]==='"'||raw[0]==="'") && raw.at(-1)===raw[0] && !/[<&]/.test(raw) && raw.length<=4098) {
          const inner=raw.slice(1,-1), css=await cssInlineSourceTree(inner);
          if(!hasError(css)) {
            const found=new Map(), duplicates=new Set();
            for(const decl of css.children.filter(c=>c.type==='Declaration')) {
              const property=decl.children.find(c=>c.type==='PropertyName');if(!property)continue;
              const prop=inner.slice(property.from,property.to).toLowerCase();if(!PROPS.has(prop))continue;
              if(found.has(prop) || duplicates.has(prop)){found.delete(prop);duplicates.add(prop);continue;}
              const part=inner.slice(property.to,decl.to), match=part.match(/^\s*:\s*([#a-z\d.+-]+)\s*$/i);
              if(!match || !validValue(prop,match[1])){duplicates.add(prop);continue;}
              const rel=property.to+part.indexOf(match[1]);found.set(prop,{value:match[1],from:value.from+1+rel,to:value.from+1+rel+match[1].length});
            }
            if(found.size && nodes.length<256) {
              const text=node.children.filter(c=>c.type==='Text').map(c=>source.slice(c.from,c.to)).join(' ').replace(/\s+/g,' ').trim().slice(0,80);
              nodes.push({tag:name,label:text || name,from:open.from,to:open.to,properties:Object.fromEntries(found)});
            }
          }
        }
      }
    }
    for(const child of node.children)if(child.type==='Element')await visit(child,balanced);
  }
  await visit(tree);if(!nodes.length)throw Error('No supported source elements found. Continue in source mode.');
  return {source,sourceSHA256,nodes};
}
export function patchHtmlOutline(model,index,property,value) {
  const span=model?.nodes?.[index]?.properties?.[property];
  if(!span || typeof value!=='string' || !validValue(property,value) || model.source.slice(span.from,span.to)!==span.value)throw Error('Property is unavailable or invalid.');
  return model.source.slice(0,span.from)+value+model.source.slice(span.to);
}

// The source-only locator is data; the Host independently re-parses saved bytes.
export async function htmlFeedbackLocator(model,index,resourceID) {
  const node=model?.nodes?.[index],source=model?.source;
  if (!node || typeof source!=='string' || typeof resourceID!=='string' || !resourceID || source.includes('\r') || source.startsWith('\uFEFF')) throw Error('Static source mapping is unavailable.');
  const encoder=new TextEncoder(), prefix=encoder.encode(source.slice(0,node.from)), bytes=encoder.encode(source.slice(node.from,node.to));
  if (!bytes.length || encoder.encode(source).length>262144) throw Error('Static source mapping is unavailable.');
  const digest=[...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(v=>v.toString(16).padStart(2,'0')).join('');
  return {kind:'dom',sourceID:resourceID,nodeID:`v1:${prefix.length}:${prefix.length+bytes.length}:${digest}`};
}
