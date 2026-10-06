import { StreamLanguage } from '@codemirror/language';
import type { Extension } from '@codemirror/state';
import { EditorState } from '@codemirror/state';
import { resolveLanguage, LANGUAGE_REGISTRY, ADVERTISED_LANGUAGE_LABELS } from '../../../static/js/editor/languageRegistry.js';

export { LANGUAGE_REGISTRY, ADVERTISED_LANGUAGE_LABELS };
export type SourceLanguageChoice = string | { id:string };
export interface SourceLanguageOptions { path?: string; dialect?: string; firstLine?: string; content?: string }
export function sourceLanguageMetadata(language?: SourceLanguageChoice, options: SourceLanguageOptions = {}) {
  return resolveLanguage(typeof language === 'string' ? language : language?.id, options);
}
const loads = new Map<string, Promise<Extension>>();
export interface SourceCommentForm { open:string; close?:string; nested?:boolean; dynamic?:string; caseInsensitive?:boolean; indented?:boolean; lineDelimited?:boolean }
export interface SourceCommentCapability {
  id:string; name:string; strategy:'grammar' | 'tokenizer' | 'none' | 'unresolved';
  forms:SourceCommentForm[]; docstrings:boolean;
}
const commentCapabilities = new Map<string, SourceCommentCapability>();

/** Complete bracket syntax in the two existing stream modes that omit it.
 * Other tokens retain their established tokenizer and indentation behavior.
 */
function bracketAwareMode(parser:string, mode:any) {
  if(parser!=='cmake'&&parser!=='wast')return mode;
  return {...mode,
    startState:(unit:number)=>({...mode.startState(unit),clankClose:'',clankKind:'',clankDepth:0}),
    copyState:(state:any)=>({...mode.copyState?.(state)||state}),
    token(stream:any,state:any){
      if(!state.clankClose) {
        if(parser==='cmake'&&!state.continueString) {
          const match=stream.match(/^(#?)\[(=*)\[/);
          if(match){state.clankClose=`]${match[2]}]`;state.clankKind=match[1]?'comment':'string';state.clankDepth=1;}
        } else if(parser==='wast'&&state.state==='start'&&stream.match('(;')) {
          state.clankClose=';)';state.clankKind='comment';state.clankDepth=1;
        }
      }
      if(state.clankClose) {
        while(!stream.eol()) {
          if(parser==='wast'&&stream.match('(;'))state.clankDepth++;
          else if(stream.match(state.clankClose)){if(--state.clankDepth===0){state.clankClose='';break;}}
          else stream.next();
        }
        return state.clankKind;
      }
      return mode.token(stream,state);
    },
  };
}

/** Metadata compatibility for syntax modes whose upstream comment command data is incomplete.
 * These are delimiter declarations on the existing tokenizer, never a second scanner.
 */
function legacyCommentData(parser:string, mode:any) {
  const data = {...mode.languageData};
  const tokens = data.commentTokens || {};
  let forms:SourceCommentForm[] = [];
  if (tokens.line) forms.push({open:tokens.line});
  if (tokens.block) forms.push(tokens.block);
  if (parser === 'cmake') forms = [{open:'#'},{open:'#[[',close:']]',dynamic:'cmake-bracket'}];
  if (parser === 'wast') forms = [{open:';;'},{open:'(;',close:';)',nested:true}];
  if (parser === 'pug') forms = [{open:'//-'},{open:'//'}];
  if (parser === 'ruby') forms.push({open:'=begin',close:'=end',lineDelimited:true});
  if (parser === 'coffeescript') forms.unshift({open:'###',close:'###'});
  if (parser === 'lua') forms = [{open:'--[[',close:']]',dynamic:'lua-long'},{open:'--'}];
  if (parser === 'clojure') forms = [{open:';'}];
  if (parser === 'vb') forms.push({open:'REM ',caseInsensitive:true});
  if (parser === 'perl') data.richCommentDataMarkers=['__END__','__DATA__'];
  if (forms.length) {
    data.richCommentForms = forms;
    data.commentTokens = {line:forms.find(form=>!form.close)?.open,block:forms.find(form=>form.close)};
  }
  if (parser === 'diff') data.richCommentCapability = 'none';
  return data;
}

/** Reflect loaded syntax capability; pending/unavailable modes remain unresolved. */
export function sourceCommentCapability(language?:SourceLanguageChoice, options:SourceLanguageOptions = {}):SourceCommentCapability {
  const entry = sourceLanguageMetadata(language,options);
  return commentCapabilities.get(entry.id) || {
    id:entry.id,name:entry.modeName || entry.displayName,
    strategy:entry.parser === 'plain' || entry.id === 'json' ? 'none' : 'unresolved',
    forms:[],docstrings:entry.id === 'python',
  };
}

export async function prepareSourceCommentCapability(language?:SourceLanguageChoice, options:SourceLanguageOptions = {}) {
  const entry = sourceLanguageMetadata(language,options);
  await loadSourceLanguage(entry.id,options);
  return sourceCommentCapability(entry.id,options);
}

// Literal imports keep every dependency auditable and let Bun emit independent lazy chunks.
const legacy: Record<string, () => Promise<any>> = {
  css: () => import('@codemirror/legacy-modes/mode/css'),
  sass: () => import('@codemirror/legacy-modes/mode/sass'),
  haxe: () => import('@codemirror/legacy-modes/mode/haxe'),
  clike: () => import('@codemirror/legacy-modes/mode/clike'),
  clojure: () => import('@codemirror/legacy-modes/mode/clojure'),
  cmake: () => import('@codemirror/legacy-modes/mode/cmake'),
  coffeescript: () => import('@codemirror/legacy-modes/mode/coffeescript'),
  diff: () => import('@codemirror/legacy-modes/mode/diff'),
  dockerfile: () => import('@codemirror/legacy-modes/mode/dockerfile'),
  groovy: () => import('@codemirror/legacy-modes/mode/groovy'),
  julia: () => import('@codemirror/legacy-modes/mode/julia'),
  lua: () => import('@codemirror/legacy-modes/mode/lua'),
  mllike: () => import('@codemirror/legacy-modes/mode/mllike'),
  perl: () => import('@codemirror/legacy-modes/mode/perl'),
  powershell: () => import('@codemirror/legacy-modes/mode/powershell'),
  pug: () => import('@codemirror/legacy-modes/mode/pug'),
  r: () => import('@codemirror/legacy-modes/mode/r'),
  ruby: () => import('@codemirror/legacy-modes/mode/ruby'),
  shell: () => import('@codemirror/legacy-modes/mode/shell'),
  stex: () => import('@codemirror/legacy-modes/mode/stex'),
  swift: () => import('@codemirror/legacy-modes/mode/swift'),
  toml: () => import('@codemirror/legacy-modes/mode/toml'),
  vb: () => import('@codemirror/legacy-modes/mode/vb'),
  wast: () => import('@codemirror/legacy-modes/mode/wast'),
};
async function load(entry: ReturnType<typeof sourceLanguageMetadata>): Promise<Extension> {
  const [kind, parser, exportName] = entry.parser.split(':');
  if (kind === 'plain') return [];
  if (kind === 'legacy') {
    const module = await legacy[parser]?.();
    if (!module?.[exportName]) throw new Error(`Syntax loader unavailable for ${entry.id}`);
    return StreamLanguage.define({...bracketAwareMode(parser,module[exportName]),languageData:legacyCommentData(parser,module[exportName])});
  }
  if (kind === 'lexical') {
    const { lexicalLanguage } = await import('./openclank-lexical-languages');
    return lexicalLanguage(parser);
  }
  if (kind === 'markdown') {
    const { markdown } = await import('@codemirror/lang-markdown');
    // Markdown specializations receive real frontmatter/fence syntax. Unknown fenced
    // languages remain Markdown code text; services/engine execution are not provided.
    const { sourceDialectParser } = await import('./openclank-lexical-languages');
    return markdown({ extensions:[sourceDialectParser(entry.id)] });
  }
  switch (parser) {
    case 'javascript': {
      const { javascript } = await import('@codemirror/lang-javascript');
      return javascript({ jsx:entry.id.endsWith('react'), typescript:entry.id.startsWith('typescript') });
    }
    case 'cpp': return (await import('@codemirror/lang-cpp')).cpp();
    case 'css': return (await import('@codemirror/lang-css')).css();
    case 'go': return (await import('@codemirror/lang-go')).go();
    case 'html': return (await import('@codemirror/lang-html')).html();
    case 'java': return (await import('@codemirror/lang-java')).java();
    case 'json': return (await import('@codemirror/lang-json')).json();
    case 'markdown': return (await import('@codemirror/lang-markdown')).markdown();
    case 'php': return (await import('@codemirror/lang-php')).php();
    case 'python': return (await import('@codemirror/lang-python')).python();
    case 'rust': return (await import('@codemirror/lang-rust')).rust();
    case 'sql': return (await import('@codemirror/lang-sql')).sql();
    case 'xml': return (await import('@codemirror/lang-xml')).xml();
    case 'yaml': return (await import('@codemirror/lang-yaml')).yaml();
    default: throw new Error(`Syntax loader unavailable for ${entry.id}`);
  }
}
/** Cached grammar preparation; failures are evicted so a later asset retry can succeed. */
export function loadSourceLanguage(language?: SourceLanguageChoice, options: SourceLanguageOptions = {}): Promise<Extension> {
  const entry = sourceLanguageMetadata(language, options);
  let pending = loads.get(entry.id);
  if (!pending) {
    pending = load(entry).then(extension => {
      const state = EditorState.create({extensions:[extension]});
      const custom = state.languageDataAt<SourceCommentForm[]>('richCommentForms',0).flat();
      const standard = state.languageDataAt<any>('commentTokens',0).flatMap(tokens => [
        ...(tokens.line ? [{open:tokens.line}] : []),...(tokens.block ? [tokens.block] : []),
      ]);
      const forms = [...custom,...standard].filter((form,index,all)=>form?.open && all.findIndex(other=>other.open===form.open&&other.close===form.close)===index);
      const declared = state.languageDataAt<string>('richCommentCapability',0)[0];
      commentCapabilities.set(entry.id,{
        id:entry.id,name:entry.modeName || entry.displayName,forms,docstrings:entry.id==='python',
        strategy:entry.parser==='plain' || entry.id==='json' || declared==='none' ? 'none'
          : forms.length || entry.id==='python' ? (entry.parser.startsWith('lezer:') || entry.parser.startsWith('markdown:') ? 'grammar' : 'tokenizer') : 'unresolved',
      });
      return extension;
    });
    loads.set(entry.id, pending);
    pending.catch(() => { if (loads.get(entry.id) === pending) loads.delete(entry.id); });
  }
  return pending;
}
