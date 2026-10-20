import test from 'node:test';
import assert from 'node:assert/strict';
import {mediaImageGeometry,suppressBitmap,restoreBitmap,BITMAP_OUTSIDE_POSITION} from '../../static/js/graphics/mediaImageGeometry.js';
const geometry=(style={},extra={})=>mediaImageGeometry({x:10,y:20,width:100,height:100,naturalWidth:200,naturalHeight:100,style,...extra});
test('cover clips native horizontal and vertical overflow through UVs, preserving destination box',()=>{
 assert.deepEqual(geometry({objectFit:'cover'}).uv,[.25,0,.75,1]);
 assert.deepEqual(geometry({objectFit:'cover'},{naturalWidth:100,naturalHeight:200}).uv,[0,.25,1,.75]);
 assert.deepEqual(geometry({objectFit:'cover',objectPosition:'0% 0%'}).uv,[0,0,.5,1]);
 assert.deepEqual(geometry({objectFit:'cover',objectPosition:'100% 100%'}).uv,[.5,0,1,1]);
 assert.deepEqual(geometry({objectFit:'cover',objectPosition:'25px 0px'}).uv,[0,0,.375,1]);
 assert.deepEqual(geometry({objectFit:'cover',objectPosition:'-125px 0px'}).uv,[.625,0,1,1]);
 assert.deepEqual(geometry({objectFit:'cover',objectPosition:'top right'}).uv,[.5,0,1,1]);
 assert.deepEqual(geometry({objectFit:'cover',objectPosition:'bottom'},{naturalWidth:100,naturalHeight:200}).uv,[0,.5,1,1]);
});
test('contain and equal intrinsic box preserve native placement without forced upscale or stretch',()=>{
 const g=geometry({objectFit:'contain'});assert.deepEqual([g.x,g.y,g.width,g.height],[10,45,100,50]);assert.deepEqual(g.uv,[0,0,1,1]);
 const equal=geometry({objectFit:'contain'},{naturalWidth:100,naturalHeight:100});assert.deepEqual([equal.x,equal.y,equal.width,equal.height],[10,20,100,100]);
 assert.equal(geometry({objectFit:'contain',objectPosition:BITMAP_OUTSIDE_POSITION}),null);
 assert.equal(geometry({objectFit:'cover',objectPosition:'1000000px 1000000px'}),null);
 assert.equal(geometry({objectFit:'cover',objectPosition:BITMAP_OUTSIDE_POSITION}),null);
 const scrolled=geometry({objectFit:'cover'},{x:-20,y:-40});assert.deepEqual(scrolled.uv,[.25,0,.75,1]);assert.equal(scrolled.clip.x,-20);assert.equal(scrolled.clip.y,-40);
});
test('border, padding and circular/elliptical radius remain excluded from bitmap destination',()=>{
 const g=geometry({objectFit:'cover',borderLeftWidth:'2px',borderRightWidth:'2px',borderTopWidth:'2px',borderBottomWidth:'2px',paddingLeft:'3px',paddingRight:'3px',paddingTop:'3px',paddingBottom:'3px',borderTopLeftRadius:'12px 14px',borderTopRightRadius:'12px',borderBottomRightRadius:'12px',borderBottomLeftRadius:'12px'});
 assert.deepEqual(g.clip,{x:15,y:25,width:90,height:90,rx:[7,7,7,7],ry:[9,7,7,7]});assert.equal(g.x,15);assert.equal(g.width,90);
});
test('unsupported effects/positions and translucent image keep ordinary rendering',()=>{
 for(const style of [{filter:'blur(2px)'},{transform:'rotate(1deg)'},{mixBlendMode:'multiply'},{opacity:'.5'},{objectPosition:'calc(50% + 2px) 50%'},{transitionDuration:'0.2s'},{animationName:'spin'}])assert.equal(geometry(style),null);
});
test('suppress only bitmap position; preserve border/background/opacity and exact authored priority',()=>{
 const values=new Map([['object-position',['20% 40%','important']],['border',['1px solid red','']],['background',['blue','']],['opacity',['1','']]]);
 const style={getPropertyValue:p=>values.get(p)?.[0]||'',getPropertyPriority:p=>values.get(p)?.[1]||'',setProperty:(p,v,priority='')=>values.set(p,[v,priority]),removeProperty:p=>values.delete(p)},image={style};
 const old=suppressBitmap(image);assert.equal(style.getPropertyValue('object-position'),BITMAP_OUTSIDE_POSITION);assert.equal(style.getPropertyValue('border'),'1px solid red');assert.equal(style.getPropertyValue('background'),'blue');assert.equal(style.getPropertyValue('opacity'),'1');
 restoreBitmap(image,old);assert.deepEqual(values.get('object-position'),['20% 40%','important']);
 const second=suppressBitmap(image);style.setProperty('object-position','right bottom');restoreBitmap(image,second);assert.equal(style.getPropertyValue('object-position'),'right bottom');
});
test('CSSOM numeric canonicalization does not prevent restoration',()=>{
 let value='50% 20%',priority='important';
 const image={style:{getPropertyValue:()=>value,getPropertyPriority:()=>priority,setProperty(_,v,p=''){value=v===BITMAP_OUTSIDE_POSITION?'-1e+06px -1e+06px':v;priority=p;},removeProperty(){value='';priority='';}}};
 const old=suppressBitmap(image);assert.equal(old.applied,'-1e+06px -1e+06px');restoreBitmap(image,old);assert.equal(value,'50% 20%');assert.equal(priority,'important');
});
