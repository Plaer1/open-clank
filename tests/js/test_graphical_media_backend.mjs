import test from 'node:test';
import assert from 'node:assert/strict';
import {createWebGL2Backend} from '../../static/js/graphics/backend-webgl2.js';
function glFixture({fail=false}={}){
 const count={uploads:0,textures:0,deletes:0,draws:0,uniforms:[],vertices:[],shaders:[]};
 const gl=new Proxy({NO_ERROR:0,getError:()=>fail?1285:0,getShaderParameter:()=>true,getProgramParameter:()=>true,getUniformLocation:(_,name)=>name,uniform4f:(...args)=>count.uniforms.push(args),uniform1i:(...args)=>count.uniforms.push(args),bufferSubData:(_,__,data)=>{if(data instanceof Float32Array)count.vertices.push([...data]);},shaderSource:(_,source)=>count.shaders.push(source),texImage2D(){count.uploads++;},createTexture(){count.textures++;return {};},deleteTexture(){count.deletes++;},drawElements(){count.draws++;}}, {get(target,key){if(key in target)return target[key];if(key.startsWith('create'))return ()=>({});if(key===key.toUpperCase())return 1;return ()=>{};}});
 const canvas={width:100,height:100,get ownerDocument(){throw Error('Direct media must not allocate image atlas scratch');}};
 return {gl,canvas,count};
}
test('media-only direct image submission reports upload success and caches one texture without atlas scratch',()=>{
 const f=glFixture(),backend=createWebGL2Backend({canvas:f.canvas,gl:f.gl,directImages:true});
 const image={naturalWidth:32,naturalHeight:32,width:32,height:32};
 backend.beginFrame();assert.equal(backend.drawImage(image,0,0,32,32),true);backend.endFrame();
 backend.beginFrame();assert.equal(backend.drawImage(image,0,0,32,32),true);backend.endFrame();
 assert.equal(f.count.uploads,1);assert.equal(f.count.textures,1);assert.equal(f.count.draws,2);
 backend.clearImageSources();assert.equal(f.count.deletes,1);backend.dispose();
});
test('media-only allocation error refuses draw and disposes the failed texture',()=>{
 const f=glFixture({fail:true}),backend=createWebGL2Backend({canvas:f.canvas,gl:f.gl,directImages:true});
 backend.beginFrame();assert.equal(backend.drawImage({width:32,height:32},0,0,32,32),false);backend.endFrame();
 assert.equal(f.count.draws,0);assert.equal(f.count.deletes,1);backend.dispose();
});
test('same texture batches flush on rounded clip change and reset clip for ordinary draws',()=>{
 const f=glFixture(),backend=createWebGL2Backend({canvas:f.canvas,gl:f.gl,directImages:true}),image={width:200,height:100};
 const shape={uv:[.25,0,.75,1],clip:{x:2,y:3,width:50,height:50,rx:[4,5,6,7],ry:[8,9,10,11]}};
 backend.beginFrame();assert.equal(backend.drawImage(image,2,3,50,50,1,shape),true);assert.equal(backend.drawImage(image,55,3,50,50),true);backend.endFrame();
 assert.equal(f.count.draws,2);assert.equal(f.count.uploads,1);
 assert.deepEqual(f.count.uniforms.filter(row=>row[0]==='uImageClip').map(row=>row[1]),[1,0]);
 assert.deepEqual(f.count.uniforms.find(row=>row[0]==='uImageBounds'),['uImageBounds',2,3,50,50]);
 assert.deepEqual(f.count.uniforms.find(row=>row[0]==='uImageRadiiX'),['uImageRadiiX',4,5,6,7]);
 assert.deepEqual(f.count.vertices[0].filter((_,i)=>i%8===2),[.25,.75,.75,.25]);
 assert(f.count.shaders.some(source=>source.includes('corner < 4')&&source.includes('discard')));backend.dispose();
});
test('invalid rounded/crop primitive fails before any image allocation',()=>{
 const f=glFixture(),backend=createWebGL2Backend({canvas:f.canvas,gl:f.gl,directImages:true}),image={width:10,height:10};
 assert.equal(backend.drawImage(image,0,0,10,10,1,{uv:[1,0,0,1],clip:{}}),false);assert.equal(f.count.uploads,0);assert.equal(f.count.textures,0);backend.dispose();
});
