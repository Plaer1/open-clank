// First-party source association registry. Syntax metadata is independent of semantic services.
// Installed-local baseline/provenance and deliberate binary exclusions: see languages.md audit.
// No runtime access to VS Code, extension directories, private settings, or reference material.
const definitions = [
  {"id":"bat","displayName":"Batch","aliases":["Batch","bat"],"extensions":[".bat",".cmd"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:bat","supportLevel":"lexical"},
  {"id":"bibtex","displayName":"BibTeX","aliases":["BibTeX","bibtex"],"extensions":[".bib"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:bibtex","supportLevel":"lexical"},
  {"id":"c","displayName":"C","aliases":["C","c"],"extensions":[".c",".i"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lezer:cpp","supportLevel":"grammar"},
  {"id":"chatagent","displayName":"Agent","aliases":["Agent","chat agent"],"extensions":[".agent.md",".chatmode.md"],"filenames":[],"patterns":["**/.github/agents/*.md","**/.claude/agents/*.md"],"firstLines":[],"parser":"markdown:chatagent","supportLevel":"grammar-dialect"},
  {"id":"clojure","displayName":"Clojure","aliases":["Clojure","clojure"],"extensions":[".clj",".cljs",".cljc",".cljx",".clojure",".edn"],"filenames":[],"patterns":[],"firstLines":[],"parser":"legacy:clojure:clojure","supportLevel":"stream"},
  {"id":"codex-rules","displayName":"Codex Rules","aliases":["Codex Rules"],"extensions":[".rules"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:codex-rules","supportLevel":"lexical"},
  {"id":"coffeescript","displayName":"CoffeeScript","aliases":["CoffeeScript","coffeescript","coffee"],"extensions":[".coffee",".cson",".iced"],"filenames":[],"patterns":[],"firstLines":[],"parser":"legacy:coffeescript:coffeeScript","supportLevel":"stream"},
  {"id":"cpp","displayName":"C++","aliases":["C++","Cpp","cpp","C/C++ header","C++ header","C header","cxx","cc","hpp","hh","hxx"],"extensions":[".cpp",".cppm",".cc",".ccm",".cxx",".cxxm",".c++",".c++m",".hpp",".hh",".hxx",".h++",".h",".ii",".ino",".inl",".ipp",".ixx",".mpp",".mxx",".tpp",".txx",".hpp.in",".h.in"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lezer:cpp","supportLevel":"grammar"},
  {"id":"cpp_embedded_latex","displayName":"C++ embedded LaTeX","aliases":[],"extensions":[],"filenames":[],"patterns":[],"firstLines":[],"parser":"legacy:stex:stex","supportLevel":"stream","helper":true,"selectable":false},
  {"id":"csharp","displayName":"C#","aliases":["C#","csharp","cs"],"extensions":[".cs",".csx",".cake"],"filenames":[],"patterns":[],"firstLines":[],"parser":"legacy:clike:csharp","supportLevel":"stream"},
  {"id":"css","displayName":"CSS","aliases":["CSS","css"],"extensions":[".css"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lezer:css","supportLevel":"grammar"},
  {"id":"cuda-cpp","displayName":"CUDA C++","aliases":["CUDA C++"],"extensions":[".cu",".cuh"],"filenames":[],"patterns":[],"firstLines":[],"parser":"legacy:clike:cpp","supportLevel":"stream"},
  {"id":"dart","displayName":"Dart","aliases":["Dart"],"extensions":[".dart"],"filenames":[],"patterns":[],"firstLines":[],"parser":"legacy:clike:dart","supportLevel":"stream"},
  {"id":"diff","displayName":"Diff","aliases":["Diff","diff"],"extensions":[".diff",".patch",".rej"],"filenames":[],"patterns":[],"firstLines":[],"parser":"legacy:diff:diff","supportLevel":"stream"},
  {"id":"dockercompose","displayName":"Compose","aliases":["Compose","compose"],"extensions":[],"filenames":[],"patterns":["compose.yml","compose.yaml","compose.*.yml","compose.*.yaml","*docker*compose*.yml","*docker*compose*.yaml"],"firstLines":[],"parser":"lezer:yaml","supportLevel":"grammar"},
  {"id":"dockerfile","displayName":"Dockerfile","aliases":["Docker","Dockerfile","Containerfile"],"extensions":[".dockerfile",".containerfile"],"filenames":["Dockerfile","Containerfile"],"patterns":["Dockerfile.*","Containerfile.*"],"firstLines":[],"parser":"legacy:dockerfile:dockerFile","supportLevel":"stream"},
  {"id":"dotenv","displayName":"Dotenv","aliases":["Dotenv"],"extensions":[".env"],"filenames":[".env",".flaskenv","user-dirs.dirs"],"patterns":[".env.*"],"firstLines":[],"parser":"lexical:dotenv","supportLevel":"lexical"},
  {"id":"fsharp","displayName":"F#","aliases":["F#","FSharp","fsharp"],"extensions":[".fs",".fsi",".fsx",".fsscript"],"filenames":[],"patterns":[],"firstLines":[],"parser":"legacy:mllike:fSharp","supportLevel":"stream"},
  {"id":"git-commit","displayName":"Git Commit Message","aliases":["Git Commit Message","git-commit"],"extensions":[],"filenames":["COMMIT_EDITMSG","MERGE_MSG"],"patterns":[],"firstLines":[],"parser":"lexical:git-commit","supportLevel":"lexical"},
  {"id":"git-rebase","displayName":"Git Rebase Message","aliases":["Git Rebase Message","git-rebase"],"extensions":[],"filenames":["git-rebase-todo"],"patterns":["**/rebase-merge/done"],"firstLines":[],"parser":"lexical:git-rebase","supportLevel":"lexical"},
  {"id":"go","displayName":"Go","aliases":["Go","golang"],"extensions":[".go"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lezer:go","supportLevel":"grammar"},
  {"id":"groovy","displayName":"Groovy","aliases":["Groovy","groovy"],"extensions":[".groovy",".gvy",".gradle",".jenkinsfile",".nf"],"filenames":["Jenkinsfile"],"patterns":["Jenkinsfile*"],"firstLines":["^#!.*\\bgroovy\\b"],"parser":"legacy:groovy:groovy","supportLevel":"stream"},
  {"id":"handlebars","displayName":"Handlebars","aliases":["Handlebars","handlebars"],"extensions":[".handlebars",".hbs",".hjs"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:handlebars","supportLevel":"lexical"},
  {"id":"hlsl","displayName":"HLSL","aliases":["HLSL","hlsl"],"extensions":[".hlsl",".hlsli",".fx",".fxh",".vsh",".psh",".cginc",".compute",".fxc"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:hlsl","supportLevel":"lexical"},
  {"id":"html","displayName":"HTML","aliases":["HTML","htm","html","xhtml","htm"],"extensions":[".html",".htm",".shtml",".xhtml",".xht",".mdoc",".jsp",".asp",".aspx",".jshtm",".volt",".ejs",".rhtml"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lezer:html","supportLevel":"grammar"},
  {"id":"ignore","displayName":"Ignore","aliases":["Ignore","ignore"],"extensions":[".gitignore_global",".gitignore",".git-blame-ignore-revs",".npmignore"],"filenames":[".vscodeignore",".dockerignore",".prettierignore"],"patterns":[".copilotignore"],"firstLines":[],"parser":"lexical:ignore","supportLevel":"lexical"},
  {"id":"ini","displayName":"Ini","aliases":["Ini","ini"],"extensions":[".ini"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:ini","supportLevel":"lexical"},
  {"id":"instructions","displayName":"Instructions","aliases":["Instructions","instructions"],"extensions":[".instructions.md","copilot-instructions.md"],"filenames":[],"patterns":["**/.claude/rules/**/*.md"],"firstLines":[],"parser":"markdown:instructions","supportLevel":"grammar-dialect"},
  {"id":"jade","displayName":"Pug","aliases":["Pug","Jade","jade"],"extensions":[".pug",".jade"],"filenames":[],"patterns":[],"firstLines":[],"parser":"legacy:pug:pug","supportLevel":"stream"},
  {"id":"java","displayName":"Java","aliases":["Java","java"],"extensions":[".java",".jav"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lezer:java","supportLevel":"grammar"},
  {"id":"javascript","displayName":"JavaScript","aliases":["JavaScript","javascript","js","node","mjs","cjs"],"extensions":[".js",".es6",".mjs",".cjs",".pac"],"filenames":["jakefile"],"patterns":[],"firstLines":["^#!.*\\bnode"],"parser":"lezer:javascript","supportLevel":"grammar"},
  {"id":"javascriptreact","displayName":"JavaScript JSX","aliases":["JavaScript JSX","JavaScript React","jsx"],"extensions":[".jsx"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lezer:javascript","supportLevel":"grammar"},
  {"id":"json","displayName":"JSON","aliases":["JSON","json"],"extensions":[".code-profile",".json",".bowerrc",".jscsrc",".webmanifest",".js.map",".css.map",".ts.map",".har",".jslintrc",".jsonld",".geojson",".ipynb",".vuerc",".tsbuildinfo",".uproject",".uplugin",".asmdef",".asmref",".yy",".yyp"],"filenames":["composer.lock",".watchmanconfig"],"patterns":[],"firstLines":[],"parser":"lezer:json","supportLevel":"grammar"},
  {"id":"jsonc","displayName":"JSON","aliases":["JSON with Comments","jsonc"],"extensions":[".code-workspace","language-configuration.json","icon-theme.json","color-theme.json",".jsonc",".eslintrc",".eslintrc.json",".jsfmtrc",".jshintrc",".swcrc",".hintrc",".babelrc",".toolset.jsonc"],"filenames":["settings.json","launch.json","tasks.json","mcp.json","keybindings.json","extensions.json","argv.json","profiles.json","devcontainer.json",".devcontainer.json","babel.config.json","bun.lock",".babelrc.json",".ember-cli","typedoc.json","tsconfig.json","jsconfig.json",".prettierrc"],"patterns":["**/.github/hooks/*.json","tsconfig.*.json","jsconfig.*.json","tsconfig-*.json","jsconfig-*.json"],"firstLines":[],"parser":"lexical:jsonc","supportLevel":"lexical","modeName":"JSON with Comments"},
  {"id":"jsonl","displayName":"JSON Lines","aliases":["JSON Lines"],"extensions":[".jsonl",".ndjson"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:jsonl","supportLevel":"lexical"},
  {"id":"jsx-tags","displayName":"JSX tags","aliases":[],"extensions":[],"filenames":[],"patterns":[],"firstLines":[],"parser":"lezer:html","supportLevel":"grammar","helper":true,"selectable":false},
  {"id":"julia","displayName":"Julia","aliases":["Julia","julia"],"extensions":[".jl"],"filenames":[],"patterns":[],"firstLines":["^#!\\s*/.*\\bjulia[0-9.-]*\\b"],"parser":"legacy:julia:julia","supportLevel":"stream"},
  {"id":"juliamarkdown","displayName":"Julia Markdown","aliases":["Julia Markdown","juliamarkdown"],"extensions":[".jmd"],"filenames":[],"patterns":[],"firstLines":[],"parser":"markdown:juliamarkdown","supportLevel":"grammar-dialect"},
  {"id":"latex","displayName":"LaTeX","aliases":["LaTeX","latex","ltx"],"extensions":[".tex",".ltx",".ctx"],"filenames":[],"patterns":[],"firstLines":[],"parser":"legacy:stex:stex","supportLevel":"stream"},
  {"id":"less","displayName":"Less","aliases":["Less","less"],"extensions":[".less"],"filenames":[],"patterns":[],"firstLines":[],"parser":"legacy:css:less","supportLevel":"stream"},
  {"id":"log","displayName":"Log","aliases":["Log"],"extensions":[".log","*.log.?"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:log","supportLevel":"lexical"},
  {"id":"lua","displayName":"Lua","aliases":["Lua","lua"],"extensions":[".lua",".script",".gui_script",".render_script"],"filenames":[],"patterns":[],"firstLines":[],"parser":"legacy:lua:lua","supportLevel":"stream"},
  {"id":"makefile","displayName":"Makefile","aliases":["Makefile","makefile"],"extensions":[".mak",".mk"],"filenames":["Makefile","makefile","GNUmakefile","OCamlMakefile"],"patterns":[],"firstLines":["^#!\\s*/usr/bin/make"],"parser":"lexical:makefile","supportLevel":"lexical"},
  {"id":"markdown","displayName":"Markdown","aliases":["Markdown","markdown"],"extensions":[".copilotmd",".md",".mkd",".mkdn",".mdwn",".mdown",".markdown",".markdn",".mdtxt",".mdtext",".litcoffee",".ron",".ronn",".workbook",".mdx"],"filenames":[],"patterns":["**/.cursor/**/*.mdc"],"firstLines":[],"parser":"lezer:markdown","supportLevel":"grammar"},
  {"id":"markdown-math","displayName":"Markdown Math","aliases":[],"extensions":[],"filenames":[],"patterns":[],"firstLines":[],"parser":"markdown:markdown-math","supportLevel":"grammar-dialect","helper":true,"selectable":false},
  {"id":"markdown_latex_combined","displayName":"Markdown LaTeX","aliases":[],"extensions":[],"filenames":[],"patterns":[],"firstLines":[],"parser":"markdown:markdown_latex_combined","supportLevel":"grammar-dialect","helper":true,"selectable":false},
  {"id":"objective-c","displayName":"Objective-C","aliases":["Objective-C"],"extensions":[".m"],"filenames":[],"patterns":[],"firstLines":[],"parser":"legacy:clike:objectiveC","supportLevel":"stream"},
  {"id":"objective-cpp","displayName":"Objective-C++","aliases":["Objective-C++"],"extensions":[".mm"],"filenames":[],"patterns":[],"firstLines":[],"parser":"legacy:clike:objectiveCpp","supportLevel":"stream"},
  {"id":"perl","displayName":"Perl","aliases":["Perl","perl"],"extensions":[".pl",".pm",".pod",".t",".PL",".psgi"],"filenames":[],"patterns":[],"firstLines":["^#!.*\\bperl\\b"],"parser":"legacy:perl:perl","supportLevel":"stream"},
  {"id":"php","displayName":"PHP","aliases":["PHP","php"],"extensions":[".php",".php4",".php5",".phtml",".ctp"],"filenames":[],"patterns":[],"firstLines":["^#!\\s*/.*\\bphp\\b"],"parser":"lezer:php","supportLevel":"grammar"},
  {"id":"powershell","displayName":"PowerShell","aliases":["PowerShell","powershell","ps","ps1","pwsh"],"extensions":[".ps1",".psm1",".psd1",".pssc",".psrc"],"filenames":[],"patterns":[],"firstLines":["^#!\\s*/.*\\bpwsh\\b"],"parser":"legacy:powershell:powerShell","supportLevel":"stream"},
  {"id":"prompt","displayName":"Prompt","aliases":["Prompt","prompt"],"extensions":[".prompt.md"],"filenames":[],"patterns":[],"firstLines":[],"parser":"markdown:prompt","supportLevel":"grammar-dialect"},
  {"id":"properties","displayName":"Properties","aliases":["Properties","properties"],"extensions":[".conf",".properties",".cfg",".directory",".gitattributes",".gitconfig",".gitmodules",".editorconfig",".repo",".npmrc"],"filenames":["gitconfig"],"patterns":["**/.config/git/config","**/.git/config"],"firstLines":[],"parser":"lexical:properties","supportLevel":"lexical"},
  {"id":"python","displayName":"Python","aliases":["Python","py"],"extensions":[".py",".rpy",".pyw",".cpy",".gyp",".gypi",".pyi",".ipy",".pyt"],"filenames":["SConstruct","SConscript"],"patterns":[],"firstLines":["^#!\\s*/?.*\\bpython[0-9.-]*\\b"],"parser":"lezer:python","supportLevel":"grammar"},
  {"id":"r","displayName":"R","aliases":["R","r"],"extensions":[".R",".Rhistory",".Rprofile",".rt"],"filenames":[],"patterns":[],"firstLines":[],"parser":"legacy:r:r","supportLevel":"stream"},
  {"id":"raku","displayName":"Raku","aliases":["Raku","Perl6","perl6"],"extensions":[".raku",".rakumod",".rakutest",".rakudoc",".nqp",".p6",".pl6",".pm6"],"filenames":[],"patterns":[],"firstLines":["(^#!.*\\bperl6\\b)|use\\s+v6|raku|=begin\\spod|my\\sclass"],"parser":"lexical:raku","supportLevel":"lexical"},
  {"id":"razor","displayName":"Razor","aliases":["Razor","razor"],"extensions":[".cshtml",".razor"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:razor","supportLevel":"lexical"},
  {"id":"restructuredtext","displayName":"reStructuredText","aliases":["reStructuredText"],"extensions":[".rst"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:restructuredtext","supportLevel":"lexical"},
  {"id":"ruby","displayName":"Ruby","aliases":["Ruby","rb"],"extensions":[".rb",".rbx",".rjs",".gemspec",".rake",".ru",".erb",".podspec",".rbi"],"filenames":["rakefile","gemfile","guardfile","podfile","capfile","cheffile","hobofile","vagrantfile","appraisals","rantfile","berksfile","berksfile.lock","thorfile","puppetfile","dangerfile","brewfile","fastfile","appfile","deliverfile","matchfile","scanfile","snapfile","gymfile"],"patterns":[],"firstLines":["^#!\\s*/.*\\bruby\\b"],"parser":"legacy:ruby:ruby","supportLevel":"stream"},
  {"id":"rust","displayName":"Rust","aliases":["Rust","rust"],"extensions":[".rs"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lezer:rust","supportLevel":"grammar"},
  {"id":"scss","displayName":"SCSS","aliases":["SCSS","scss","sass"],"extensions":[".scss"],"filenames":[],"patterns":[],"firstLines":[],"parser":"legacy:css:sCSS","supportLevel":"stream"},
  {"id":"search-result","displayName":"Search Result","aliases":["Search Result"],"extensions":[".code-search"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:search-result","supportLevel":"lexical"},
  {"id":"shaderlab","displayName":"ShaderLab","aliases":["ShaderLab","shaderlab"],"extensions":[".shader"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:shaderlab","supportLevel":"lexical"},
  {"id":"shellscript","displayName":"Shell","aliases":["Shell Script","shellscript","bash","fish","sh","zsh","ksh","csh"],"extensions":[".sh",".bash",".bashrc",".bash_aliases",".bash_profile",".bash_login",".ebuild",".eclass",".profile",".bash_logout",".xprofile",".xsession",".xsessionrc",".Xsession",".zsh",".zshrc",".zprofile",".zlogin",".zlogout",".zshenv",".zsh-theme",".fish",".ksh",".csh",".cshrc",".tcshrc",".yashrc",".yash_profile"],"filenames":["APKBUILD","PKGBUILD",".envrc",".hushlogin","zshrc","zshenv","zlogin","zprofile","zlogout","bashrc_Apple_Terminal","zshrc_Apple_Terminal"],"patterns":[],"firstLines":["^#!.*\\b(bash|fish|zsh|sh|ksh|dtksh|pdksh|mksh|ash|dash|yash|sh|csh|jcsh|tcsh|itcsh).*|^#\\s*-\\*-[^*]*mode:\\s*shell-script[^*]*-\\*-"],"parser":"legacy:shell:shell","supportLevel":"stream"},
  {"id":"skill","displayName":"Skill","aliases":["Skill","skill"],"extensions":[],"filenames":["SKILL.md"],"patterns":[],"firstLines":[],"parser":"markdown:skill","supportLevel":"grammar-dialect"},
  {"id":"snippets","displayName":"Code Snippets","aliases":["Code Snippets"],"extensions":[".code-snippets"],"filenames":[],"patterns":["**/User/snippets/*.json","**/User/profiles/*/snippets/*.json","**/snippets*.json"],"firstLines":[],"parser":"lexical:snippets","supportLevel":"lexical"},
  {"id":"sql","displayName":"SQL","aliases":["MS SQL","T-SQL"],"extensions":[".sql",".dsql"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lezer:sql","supportLevel":"grammar"},
  {"id":"swift","displayName":"Swift","aliases":["Swift","swift"],"extensions":[".swift"],"filenames":[],"patterns":[],"firstLines":[],"parser":"legacy:swift:swift","supportLevel":"stream"},
  {"id":"tex","displayName":"TeX","aliases":["TeX","tex"],"extensions":[".sty",".cls",".bbx",".cbx"],"filenames":[],"patterns":[],"firstLines":[],"parser":"legacy:stex:stex","supportLevel":"stream"},
  {"id":"typescript","displayName":"TypeScript","aliases":["TypeScript","ts","typescript"],"extensions":[".ts",".cts",".mts"],"filenames":[],"patterns":[],"firstLines":["^#!.*\\b(deno|bun|ts-node)\\b"],"parser":"lezer:javascript","supportLevel":"grammar"},
  {"id":"typescriptreact","displayName":"TypeScript JSX","aliases":["TypeScript JSX","TypeScript React","tsx"],"extensions":[".tsx"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lezer:javascript","supportLevel":"grammar"},
  {"id":"vb","displayName":"Visual Basic","aliases":["Visual Basic","vb"],"extensions":[".vb",".brs",".vbs",".bas",".vba"],"filenames":[],"patterns":[],"firstLines":[],"parser":"legacy:vb:vb","supportLevel":"stream"},
  {"id":"wat","displayName":"WebAssembly Text","aliases":["WebAssembly Text Format"],"extensions":[".wat"],"filenames":[],"patterns":[],"firstLines":["^\\(module"],"parser":"legacy:wast:wast","supportLevel":"stream"},
  {"id":"xml","displayName":"XML","aliases":["XML","xml"],"extensions":[".xml",".xsd",".ascx",".atom",".axml",".axaml",".bpmn",".cpt",".csl",".csproj",".csproj.user",".dita",".ditamap",".dtd",".ent",".mod",".dtml",".fsproj",".fxml",".iml",".isml",".jmx",".launch",".menu",".mxml",".nuspec",".opml",".owl",".proj",".props",".pt",".publishsettings",".pubxml",".pubxml.user",".rbxlx",".rbxmx",".rdf",".rng",".rss",".shproj",".slnx",".storyboard",".svg",".targets",".tld",".tmx",".vbproj",".vbproj.user",".vcxproj",".vcxproj.filters",".wixproj",".wsdl",".wxi",".wxl",".wxs",".xaml",".xbl",".xib",".xlf",".xliff",".xpdl",".xul",".xoml"],"filenames":[],"patterns":[],"firstLines":["(\\<\\?xml.*)|(\\<svg)|(\\<\\!doctype\\s+svg)"],"parser":"lezer:xml","supportLevel":"grammar"},
  {"id":"xsl","displayName":"XSL","aliases":["XSL","xsl"],"extensions":[".xsl",".xslt"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lezer:xml","supportLevel":"grammar"},
  {"id":"yaml","displayName":"YAML","aliases":["YAML","yaml"],"extensions":[".yaml",".yml",".eyaml",".eyml",".cff",".yaml-tmlanguage",".yaml-tmpreferences",".yaml-tmtheme",".winget"],"filenames":[],"patterns":[],"firstLines":["^#cloud-config"],"parser":"lezer:yaml","supportLevel":"grammar"},
  {"id":"plaintext","displayName":"Plain text","aliases":[],"extensions":[".txt",".text",".csv",".tsv",".config"],"filenames":["LICENSE","README","Procfile","justfile",".nvmrc"],"patterns":[],"firstLines":[],"parser":"plain","supportLevel":"plain"},
  {"id":"kotlin","displayName":"Kotlin","aliases":["kt","kts"],"extensions":[".kt",".kts"],"filenames":[],"patterns":[],"firstLines":[],"parser":"legacy:clike:kotlin","supportLevel":"stream"},
  {"id":"toml","displayName":"TOML","aliases":[],"extensions":[".toml"],"filenames":[],"patterns":[],"firstLines":[],"parser":"legacy:toml:toml","supportLevel":"stream"},
  {"id":"mermaid","displayName":"Mermaid","aliases":["mmd"],"extensions":[".mmd",".mermaid"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:mermaid","supportLevel":"lexical"},
  {"id":"json5","displayName":"JSON5","aliases":[],"extensions":[".json5"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:json5","supportLevel":"lexical"},
  {"id":"cmake","displayName":"CMake","aliases":[],"extensions":[".cmake"],"filenames":["CMakeLists.txt"],"patterns":[],"firstLines":[],"parser":"legacy:cmake:cmake","supportLevel":"stream"},
  {"id":"gds","displayName":"GDScript","aliases":["gd","gdscript"],"extensions":[".gd"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:gds","supportLevel":"lexical"},
  {"id":"gdshader","displayName":"Godot Shader","aliases":["gdshaderinc","godot shader"],"extensions":[".gdshader",".gdshaderinc"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:gdshader","supportLevel":"lexical"},
  {"id":"godot-resource","displayName":"Godot Resource","aliases":["tscn","tres","godot"],"extensions":[".tscn",".tres",".escn",".gdextension"],"filenames":["project.godot","export_presets.cfg"],"patterns":[],"firstLines":[],"parser":"lexical:godot-resource","supportLevel":"lexical"},
  {"id":"glsl","displayName":"GLSL","aliases":[],"extensions":[".glsl",".vert",".frag",".geom",".tesc",".tese",".comp"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:glsl","supportLevel":"lexical"},
  {"id":"wgsl","displayName":"WGSL","aliases":[],"extensions":[".wgsl"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:wgsl","supportLevel":"lexical"},
  {"id":"unreal-shader","displayName":"Unreal Shader","aliases":["usf","ush"],"extensions":[".usf",".ush"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:unreal-shader","supportLevel":"lexical"},
  {"id":"unrealscript","displayName":"UnrealScript","aliases":[],"extensions":[".uc"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:unrealscript","supportLevel":"lexical"},
  {"id":"squirrel","displayName":"Squirrel","aliases":[],"extensions":[".nut"],"filenames":[],"patterns":[],"firstLines":[],"parser":"legacy:clike:squirrel","supportLevel":"stream"},
  {"id":"sourcepawn","displayName":"SourcePawn","aliases":["sp"],"extensions":[".sp"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:sourcepawn","supportLevel":"lexical"},
  {"id":"keyvalues","displayName":"Valve KeyValues","aliases":["vdf","kv","vmt"],"extensions":[".vdf",".kv",".vmt",".res"],"filenames":["gameinfo.txt"],"patterns":[],"firstLines":[],"parser":"lexical:keyvalues","supportLevel":"lexical"},
  {"id":"kv3","displayName":"Valve KeyValues3","aliases":["kv3"],"extensions":[".kv3",".vmat",".vmap",".vmdl"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:kv3","supportLevel":"lexical"},
  {"id":"source-config","displayName":"Source Config","aliases":["source cfg"],"extensions":[],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:source-config","supportLevel":"lexical"},
  {"id":"vmf","displayName":"Valve Map","aliases":["vmf"],"extensions":[".vmf"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:vmf","supportLevel":"lexical"},
  {"id":"fgd","displayName":"Forge Game Data","aliases":["fgd"],"extensions":[".fgd"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:fgd","supportLevel":"lexical"},
  {"id":"papyrus","displayName":"Papyrus","aliases":["psc"],"extensions":[".psc"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:papyrus","supportLevel":"lexical"},
  {"id":"papyrus-flags","displayName":"Papyrus Flags","aliases":[],"extensions":[".flg"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:papyrus-flags","supportLevel":"lexical"},
  {"id":"geck","displayName":"GECK Script","aliases":["geck","bethesda legacy","tes script"],"extensions":[],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:geck","supportLevel":"lexical"},
  {"id":"mgcb","displayName":"MonoGame Content Builder","aliases":["mgcb"],"extensions":[".mgcb"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:mgcb","supportLevel":"lexical"},
  {"id":"gml","displayName":"GameMaker Language","aliases":["gml"],"extensions":[".gml"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:gml","supportLevel":"lexical"},
  {"id":"renpy","displayName":"Ren'Py","aliases":["renpy"],"extensions":[".rpy",".rpym"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:renpy","supportLevel":"lexical"},
  {"id":"uxml","displayName":"Unity UXML","aliases":["uxml"],"extensions":[".uxml"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lezer:xml","supportLevel":"grammar-dialect"},
  {"id":"uss","displayName":"Unity USS","aliases":["uss"],"extensions":[".uss"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:uss","supportLevel":"lexical"},
  {"id":"unity-yaml","displayName":"Unity YAML","aliases":["unity yaml"],"extensions":[],"filenames":[],"patterns":[],"firstLines":[],"parser":"lezer:yaml","supportLevel":"grammar-dialect"},
  {"id":"obj","displayName":"Wavefront OBJ","aliases":["obj","mtl"],"extensions":[".obj",".mtl"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:obj","supportLevel":"lexical"},
  {"id":"sass","displayName":"Sass","aliases":[],"extensions":[".sass"],"filenames":[],"patterns":[],"firstLines":[],"parser":"legacy:sass:sass","supportLevel":"stream"},
  {"id":"haxe","displayName":"Haxe","aliases":["hx"],"extensions":[".hx"],"filenames":[],"patterns":[],"firstLines":[],"parser":"legacy:haxe:haxe","supportLevel":"stream"},
  {"id":"hxml","displayName":"Haxe Build","aliases":["hxml"],"extensions":[".hxml"],"filenames":[],"patterns":[],"firstLines":[],"parser":"legacy:haxe:hxml","supportLevel":"stream"},
  {"id":"luau","displayName":"Luau","aliases":["roblox","roblox luau"],"extensions":[".luau"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:luau","supportLevel":"lexical"},
  {"id":"pico8","displayName":"PICO-8 Cartridge","aliases":["pico-8","p8"],"extensions":[".p8"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:pico8","supportLevel":"lexical"},
  {"id":"angelscript","displayName":"AngelScript","aliases":["as","angelscript"],"extensions":[],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:angelscript","supportLevel":"lexical"},
  {"id":"quakec","displayName":"QuakeC","aliases":["qc"],"extensions":[],"filenames":["progs.src"],"patterns":[],"firstLines":[],"parser":"lexical:quakec","supportLevel":"lexical"},
  {"id":"source-qc","displayName":"Source Model QC","aliases":[],"extensions":[],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:source-qc","supportLevel":"lexical"},
  {"id":"acs","displayName":"Doom ACS","aliases":["acs"],"extensions":[".acs"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:acs","supportLevel":"lexical"},
  {"id":"zscript","displayName":"Doom ZScript","aliases":["zscript"],"extensions":[".zs"],"filenames":["ZSCRIPT"],"patterns":[],"firstLines":[],"parser":"lexical:zscript","supportLevel":"lexical"},
  {"id":"ink","displayName":"Ink","aliases":["inkle"],"extensions":[".ink"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:ink","supportLevel":"lexical"},
  {"id":"yarn","displayName":"Yarn Spinner","aliases":["yarn"],"extensions":[".yarn"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:yarn","supportLevel":"lexical"},
  {"id":"twee","displayName":"Twine/Twee","aliases":["twine","twee","tw"],"extensions":[".twee",".tw"],"filenames":[],"patterns":[],"firstLines":[],"parser":"lexical:twee","supportLevel":"lexical"},
 ];
function freezeDefinition(value) {
  for (const key of ['aliases', 'extensions', 'filenames', 'patterns', 'firstLines']) Object.freeze(value[key]);
  return Object.freeze({ ...value, semanticServices:false, commentCapability:value.parser === 'plain' ? 'none' : 'syntax-derived', selectable:value.selectable !== false });
}
export const LANGUAGE_REGISTRY = Object.freeze(definitions.map(freezeDefinition));
const byId = new Map(LANGUAGE_REGISTRY.map(entry => [entry.id, entry]));
const aliases = new Map();
for (const entry of LANGUAGE_REGISTRY) {
  for (const alias of [entry.id, entry.displayName, entry.modeName, ...entry.aliases].filter(Boolean)) {
    const key = String(alias).toLowerCase();
    // Keep JSON = strict JSON; JSONC's compatible display label must not steal it.
    if (!aliases.has(key) || key === entry.id) aliases.set(key, entry);
  }
}
const filenameRules = LANGUAGE_REGISTRY.flatMap(entry => entry.filenames.map(name => ({name:name.toLowerCase(), entry})));
const suffixRules = LANGUAGE_REGISTRY.flatMap(entry => entry.extensions.filter(suffix => !/[?*]/.test(suffix)).map(suffix => ({suffix:suffix.toLowerCase(), entry})))
  .sort((a,b) => b.suffix.length - a.suffix.length || Number(b.entry.id === 'renpy') - Number(a.entry.id === 'renpy'));
function globExpression(pattern) {
  const parts = pattern.split(/(\*\*\/|\*\*|\*|\?)/);
  return new RegExp('^' + parts.map(part => part === '**/' ? '(?:.*/)?' : part === '**' ? '.*' : part === '*' ? '[^/]*' : part === '?' ? '[^/]' : part.replace(/[.+^${}()|[\]\\]/g, '\\$&')).join('') + '$', 'i');
}
const patternRules = LANGUAGE_REGISTRY.flatMap(entry => [...entry.patterns, ...entry.extensions.filter(suffix => /[?*]/.test(suffix))].map(pattern => ({pattern, expression:globExpression(pattern), entry})));
const firstLineRules = LANGUAGE_REGISTRY.flatMap(entry => entry.firstLines.map(pattern => ({expression:new RegExp(pattern),entry})));
const binarySuffix = /\.(?:wasm|pex|rpyc|rpa|uasset|umap|bsp|scn|res_c|v[a-z0-9]+_c|xnb|mgfxo|nif|esp|esm|esl|dll|exe|o|a|so|dylib|png|jpe?g|dds|zip|7z|blend|fbx)$/i;
export const EDITOR_CONDITIONAL_TEXT_SUFFIXES = Object.freeze(['.unity','.prefab','.asset','.mat','.meta','.controller','.anim','.obj','.res','.kv3','.vmat','.vmap','.vmdl','.as','.qc','.inc']);
export const EDITOR_BINARY_SUFFIX_PATTERN = binarySuffix.source;
const conditionalAsset = /\.(?:unity|prefab|asset|mat|meta|controller|anim)$/i;
const plain = byId.get('plaintext');
function sampleContent(options) { return typeof options.content === 'string' ? options.content.slice(0, 4096) : String(options.firstLine || '').slice(0, 4096); }
function pathInfo(path) {
  const normalized = String(path || '').replace(/\\/g,'/');
  return { path:normalized, leaf:normalized.split('/').pop() || '' };
}
function pathLanguage(path, options = {}) {
  const info = pathInfo(path), leaf = info.leaf.toLowerCase(), sample = sampleContent(options);
  if (binarySuffix.test(leaf) || sample.includes('\0')) return null;
  if (conditionalAsset.test(leaf)) return /^%YAML|^---\s*!u!/m.test(sample) ? byId.get('unity-yaml') : null;
  if (/\.obj$/i.test(leaf) && !/^\s*(?:v|vt|vn|f|o|g|mtllib)\s/m.test(sample)) return null;
  if (/\.res$/i.test(leaf) && !/^\s*(?:"[^"\n]+"|[A-Za-z_]\w*)\s*\{/m.test(sample)) return null;
  // Ambiguous formats only change language when actual text identifies the dialect.
  if (/\.tsx$/i.test(leaf) && /^\s*(?:<\?xml|<tileset\b)/.test(sample)) return byId.get('xml');
  if (/\.shader$/i.test(leaf) && /\bshader_type\s+(?:spatial|canvas_item|particles|sky|fog)\s*;/.test(sample)) return byId.get('gdshader');
  if (/\.as$/i.test(leaf)) return /\b(?:void|int|uint|string|float|double)\s+[A-Za-z_]\w*\s*\(/.test(sample) ? byId.get('angelscript') : null;
  if (/\.qc$/i.test(leaf)) return /^\s*\$[A-Za-z_]/m.test(sample) ? byId.get('source-qc') : /\b(?:void|float|vector|entity)\b/.test(sample) ? byId.get('quakec') : null;
  if (/\.inc$/i.test(leaf)) return /^\s*(?:#pragma\s+(?:semicolon|newdecls)|native\s+\w+|methodmap\s+\w+)/m.test(sample) ? byId.get('sourcepawn') : null;
  if (/\.cfg$/i.test(leaf) && (/(?:^|\/)(?:csgo|tf|hl2|game)\/cfg\//i.test(info.path) || /^\s*(?:bind|alias|exec|sv_\w+|cl_\w+)\s/m.test(sample))) return byId.get('source-config');
  if (/\.txt$/i.test(leaf) && /^\s*(?:scriptname|scn)\s+\w+/im.test(sample) && /^\s*begin\s+\w+/im.test(sample)) return byId.get('geck');
  if (/\.(?:kv3|vmat|vmap|vmdl)$/i.test(leaf) && !/^\s*<!--\s*kv3\b/i.test(sample)) return null;
  const exact = filenameRules.find(rule => leaf === rule.name);
  if (exact) return exact.entry;
  const pattern = patternRules.find(rule => rule.expression.test(info.path) || rule.expression.test(leaf));
  if (pattern) return pattern.entry;
  const suffix = suffixRules.find(rule => leaf.endsWith(rule.suffix));
  if (suffix && suffix.entry.id !== 'plaintext') return suffix.entry;
  const firstLine = String(options.firstLine || sample.split(/\r?\n/,1)[0] || '').slice(0,1024);
  const first = firstLineRules.find(rule => rule.expression.test(firstLine));
  return first?.entry || suffix?.entry || null;
}
/** Explicit language choice wins over ambiguous filenames; binary bytes never become source. */
export function resolveLanguage(language, options = {}) {
  const sample = sampleContent(options);
  if (sample.includes('\0') || binarySuffix.test(String(options.path || ''))) return plain;
  const requested = String(language || '').trim().toLowerCase();
  if (requested) {
    const selected = aliases.get(requested);
    if (selected) {
      if (selected.id === 'json' && (String(options.dialect || '').toLowerCase() === 'jsonc' || pathLanguage(options.path,options)?.id === 'jsonc')) return byId.get('jsonc');
      return selected;
    }
  }
  return pathLanguage(options.path, options) || plain;
}
export function languageForPath(path, options = {}) {
  const entry = resolveLanguage('', {...options,path});
  const leaf = pathInfo(path).leaf.toLowerCase();
  if (entry.id === 'cpp' && /\.h$/.test(leaf)) return 'C/C++ header';
  if (entry.id === 'cpp' && /\.(?:hpp|hh|hxx|h\+\+)$/.test(leaf)) return 'C++ header';
  return entry.displayName;
}
export function languageDialectForPath(path, options = {}) {
  const entry = resolveLanguage('', {...options,path});
  return ['jsonc','json5','jsonl'].includes(entry.id) ? entry.id : '';
}
/** Text hint only: server MIME/content checks still own edit authority. */
export function isTextPath(path, options = {}) { return Boolean(pathLanguage(path,options)); }
export function sourceLanguageMetadata(language, options = {}) { return resolveLanguage(language,options); }
export const ADVERTISED_LANGUAGE_LABELS = Object.freeze([...new Set([...["JavaScript", "JavaScript JSX", "TypeScript", "TypeScript JSX", "Python", "Rust", "Go", "Java", "Kotlin", "C", "C/C++ header", "C++", "C++ header", "C#", "Ruby", "PHP", "Swift", "Shell", "JSON", "YAML", "TOML", "XML", "HTML", "CSS", "SCSS", "Markdown", "SQL", "Mermaid", "Dockerfile", "Plain text"], ...LANGUAGE_REGISTRY.filter(entry => entry.selectable).map(entry => entry.modeName || entry.displayName)])]);
