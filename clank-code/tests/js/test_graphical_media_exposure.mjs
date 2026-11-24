import test from 'node:test';
import assert from 'node:assert/strict';
import {mediaImageExposed,translationOnlyTransform} from '../../static/js/graphics/mediaImageExposure.js';
test('native modal final identity and pure translation preserve image geometry; scale/rotation/perspective stay ordinary',()=>{
 for(const value of ['none','matrix(1, 0, 0, 1, 0, 0)','matrix(1, 0, 0, 1, -300, 120)'])assert.equal(translationOnlyTransform(value),true);
 for(const value of ['matrix(0.95, 0, 0, 0.95, 0, 8)','matrix(0, 1, -1, 0, 0, 0)','matrix3d(1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1)','translate(10px)','matrix(1,0,0,1,NaN,0)'])assert.equal(translationOnlyTransform(value),false);
});
function fixture(){
 const image={dataset:{},children:[],getBoundingClientRect:()=>({left:20,top:20,right:120,bottom:100,width:100,height:80})};
 const root={dataset:{},children:[image],getBoundingClientRect:()=>({left:0,top:0,right:200,bottom:150,width:200,height:150})};image.parentElement=root;
 const styles=new Map([[root,{transform:'matrix(1, 0, 0, 1, 0, 0)'}]]);
 const win={getComputedStyle:node=>({display:'block',visibility:'visible',opacity:'1',zIndex:'auto',...styles.get(node)})};
 const doc={elementFromPoint:()=>image};return {image,root,styles,win,doc};
}
test('small corner sibling between ownership samples is still rejected',()=>{
 const f=fixture();assert.equal(mediaImageExposed(f),true);
 f.root.children.push({dataset:{},children:[],getBoundingClientRect:()=>({left:40,top:29,right:46,bottom:35})});assert.equal(mediaImageExposed(f),false);
});
test('ancestor clip/filter and inspection-budget exhaustion are truthful ordinary fallback',()=>{
 const f=fixture();f.styles.set(f.root,{clipPath:'inset(0 round 10px)'});assert.equal(mediaImageExposed(f),false);
 f.styles.set(f.root,{filter:'blur(2px)'});assert.equal(mediaImageExposed(f),false);f.styles.clear();assert.equal(mediaImageExposed({...f,work:{remaining:1}}),false);
});
const rect=(left=0,top=0,right=200,bottom=150)=>({left,top,right,bottom,width:right-left,height:bottom-top});
function stacked(){
 const f=fixture(),body={dataset:{},children:[],getBoundingClientRect:()=>rect()},modal={dataset:{},children:[f.root],getBoundingClientRect:()=>rect()},behind={dataset:{},children:[],getBoundingClientRect:()=>rect()};
 f.root.parentElement=modal;modal.parentElement=body;behind.parentElement=body;body.children=[behind,modal];f.styles.set(f.root,{transform:'none'});f.styles.set(modal,{position:'fixed',zIndex:'9000'});return{...f,body,modal,behind};
}
test('nearest existing ancestor context admits lower app overlap without adding isolation or z-index',()=>{const f=stacked();assert(mediaImageExposed(f));assert.equal(f.styles.get(f.root).transform,'none');});
test('sibling inspection continues inside existing context, including tiny corner overlay outside hit probes',()=>{const f=stacked();f.modal.children.push({dataset:{},children:[],getBoundingClientRect:()=>rect(40,29,46,35)});assert.equal(mediaImageExposed(f),false);});
test('external above overlay stays in shared context paint order; sampled external obstruction still rejects',()=>{const f=stacked(),overlay={};f.body.children.push(overlay);assert(mediaImageExposed(f),'tiny external sibling cannot be overpainted by canvas inside modal context');f.doc.elementFromPoint=()=>overlay;assert.equal(mediaImageExposed(f),false);});
test('no known context keeps full conservative sibling walk and root must be an actual ancestor',()=>{const f=stacked();f.styles.set(f.modal,{position:'relative',zIndex:'auto'});assert.equal(mediaImageExposed(f),false);f.body.children=[f.modal];assert(mediaImageExposed(f),'disjoint unknown-context path retains old behavior');assert.equal(mediaImageExposed({...f,root:{}}),false);});
test('all ancestor clip/mask/transform/visibility checks continue above the context boundary',()=>{for(const change of [{clipPath:'inset(0)'},{maskImage:'linear-gradient(black,transparent)'},{filter:'blur(1px)'},{transform:'matrix(2,0,0,2,0,0)'},{opacity:'0'}]){const f=stacked();f.styles.set(f.body,change);assert.equal(mediaImageExposed(f),false);}const f=stacked();assert.equal(mediaImageExposed({...f,work:{remaining:10}}),false);});
test('image-local clip/mask and below-root opacity/blend cannot be reproduced by root sibling canvas',()=>{for(const style of [{clipPath:'circle(40%)'},{maskImage:'linear-gradient(black,transparent)'}]){const f=stacked();f.styles.set(f.image,style);assert.equal(mediaImageExposed(f),false);}for(const style of [{opacity:'.5'},{mixBlendMode:'multiply'}]){const f=stacked(),wrapper={dataset:{},children:[f.image],parentElement:f.root,getBoundingClientRect:()=>rect()};f.image.parentElement=wrapper;f.root.children=[wrapper];f.styles.set(wrapper,style);assert.equal(mediaImageExposed(f),false);}});
test('shared root/above-root opacity and blending remain shared image/canvas effects',()=>{for(const style of [{opacity:'.5'},{mixBlendMode:'multiply'}]){const f=stacked();f.styles.set(f.root,style);assert(mediaImageExposed(f));const g=stacked();g.styles.set(g.modal,{position:'fixed',zIndex:'9000',...style});assert(mediaImageExposed(g));}});
test('oversized sibling child collections refuse before eager enumeration or enqueue',()=>{
 const f=stacked();let read=0;const children={length:1000000,get 0(){read++;throw Error('must not enumerate');}};
 f.root.children.push({dataset:{},children,getBoundingClientRect:()=>rect(400,400,410,410)});assert.equal(mediaImageExposed(f),false);assert.equal(read,0);
 const g=stacked();g.root.children=children;assert.equal(mediaImageExposed(g),false);assert.equal(read,0);
});
function rounded(size=28,radius=4){
 const f=stacked(),box=rect(20,20,20+size,20+size),imageBox={...box},wrapper={dataset:{},parentElement:f.root,children:[f.image],getBoundingClientRect:()=>box};f.root.children=[wrapper];f.image.parentElement=wrapper;f.image.getBoundingClientRect=()=>imageBox;
 const style={transform:'none',overflowX:'hidden',overflowY:'hidden'};for(const side of ['Left','Top','Right','Bottom']){style['border'+side+'Width']='0px';style['padding'+side]='0px';}for(const c of ['TopLeft','TopRight','BottomLeft','BottomRight'])style['border'+c+'Radius']=radius+'px';
 f.styles.set(wrapper,{...style});f.styles.set(f.image,{...style});for(const n of [f.image,wrapper])Object.assign(n,{scrollLeft:0,scrollTop:0,clientWidth:size,clientHeight:size,scrollWidth:size,scrollHeight:size});return{...f,wrapper,box,imageBox};
}
test('identical immediate Files wrapper/image circular clips are redundant for list/grid/gallery sizes',()=>{for(const [size,radius]of [[28,4],[52,4],[64,4],[86,5],[28,40]]){const f=rounded(size,radius);assert(mediaImageExposed(f));}});
test('rounded clip support never widens mismatched box/radius/ellipse/percent/border/padding/scroll/transform',()=>{
 for(const change of [f=>f.imageBox.left+=.0001,f=>f.styles.get(f.wrapper).borderTopLeftRadius='8px',f=>f.styles.get(f.image).borderTopLeftRadius='4px 4px',f=>f.styles.get(f.image).borderTopLeftRadius='10%',f=>f.styles.get(f.wrapper).paddingLeft='1px',f=>f.styles.get(f.image).borderLeftWidth='1px',f=>f.wrapper.scrollLeft=1,f=>f.wrapper.scrollWidth++,f=>f.styles.get(f.wrapper).transform='matrix(1, 0, 0, 1, 0, 0)',f=>f.styles.get(f.wrapper).overflowX='auto']){const f=rounded();change(f);assert.equal(mediaImageExposed(f),false);}
 const f=rounded(),inner={dataset:{},children:[f.image],parentElement:f.wrapper,getBoundingClientRect:f.image.getBoundingClientRect};f.image.parentElement=inner;f.wrapper.children=[inner];assert.equal(mediaImageExposed(f),false,'non-immediate rounded ancestor remains ordinary');
});
