import { StreamLanguage, type StreamParser, type StringStream } from '@codemirror/language';
import { tags } from '@lezer/highlight';
import type { markdown } from '@codemirror/lang-markdown';
type MarkdownConfig = NonNullable<NonNullable<Parameters<typeof markdown>[0]>['extensions']>;

// First-party lexical adapters. They recognize source tokens and preserve multiline
// state; they are not engine compilers, TextMate copies, or semantic validators.
type Rule = { expression: RegExp; style: string };
type Config = {
  keywords?: string; types?: string; builtins?: string; atoms?: string;
  lineComments?: string[]; blockComments?: Array<[string,string]>;
  nestedComments?: boolean; caseInsensitive?: boolean; tripleStrings?: boolean;
  multilineStrings?: boolean; strings?: string[]; doubledQuotes?: boolean; interpolation?: RegExp;
  rules?: Rule[]; preprocessor?: boolean; identifier?: RegExp;
  luaLongStrings?: boolean; cartridge?: boolean;
  lineRule?: (text: string, line: number) => string | null;
  commentForms?: Array<{open:string;close?:string;caseInsensitive?:boolean;indented?:boolean}>;
  indentedComments?: boolean;
};
type State = { close: string; open: string; depth: number; string: string; rawString: boolean; section: string; line: number; commentIndent:number };
const wordSet = (source = '', folded = false) => new Set(source.split(/\s+/).filter(Boolean).map(word => folded ? word.toLowerCase() : word));
const cKeywords = 'break case const continue default do else enum extern for goto if inline return static struct switch typedef union volatile while sizeof';
const cppKeywords = `${cKeywords} alignas alignof asm auto bool catch class concept constexpr consteval constinit decltype delete explicit export false friend mutable namespace new noexcept nullptr operator private protected public requires static_assert template this thread_local throw true try typename using virtual`;
const pythonKeywords = 'and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield';
const shaderTypes = 'void bool int uint float half double float2 float3 float4 float2x2 float3x3 float4x4 int2 int3 int4 uint2 uint3 uint4 half2 half3 half4 matrix vector sampler sampler2D sampler3D samplerCUBE Texture2D Texture3D TextureCube SamplerState SamplerComparisonState RWTexture2D StructuredBuffer RWStructuredBuffer cbuffer';
const shaderBuiltins = 'abs acos all any asin atan atan2 ceil clamp cos cross ddx ddy degrees determinant distance dot exp exp2 floor frac frexp fwidth isfinite isinf isnan ldexp length lerp log log2 max min mul normalize pow radians reflect refract round rsqrt saturate sign sin smoothstep sqrt step tan tex2D tex2Dlod texCUBE transpose trunc';
const glslTypes = 'void bool int uint float double vec2 vec3 vec4 ivec2 ivec3 ivec4 uvec2 uvec3 uvec4 bvec2 bvec3 bvec4 dvec2 dvec3 dvec4 mat2 mat3 mat4 mat2x3 mat2x4 mat3x2 mat3x4 mat4x2 mat4x3 sampler2D sampler3D samplerCube sampler2DArray sampler2DShadow image2D';
const glslBuiltins = `${shaderBuiltins} mix fract inversesqrt texture textureLod textureGrad textureSize texelFetch dFdx dFdy faceforward matrixCompMult lessThan greaterThan equal notEqual not`;
const cForms = { lineComments:['//'], blockComments:[['/*','*/']] as Array<[string,string]>, preprocessor:true };
const shaderRules: Rule[] = [{expression:/^(?:SV_[A-Za-z_]+|COLOR\d*|TEXCOORD\d*|POSITION\d*)\b/,style:'meta'}, {expression:/^(?:float|half|double|int|uint|bool)[1-4](?:x[1-4])?\b/,style:'typeName'}];
const luaKeywords = 'and break do else elseif end for function goto if in local not or repeat return then until while';
const configs: Record<string,Config> = {
  luau: { interpolation:/^\{[^}\n]*\}/, lineComments:['--'], luaLongStrings:true, strings:['"',"'",'`'], keywords:`${luaKeywords} continue type export typeof const`, types:'any nil boolean number string thread unknown never buffer', atoms:'true false nil', builtins:'game workspace script Instance Vector3 CFrame Color3 UDim2 Enum task require pairs ipairs print assert setmetatable', rules:[{expression:/^@[A-Za-z_]\w*/,style:'meta'}] },
  pico8: { lineComments:['--','//'], luaLongStrings:true, cartridge:true, keywords:luaKeywords, atoms:'true false nil', builtins:'_init _update _update60 _draw cls spr sspr map sfx music print btn btnp rect rectfill circ circfill line pset pget color pal add del all foreach count rnd flr mid sin cos atan2', preprocessor:true },
  angelscript: { ...cForms, keywords:`${cKeywords} class interface mixin namespace import from shared external final abstract override private protected public funcdef this super cast in out inout is notis`, types:'void bool int int8 int16 int32 int64 uint uint8 uint16 uint32 uint64 float double string array dictionary', atoms:'true false null', rules:[{expression:/^@/,style:'meta'}] },
  quakec: { ...cForms, keywords:'local void float vector string entity field if else while do return', builtins:'spawn remove precache_model precache_sound setmodel setorigin sound print bprint dprint', atoms:'world self other time frametime', rules:[{expression:/^\$[A-Za-z_]\w*/,style:'meta'}] },
  'source-qc': { ...cForms, keywords:'studio animation sequence body bodygroup blank loop fps snap rotate activity', rules:[{expression:/^\$[A-Za-z_]\w*/,style:'keyword'}] },
  acs: { ...cForms, keywords:`${cKeywords} script function world global net clientside open enter respawn death disconnect pickup unloading`, types:'int bool str void', atoms:'TRUE FALSE', builtins:'Print Delay Thing_Spawn SetActorProperty GetActorProperty ACS_Execute' },
  zscript: { ...cForms, keywords:`${cppKeywords} actor states default extends replaces version action let readonly override virtual native final clearscope play ui`, types:'int double bool String Name Vector2 Vector3 State Actor Color', atoms:'true false null', builtins:'Spawn A_Chase A_Look A_FaceTarget A_SpawnItemEx' },
  jsonc: { ...cForms, keywords:'', atoms:'true false null', strings:['"'], rules:[{expression:/^[{}[\],:]/,style:'punctuation'}] },
  snippets: { ...cForms, interpolation:/^\$(?:\d+|\{[^}\n]*\}|[A-Z_]+)/, atoms:'true false null', strings:['"'], rules:[{expression:/^\$(?:\d+|\{[^}]*\}|[A-Z_]+)/,style:'variableName'}] },
  json5: { ...cForms, atoms:'true false null Infinity NaN', rules:[{expression:/^[A-Za-z_$][\w$]*(?=\s*:)/,style:'propertyName'}] },
  jsonl: { atoms:'true false null', strings:['"'] },
  ini: { lineComments:[';','#'], rules:[{expression:/^\[[^\]]*\]/,style:'meta'},{expression:/^[^=:\s]+(?=\s*[=:])/,style:'propertyName'}], atoms:'true false yes no on off' },
  properties: { lineComments:['#','!',';'], rules:[{expression:/^\[[^\]]*\]/,style:'meta'},{expression:/^[^=:\s]+(?=\s*[=:])/,style:'propertyName'},{expression:/^\\(?:u[0-9a-fA-F]{4}|.)/,style:'escape'}] },
  dotenv: { lineComments:['#'], keywords:'export', rules:[{expression:/^[A-Za-z_][\w]*(?=\s*=)/,style:'propertyName'},{expression:/^\$\{[^}]*\}/,style:'variableName'}] },
  ignore: { lineComments:['#'], strings:[], rules:[{expression:/^!/,style:'operator'},{expression:/^(?:\*\*?|\?|\[[^\]]+\])/,style:'special'},{expression:/^\//,style:'punctuation'}] },
  bat: { caseInsensitive:true, keywords:'echo set setlocal endlocal call goto if else for in do not exist errorlevel defined pause exit pushd popd cd rem choice start copy del move mkdir rmdir type', lineComments:['::'], commentForms:[{open:'rem',caseInsensitive:true}], rules:[{expression:/^rem\b.*/i,style:'comment'},{expression:/^:[\w.-]+/,style:'labelName'},{expression:/^%(?:[^%\s]+%|[0-9*])|^![^!]+!/,style:'variableName'}] },
  makefile: { lineComments:['#'], keywords:'include -include sinclude define endef ifdef ifndef ifeq ifneq else endif export unexport override private vpath', rules:[{expression:/^\$\([^)]*\)|^\$\{[^}]*\}|^\$[@<^?*%+|]/,style:'variableName'},{expression:/^[\w./%+-]+(?=\s*:)/,style:'labelName'},{expression:/^[\w-]+(?=\s*(?:\?=|:=|\+=|=))/,style:'propertyName'}] },
  hlsl: { ...cForms, keywords:`${cKeywords} technique technique10 technique11 pass compile shader register packoffset in out inout uniform row_major column_major groupshared globallycoherent precise nointerpolation centroid linear sample noperspective discard`, types:shaderTypes, builtins:shaderBuiltins, atoms:'true false', rules:shaderRules },
  shaderlab: { ...cForms, keywords:`Shader Properties SubShader Pass Tags LOD Name UsePass GrabPass Fallback CustomEditor Cull ZWrite ZTest Blend BlendOp ColorMask Offset Stencil Ref Comp Fail ZFail HLSLPROGRAM ENDHLSL HLSLINCLUDE CGPROGRAM ENDCG CGINCLUDE ${cKeywords} uniform in out inout discard`, types:`${shaderTypes} Range Color Vector 2D 3D Cube`, builtins:shaderBuiltins, atoms:'On Off Always Never Equal LEqual GEqual Less Greater NotEqual Zero One SrcAlpha OneMinusSrcAlpha', rules:shaderRules },
  'unreal-shader': { ...cForms, keywords:`${cKeywords} in out inout uniform discard register`, types:shaderTypes, builtins:`${shaderBuiltins} GetMaterialParameters GetMaterialPixelParameters`, atoms:'true false', rules:shaderRules },
  glsl: { ...cForms, keywords:`${cKeywords} layout uniform attribute varying in out inout precision highp mediump lowp flat smooth noperspective invariant discard coherent volatile restrict readonly writeonly buffer shared`, types:glslTypes, builtins:glslBuiltins, atoms:'true false', rules:[{expression:/^gl_[\w]+/,style:'builtin'}] },
  gdshader: { ...cForms, keywords:`${cKeywords} shader_type render_mode uniform varying in out inout global instance group_uniforms discard`, types:glslTypes, builtins:`${glslBuiltins} vertex fragment light start process`, atoms:'true false spatial canvas_item particles sky fog', rules:[{expression:/^[A-Z][A-Z0-9_]+\b/,style:'builtin'},{expression:/^hint_[\w]+\b/,style:'meta'}] },
  wgsl: { ...cForms, nestedComments:true, keywords:'alias break case const const_assert continue continuing default diagnostic discard else enable false fn for if let loop override requires return struct switch true var while', types:'array atomic bool f16 f32 i32 u32 vec2 vec3 vec4 mat2x2 mat2x3 mat2x4 mat3x2 mat3x3 mat3x4 mat4x2 mat4x3 mat4x4 ptr sampler sampler_comparison texture_1d texture_2d texture_2d_array texture_3d texture_cube texture_storage_2d', builtins:'textureSample textureLoad textureStore textureDimensions workgroupBarrier storageBarrier dot cross normalize mix clamp', rules:[{expression:/^@[A-Za-z_][\w]*/,style:'meta'}] },
  gds: { lineComments:['#'], tripleStrings:true, keywords:'and as assert await break class class_name const continue elif else enum extends for func if in is match not or pass preload return self signal static super var when while yield', types:'bool int float String StringName NodePath Vector2 Vector2i Vector3 Vector3i Vector4 Vector4i Color Transform2D Transform3D Basis Quaternion AABB Rect2 Array Dictionary PackedByteArray', builtins:'load preload print range len get_node', atoms:'true false null', rules:[{expression:/^@[A-Za-z_][\w]*/,style:'meta'},{expression:/^(?:\$|%)[\w/]+|^[&^](?=["'])/,style:'variableName'}] },
  'godot-resource': { lineComments:[';','#'], rules:[{expression:/^\[[^\]]*\]/,style:'meta'},{expression:/^[\w./]+(?=\s*=)/,style:'propertyName'}], types:'Vector2 Vector2i Vector3 Vector3i Vector4 Vector4i Color Transform2D Transform3D Basis Quaternion Rect2 Rect2i AABB NodePath StringName PackedByteArray PackedInt32Array PackedFloat32Array PackedStringArray PackedVector2Array PackedVector3Array PackedColorArray', builtins:'ExtResource SubResource Resource', atoms:'true false null' },
  unrealscript: { ...cForms, caseInsensitive:true, keywords:'abstract auto break case class config const continue default defaultproperties delegate do else enum event exec extends final for foreach function global if ignores input interface iterator latent local native operator optional out reliable replication return simulated singular state static stop struct super switch transient travel unreliable var while within', types:'bool byte int float string name vector rotator object class array', atoms:'true false none' },
  sourcepawn: { ...cForms, keywords:'public stock static native forward new decl const enum struct methodmap property typedef typeset function if else while for do switch case default return break continue delete view_as sizeof', types:'int float bool char void Handle String Float any Action Plugin', atoms:'true false null INVALID_HANDLE', rules:[{expression:/^@[A-Za-z_][\w]*/,style:'meta'}] },
  keyvalues: { ...cForms, keywords:'', rules:[{expression:/^#(?:include|base)\b/,style:'meta'},{expression:/^\[\!?\$[^\]]+\]/,style:'meta'}] },
  kv3: { ...cForms, blockComments:[['<!--','-->'],['/*','*/']], multilineStrings:true, tripleStrings:true, atoms:'true false null', types:'resource resource_name panorama soundevent subclass', rules:[{expression:/^[\w.]+(?=\s*=)/,style:'propertyName'},{expression:/^(?:encoding|format):[\w:-]+/,style:'meta'}] },
  'source-config': { lineComments:['//'], keywords:'alias bind unbind unbindall exec echo wait toggle incrementvar bindtoggle', rules:[{expression:/^(?:sv|cl|mat|r|snd|net|mp|con|fps)_[\w]+/,style:'propertyName'}] },
  vmf: { ...cForms, keywords:'versioninfo visgroups viewsettings world entity solid side editor connections cameras cordon cordons vertices_plus dispinfo normals distances offsets offset_normals alphas triangle_tags allowed_verts' },
  fgd: { ...cForms, keywords:'input output integer float string choices flags target_destination target_source void color255 studio sprite sound', rules:[{expression:/^@[A-Za-z]+/,style:'meta'}] },
  papyrus: { lineComments:[';'], blockComments:[[';/','/;']], caseInsensitive:true, keywords:'Scriptname extends import auto autoreadonly betaonly debugonly const native global function endfunction event endevent state endstate property endproperty group endgroup struct endstruct if elseif else endif while endwhile return new as is self parent bool int float string var', atoms:'true false none', types:'Actor ObjectReference Form Quest Alias ReferenceAlias Location Weapon Armor' },
  'papyrus-flags': { lineComments:[';','//'], keywords:'flag Script Property Variable Function Struct', rules:[{expression:/^[A-Za-z_]\w*(?=\s+\d)/,style:'propertyName'}] },
  geck: { lineComments:[';'], caseInsensitive:true, keywords:'scriptname scn begin end if elseif else endif set to let return while loop foreach continue break function', types:'short long int float ref reference string_var array_var', builtins:'GameMode MenuMode OnActivate OnAdd OnDrop OnLoad OnDeath OnEquip OnUnequip GetSelf GetPlayer AddItem RemoveItem ShowMessage', atoms:'true false' },
  mgcb: { lineComments:['#'], strings:['"'], rules:[{expression:/^\/(?:outputDir|intermediateDir|platform|config|profile|compress|importer|processor|processorParam|build|copy|reference|rebuild|clean|quiet|incremental|launchDebugger)\b/i,style:'keyword'},{expression:/^#[\w ].*/,style:'comment'}] },
  gml: { ...cForms, keywords:'and or xor not var globalvar static enum function constructor if else for while do until repeat switch case default with break continue return exit try catch finally throw new delete begin end', types:'real int64 bool string array struct', builtins:'show_debug_message instance_create_layer instance_destroy draw_sprite keyboard_check random irandom ds_list_create array_create', atoms:'true false undefined noone all self other global', rules:[{expression:/^#(?:region|endregion|macro)\b/,style:'meta'},{expression:/^(?:obj|spr|snd|rm|scr)_[\w]+/,style:'builtin'}] },
  renpy: { interpolation:/^(?:\{\/?[\w=:#]+\}|\[[^\]\n]*\])/, lineComments:['#'], tripleStrings:true, keywords:`${pythonKeywords} label jump call screen show hide scene with menu image define default init python transform style translate voice play stop queue pause window character return`, builtins:'Character config gui renpy', atoms:'True False None', rules:[{expression:/^\{\/?[\w=:#]+\}/,style:'meta'},{expression:/^\$\s*/,style:'meta'}] },
  uss: { ...cForms, keywords:'none auto inherit initial unset', rules:[{expression:/^--?[\w-]+(?=\s*:)/,style:'propertyName'},{expression:/^\.[\w-]+|^#[\w-]+/,style:'className'},{expression:/^:[\w-]+/,style:'meta'},{expression:/^@[\w-]+/,style:'meta'},{expression:/^(?:px|em|rem|s|ms|deg|%)\b/,style:'unit'}] },
  obj: { lineComments:['#'], keywords:'v vt vn vp f l p o g s usemtl mtllib newmtl Ka Kd Ks Ke Ns Ni d Tr illum map_Kd map_Ks map_Bump bump' },
  'codex-rules': { lineComments:['#'], tripleStrings:true, keywords:'and as break continue def elif else for if in lambda load not or pass return', atoms:'True False None', builtins:'prefix_rule glob repo_rule allow forbidden prompt' },
  raku: { lineComments:['#'], blockComments:[['#`(',')']], keywords:'my our state has class role module package grammar method sub multi proto token rule regex enum subset constant use need require if elsif else unless given when default for loop while until repeat gather take do try CATCH CONTROL LEAVE ENTER return next last redo say print', types:'Int Num Rat Str Bool Array Hash List Map Set Pair Any Mu Nil', atoms:'True False Nil', rules:[{expression:/^[$@%&][\w:*!.-]+/,style:'variableName'},{expression:/^:[\w-]+/,style:'meta'}] },
  bibtex: { lineComments:['%'], rules:[{expression:/^@[A-Za-z]+/,style:'keyword'},{expression:/^[\w-]+(?=\s*=)/,style:'propertyName'}] },
  less: { ...cForms, keywords:'when and not all important', rules:[{expression:/^@[\w-]+/,style:'variableName'},{expression:/^\.[\w-]+|^#[\w-]+/,style:'className'},{expression:/^[\w-]+(?=\s*:)/,style:'propertyName'},{expression:/^#[0-9a-fA-F]{3,8}\b/,style:'color'}] },
  handlebars: { blockComments:[['{{!--','--}}'],['<!--','-->']], keywords:'if else unless each with lookup log', rules:[{expression:/^\{\{[#/>!]?|^\}\}/,style:'meta'},{expression:/^<\/?[A-Za-z][\w:.-]*/,style:'tagName'},{expression:/^@[\w]+/,style:'variableName'},{expression:/^[\w:-]+(?=\s*=)/,style:'attributeName'}] },
  razor: { ...cForms, blockComments:[['@*','*@'],['<!--','-->'],['/*','*/']], keywords:`${cppKeywords} model using inject namespace page section functions code inherits implements attribute await async foreach get set`, types:'string int bool double decimal dynamic var Task', rules:[{expression:/^@[A-Za-z_][\w.]*/,style:'meta'},{expression:/^<\/?[A-Za-z][\w:.-]*/,style:'tagName'},{expression:/^[\w:-]+(?=\s*=)/,style:'attributeName'}] },
  restructuredtext: { lineComments:[], indentedComments:true, commentForms:[{open:'..',indented:true}], rules:[{expression:/^\.\.\s+[\w-]+::/,style:'keyword'},{expression:/^:[\w-]+:/,style:'meta'},{expression:/^``[^`]*``|^`[^`]*`_?/,style:'string'},{expression:/^\*\*[^*]+\*\*|^\*[^*]+\*/,style:'emphasis'}], lineRule:text => /^\s*[=~^"'`:+#*_-]{3,}\s*$/.test(text) ? 'heading' : /^\s*\.\.(?:\s+(?![\w-]+::)|\s*$)/.test(text) ? 'comment' : null },
  'git-commit': { lineComments:['#'], lineRule:(text,line) => line === 0 && text ? 'heading' : null, rules:[{expression:/^\b(?:fixup|squash)!/,style:'keyword'},{expression:/^[0-9a-f]{7,40}\b/,style:'number'}] },
  'git-rebase': { lineComments:['#'], keywords:'pick p reword r edit e squash s fixup f exec x break b drop d label l reset t merge m update-ref u', rules:[{expression:/^[0-9a-f]{7,40}\b/,style:'number'}] },
  log: { caseInsensitive:true, keywords:'TRACE DEBUG INFO NOTICE WARNING WARN ERROR FATAL CRITICAL', rules:[{expression:/^\d{4}-\d\d-\d\d(?:[T ][\d:.+-Z]+)?/,style:'meta'},{expression:/^\[[^\]]*\]/,style:'meta'}] },
  'search-result': { rules:[{expression:/^\d+(?::\d+)?:/,style:'number'},{expression:/^(?:[^\s:]+\/)+[^:]+:/,style:'link'}], lineRule:text => /^#\s/.test(text) ? 'heading' : null },
  mermaid: { lineComments:['%%'], keywords:'flowchart graph sequenceDiagram classDiagram stateDiagram stateDiagram-v2 erDiagram gantt pie mindmap timeline gitGraph journey quadrantChart xychart-beta block-beta subgraph end participant actor title section class state Note direction todayMarker dateFormat axisFormat accTitle accDescr', rules:[{expression:/^(?:-->|-.->|==>|-->>|->>|--|==|\+\+)/,style:'operator'}] },
};

function lexicalParser(id: string, config: Config): StreamParser<State> {
  const folded = config.caseInsensitive === true;
  const keywords = wordSet(config.keywords,folded), types = wordSet(config.types,folded), builtins = wordSet(config.builtins,folded), atoms = wordSet(config.atoms,folded);
  const comments = [...(config.lineComments || [])].sort((a,b) => b.length - a.length);
  const blocks = [...(config.blockComments || [])].sort((a,b) => b[0].length - a[0].length);
  const quotes = config.strings || ['"',"'"];
  function block(stream: StringStream, state: State) {
    while (!stream.eol()) {
      if (config.nestedComments && stream.match(state.open)) state.depth++;
      else if (stream.match(state.close)) {
        if (--state.depth <= 0) { state.close = ''; state.open = ''; break; }
      } else stream.next();
    }
    return 'comment';
  }
  function string(stream: StringStream, state: State) {
    while (!stream.eol()) {
      if (config.interpolation && stream.match(config.interpolation,false)) {
        if (stream.pos > stream.start) break;
        stream.match(config.interpolation);return 'variableName';
      }
      if (stream.match(state.string)) {
        if (config.doubledQuotes && state.string.length === 1 && stream.match(state.string)) continue;
        state.string = ''; break;
      }
      if (stream.next() === '\\' && !state.rawString) stream.next();
    }
    if (state.string.length === 1 && state.string !== '`' && !config.multilineStrings && stream.eol()) state.string = '';
    return 'string';
  }
  return {
    name:`openclank-${id}`,
    startState:() => ({close:'',open:'',depth:0,string:'',rawString:false,section:'header',line:-1,commentIndent:-1}),
    copyState:state => ({...state}),
    token(stream, state) {
      if (stream.sol()) {
        state.line++;
        if (config.cartridge && /^__(?:lua|gfx|gff|map|sfx|music|label)__$/.test(stream.string)) {
          state.section=stream.string;state.close='';state.string='';stream.skipToEnd();return 'meta';
        }
      }
      if (config.cartridge && state.section !== '__lua__') { stream.skipToEnd();return state.section === 'header' ? 'meta' : 'number'; }
      if (state.commentIndent>=0) {
        if (!stream.string.trim() || stream.indentation()>state.commentIndent) {stream.skipToEnd();return 'comment';}
        state.commentIndent=-1;
      }
      if (state.close) return block(stream,state);
      if (state.string) return string(stream,state);
      if (stream.eatSpace()) return null;
      if (stream.sol() || stream.pos === stream.indentation()) {
        const style = config.lineRule?.(stream.string,state.line);
        if (style) { if(style==='comment'&&config.indentedComments)state.commentIndent=stream.indentation();stream.skipToEnd(); return style; }
      }
      if (config.luaLongStrings) {
        const comment = stream.match(/^--\[(=*)\[/);
        if (comment) { state.open=comment[0];state.close=']'+comment[1]+']';state.depth=1;return block(stream,state); }
        const literal = stream.match(/^\[(=*)\[/);
        if (literal) { state.string=']'+literal[1]+']';state.rawString=true;return string(stream,state); }
      }
      for (const [open,close] of blocks) if (stream.match(open)) { state.open=open;state.close=close;state.depth=1;return block(stream,state); }
      for (const open of comments) if (stream.match(open)) { stream.skipToEnd();return 'comment'; }
      for (const rule of config.rules || []) if (stream.match(rule.expression)) return rule.style;
      if (config.tripleStrings) for (const quote of ['"""',"'''"]) if (stream.match(quote)) { state.string=quote;state.rawString=false;return string(stream,state); }
      for (const quote of quotes) if (stream.match(quote)) { state.string=quote;state.rawString=false;return string(stream,state); }
      if (config.preprocessor && stream.match(/^#\s*[A-Za-z_]\w*/)) return 'meta';
      if (stream.match(/^(?:0[xX][\da-fA-F_]+|0[bB][01_]+|(?:\d[\d_]*(?:\.[\d_]*)?|\.\d[\d_]*)(?:[eE][+-]?[\d_]+)?)(?:[uUlLfFhH]+)?/)) return 'number';
      const identifier = stream.match(config.identifier || /^[A-Za-z_$][\w$]*/);
      if (identifier) {
        const word = folded ? stream.current().toLowerCase() : stream.current();
        if (keywords.has(word)) return 'keyword';
        if (types.has(word)) return 'typeName';
        if (builtins.has(word)) return 'builtin';
        if (atoms.has(word)) return 'atom';
        if (/^\s*(?:=|:)/.test(stream.string.slice(stream.pos))) return 'propertyName';
        if (/^\s*\(/.test(stream.string.slice(stream.pos))) return 'variableName';
        return null;
      }
      if (stream.match(/^[{}[\]();,.]/)) return 'punctuation';
      if (stream.match(/^[+*/%=!<>|&^~?:-]+/)) return 'operator';
      stream.next();return null;
    },
    languageData: {
      commentTokens: { line:comments[0], block:blocks[0] ? {open:blocks[0][0],close:blocks[0][1]} : undefined },
      // Reflect the tokenizer's complete syntax, not another per-language allowlist.
      richCommentForms: [
        ...comments.map(open => ({open})),
        ...blocks.map(([open,close]) => ({open,close,nested:config.nestedComments === true})),
        ...(config.commentForms || []),
        ...(config.luaLongStrings ? [{open:'--[[',close:']]',dynamic:'lua-long'}] : []),
      ],
      richCommentCapability: comments.length || blocks.length || config.luaLongStrings || config.commentForms?.length || config.rules?.some(rule => rule.style === 'comment') ? 'tokenizer' : 'none',
    },
  };
}
type NarrativeState = { close:string; quote:string; expression:string; depth:number; section:string; link:boolean };
const narrativeKeywords = wordSet('VAR CONST LIST INCLUDE EXTERNAL TODO END DONE temp function return if else elseif endif set declare jump detour return wait stop once endonce and or not is true false null');
/** Prose remains prose; only narrative structure and expression regions are syntax. */
function narrativeParser(id: string): StreamParser<NarrativeState> {
  const ink = id === 'ink', yarn = id === 'yarn', twee = id === 'twee';
  return {
    name:`openclank-${id}`,
    startState:() => ({close:'',quote:'',expression:'',depth:0,section:yarn ? 'header' : 'body',link:false}),
    copyState:state => ({...state}),
    token(stream,state) {
      if (stream.sol() && state.expression === 'line') { state.expression='';state.quote=''; }
      if (state.close) {
        while (!stream.eol()) if (stream.match(state.close)) { state.close='';break; } else stream.next();
        return 'comment';
      }
      if (state.quote) {
        while (!stream.eol()) {
          if (stream.match(state.quote)) { state.quote='';break; }
          if (stream.next() === '\\') stream.next();
        }
        return 'string';
      }
      if (stream.sol()) {
        if (twee && stream.match(/^::[^\n]*/)) { state.expression='';state.depth=0;state.link=false;return 'heading'; }
        if (ink && stream.match(/^\s*={1,}[^\n]*/)) return 'heading';
        if (yarn && stream.match(/^\s*(?:---|===)\s*$/)) {
          state.section=stream.current().trim() === '---' ? 'body' : 'header';state.expression='';state.depth=0;return 'meta';
        }
      }
      if (stream.eatSpace()) return null;
      if (ink && stream.match('/*')) { state.close='*/';return 'comment'; }
      if (twee && stream.match('<!--')) { state.close='-->';return 'comment'; }
      if (!twee && stream.match('//')) { stream.skipToEnd();return 'comment'; }
      if (state.link) {
        if (stream.match(']]')) { state.link=false;return 'link'; }
        while (!stream.eol() && !stream.match(']]',false)) stream.next();
        return 'link';
      }
      if (state.expression) {
        if (state.expression !== 'line' && stream.match(state.expression)) {
          if (--state.depth <= 0) state.expression='';
          return 'punctuation';
        }
        if ((state.expression === '}' && stream.match('{')) || (state.expression === ')' && stream.match('('))) { state.depth++;return 'punctuation'; }
        if (stream.match('"')) { state.quote='"';return 'string'; }
        if (stream.match(/^[$_][A-Za-z_][\w.]*/)) return 'variableName';
        if (stream.match(/^(?:\d+(?:\.\d+)?|\.\d+)/)) return 'number';
        if (stream.match(/^[A-Za-z_][\w.]*/)) return narrativeKeywords.has(stream.current()) ? 'keyword' : 'variableName';
        if (stream.match(/^[+*/%=!<>|&^~?:-]+/)) return 'operator';
        stream.next();return 'punctuation';
      }
      if (yarn && state.section === 'header' && stream.match(/^[A-Za-z_][\w-]*(?=\s*:)/)) return 'propertyName';
      if ((yarn || twee) && stream.match('<<')) { state.expression='>>';state.depth=1;return 'meta'; }
      if (twee && stream.match('[[')) { state.link=true;return 'link'; }
      if (twee && stream.match(/^\([A-Za-z][\w-]*:/)) { state.expression=')';state.depth=1;return 'meta'; }
      if (!twee && stream.match('{')) { state.expression='}';state.depth=1;return 'punctuation'; }
      if (ink && stream.match('~')) { state.expression='line';return 'meta'; }
      if (ink && stream.match(/^(?:VAR|CONST|LIST|INCLUDE|EXTERNAL|TODO)\b/)) { state.expression='line';return 'keyword'; }
      if (ink && stream.match(/^(?:->->|->|<-|<>|[+*]+|-)(?=\s|[A-Za-z_(]|$)/)) return 'operator';
      if (yarn && stream.match('->')) return 'operator';
      if (!twee && stream.match(/#[^\n]*/)) return 'meta';
      if (twee && stream.match(/^<\/?[A-Za-z][^>]*>/)) return 'tagName';
      if (stream.match(/^\\./)) return 'escape';
      stream.next();return null;
    },
    languageData:{commentTokens:ink ? {line:'//',block:{open:'/*',close:'*/'}} : yarn ? {line:'//'} : {block:{open:'<!--',close:'-->'}}, richCommentCapability:'tokenizer'},
  };
}
const narrativeLoaded = new Map<string, StreamLanguage<NarrativeState>>();
const loaded = new Map<string, StreamLanguage<State>>();
export function lexicalLanguage(id: string) {
  if (['ink','yarn','twee'].includes(id)) {
    let narrative = narrativeLoaded.get(id);
    if (!narrative) { narrative=StreamLanguage.define(narrativeParser(id));narrativeLoaded.set(id,narrative); }
    return narrative;
  }
  let language = loaded.get(id);
  if (!language) {
    const config = configs[id];
    if (!config) throw new Error(`No lexical adapter for ${id}`);
    language = StreamLanguage.define(lexicalParser(id,config));loaded.set(id,language);
  }
  return language;
}

/** Markdown-derived agent/prompt documents gain frontmatter and template/math tokens. */
export function sourceDialectParser(id: string): MarkdownConfig {
  return {
    defineNodes:[{name:'SourceFrontmatter',style:tags.meta},{name:'SourceTemplate',style:tags.variableName},{name:'SourceMath',style:tags.processingInstruction}],
    parseBlock:[{
      name:'SourceFrontmatter',before:'HorizontalRule',
      parse(cx,line) {
        if (cx.lineStart !== 0 || !/^---\s*$/.test(line.text)) return false;
        const from = cx.lineStart;let to = from + line.text.length, count = 0;
        while (cx.nextLine()) {
          to = cx.lineStart + line.text.length;
          if (/^(?:---|\.\.\.)\s*$/.test(line.text) || ++count >= 256) { cx.nextLine();break; }
        }
        cx.addElement(cx.elt('SourceFrontmatter',from,to));return true;
      },
    }],
    parseInline:[{
      name:'SourceTemplate',before:'Escape',
      parse(cx,next,pos) {
        const remaining = cx.slice(pos,cx.end);
        const template = /^\{\{[^\n}]*\}\}/.exec(remaining);
        if (template) return cx.addElement(cx.elt('SourceTemplate',pos,pos+template[0].length));
        if (['juliamarkdown','markdown-math','markdown_latex_combined','cpp_embedded_latex'].includes(id) && next === 36) {
          const math = /^\$\$?[^\n$]+\$\$?/.exec(remaining);
          if (math) return cx.addElement(cx.elt('SourceMath',pos,pos+math[0].length));
        }
        return -1;
      },
    }],
  };
}
