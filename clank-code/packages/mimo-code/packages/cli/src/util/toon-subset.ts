/** Strict canonical TOON 4.4 subset. No host-object coercion or source-byte claim. */
export type ToonOptions = { delimiter?: "comma" | "tab" | "pipe"; indentSize?: number };
type ResolvedToonOptions = { delimiter: "," | "\t" | "|"; marker: "" | "\t" | "|"; indentSize: number };
function resolveOptions(options?: ToonOptions): ResolvedToonOptions {
  if (options !== undefined && (options === null || typeof options !== "object" || Array.isArray(options) || Reflect.ownKeys(options).some((k) => k !== "delimiter" && k !== "indentSize")))
    throw new SyntaxError("Invalid TOON options");
  const delimiter = options?.delimiter === undefined ? "comma" : options.delimiter,
    indentSize = options?.indentSize === undefined ? 2 : options.indentSize;
  if ((delimiter !== "comma" && delimiter !== "tab" && delimiter !== "pipe") || !Number.isInteger(indentSize) || indentSize < 1 || indentSize > 8)
    throw new SyntaxError("Invalid TOON options");
  return { delimiter: delimiter === "comma" ? "," : delimiter === "tab" ? "\t" : "|", marker: delimiter === "comma" ? "" : delimiter === "tab" ? "\t" : "|", indentSize };
}
type Atom = null | boolean | number | string;
export type Node =
  | { k: "a"; v: Atom }
  | { k: "o"; v: [string, Node][] }
  | { k: "l"; v: Node[] };
const LIMIT = 51200,
  DEPTH = 32,
  VALUES = 16384;
const utf8Bytes = (s: string) => Buffer.byteLength(s, "utf8");
const validateUnicodeScalar = (s: string) => {
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i);
    if (c >= 0xd800 && c <= 0xdbff) {
      const d = s.charCodeAt(++i);
      if (!(d >= 0xdc00 && d <= 0xdfff))
        throw new SyntaxError("Invalid or unsupported TOON subset input");
    } else if (c >= 0xdc00 && c <= 0xdfff)
      throw new SyntaxError("Invalid or unsupported TOON subset input");
  }
  return s;
};
function parseJson(text: string, spans?: WeakMap<Node, Readonly<{start:number;end:number}>>): Node {
  if (typeof text !== "string" || utf8Bytes(text) > LIMIT)
    throw new SyntaxError("Invalid or unsupported TOON subset input");
  let i = 0,
    count = 0;
  const ws = () => {
    while (/[\x20\t\r\n]/.test(text[i] ?? "\0")) i++;
  };
  const str = () => {
    const start = i++;
    let escaped = false;
    while (i < text.length) {
      const c = text[i++]!;
      if (escaped) {
        escaped = false;
        continue;
      }
      if (c === "\\") {
        escaped = true;
        continue;
      }
      if (c === '"')
        return validateUnicodeScalar(JSON.parse(text.slice(start, i)));
    }
    throw new SyntaxError("Invalid or unsupported TOON subset input");
  };
  const value = (depth: number): Node => {
    if (++count > VALUES || depth > DEPTH)
      throw new SyntaxError("Invalid or unsupported TOON subset input");
    ws();
    const start = i;
    const parsed = (): Node => {
    const c = text[i];
    if (c === '"') return { k: "a", v: str() };
    if (c === "{") {
      i++;
      ws();
      const v: [string, Node][] = [],
        seen = new Set<string>();
      if (text[i] === "}") {
        i++;
        return { k: "o", v };
      }
      for (;;) {
        ws();
        if (text[i] !== '"')
          throw new SyntaxError("Invalid or unsupported TOON subset input");
        const key = str();
        if (seen.has(key))
          throw new SyntaxError("Invalid or unsupported TOON subset input");
        seen.add(key);
        ws();
        if (text[i++] !== ":")
          throw new SyntaxError("Invalid or unsupported TOON subset input");
        v.push([key, value(depth + 1)]);
        ws();
        const next = text[i++];
        if (next === "}") break;
        if (next !== ",")
          throw new SyntaxError("Invalid or unsupported TOON subset input");
      }
      return { k: "o", v };
    }
    if (c === "[") {
      i++;
      ws();
      const v: Node[] = [];
      if (text[i] === "]") {
        i++;
        return { k: "l", v };
      }
      for (;;) {
        v.push(value(depth + 1));
        ws();
        const next = text[i++];
        if (next === "]") break;
        if (next !== ",")
          throw new SyntaxError("Invalid or unsupported TOON subset input");
      }
      return { k: "l", v };
    }
    for (const [word, v] of [
      ["true", true],
      ["false", false],
      ["null", null],
    ] as const)
      if (text.startsWith(word, i)) {
        i += word.length;
        return { k: "a", v };
      }
    const match = /^-?(?:0|[1-9]\d*)/.exec(text.slice(i));
    if (!match || match[0] === "-0")
      throw new SyntaxError("Invalid or unsupported TOON subset input");
    i += match[0].length;
    const n = Number(match[0]);
    if (!Number.isSafeInteger(n))
      throw new SyntaxError("Invalid or unsupported TOON subset input");
    return { k: "a", v: n };
    };
    const node = parsed();
    spans?.set(node, Object.freeze({start, end:i}));
    return node;
  };
  const out = value(0);
  ws();
  if (i !== text.length)
    throw new SyntaxError("Invalid or unsupported TOON subset input");
  return out;
}
export function parseJsonInput(text: string): Node { return parseJson(text); }

/** Immutable value-token evidence in UTF-16 indices into this exact source. */
export function parseJsonInputWithSpans(text: string): Readonly<{root:Node; spanOf:(node:Node)=>Readonly<{start:number;end:number}>|undefined}> {
  const spans = new WeakMap<Node, Readonly<{start:number;end:number}>>();
  const root = parseJson(text, spans);
  const freeze = (node:Node): void => {
    if (node.k === "o") { for (const entry of node.v) { freeze(entry[1]); Object.freeze(entry); } Object.freeze(node.v); }
    else if (node.k === "l") { for (const child of node.v) freeze(child); Object.freeze(node.v); }
    Object.freeze(node);
  };
  freeze(root);
  return Object.freeze({root, spanOf:(node:Node) => spans.get(node)});
}
const json = (n: Node): string =>
  n.k === "a"
    ? JSON.stringify(n.v)
    : n.k === "l"
      ? "[" + n.v.map(json).join(",") + "]"
      : "{" +
        n.v.map(([k, v]) => JSON.stringify(k) + ":" + json(v)).join(",") +
        "}";
export { json as serializeOrderedJson };
const quoteString = (s: string) =>
  '"' +
  Array.from(s, (c) => {
    const n = c.codePointAt(0)!;
    return c === "\\"
      ? "\\\\"
      : c === '"'
        ? '\\"'
        : c === "\n"
          ? "\\n"
          : c === "\r"
            ? "\\r"
            : c === "\t"
              ? "\\t"
              : n < 32
                ? "\\u" + n.toString(16).padStart(4, "0")
                : c;
  }).join("") +
  '"';
const key = (s: string) =>
  /^[A-Za-z_][A-Za-z0-9_.]*$/.test(s) ? s : quoteString(s);
const token = (v: Atom, options: ResolvedToonOptions, root = false): string =>
  typeof v !== "string"
    ? String(v)
    : !v ||
        /^[ \t]|[ \t]$/.test(v) ||
        /^(?:true|false|null)$/.test(v) ||
        /^[+-]?[0-9]+(?:\.[0-9]+)?(?:e[+-]?[0-9]+)?$/i.test(v) ||
        /[:"\\\[\]{}\x00-\x1f]/.test(v) ||
        v.includes(options.delimiter) ||
        /^[-#]/.test(v) ||
        (root && v.startsWith("\ufeff"))
      ? quoteString(v)
      : v;
const isAtom = (n: Node) => n.k === "a";
function requiresKeyedTable(n: Node): boolean {
  if (n.k !== "o" || n.v.length < 2) return false;
  const vals = n.v.map((x) => x[1]);
  if (vals.some((x) => x.k !== "o" || !x.v.length)) return false;
  const first = vals[0] as Extract<Node, { k: "o" }>;
  return vals.every(
    (x) =>
      x.k === "o" &&
      x.v.length === first.v.length &&
      first.v.every(([k]) => x.v.some(([kk]) => kk === k)),
  );
}
function* renderLines(root: Node, options: ResolvedToonOptions): Generator<string> {
  function* emit(n: Node, prefix: string, depth: number): Generator<string> {
    if (depth > DEPTH)
      throw new SyntaxError("Invalid or unsupported TOON subset input");
    const pad = " ".repeat(options.indentSize * depth);
    if (requiresKeyedTable(n)) {
      if (n.k !== "o") throw new SyntaxError("Invalid or unsupported TOON subset input");
      const first = n.v[0]![1] as Extract<Node, { k: "o" }>, names = first.v.map(([k]) => k);
      if (!first.v.every(([, v]) => isAtom(v)) || !n.v.every(([, row]) => row.k === "o" && row.v.length === names.length && row.v.every(([k, v], i) => k === names[i] && isAtom(v))))
        throw new SyntaxError("Invalid or unsupported TOON subset input");
      yield (pad + prefix + "[" + n.v.length + ":" + options.marker + "]{" + names.map(key).join(options.delimiter) + "}:");
      for (const [entryKey, row] of n.v) {
        if (row.k !== "o") throw new SyntaxError("Invalid or unsupported TOON subset input");
        yield (" ".repeat(options.indentSize * (depth + 1)) + key(entryKey) + ": " + row.v.map(([, v]) => token((v as Extract<Node, { k: "a" }>).v, options)).join(options.delimiter));
      }
      return;
    }
    if (n.k === "a") {
      yield (pad + (prefix ? prefix + ": " : "") + token(n.v, options, !prefix));
      return;
    }
    if (n.k === "o") {
      if (prefix) yield (pad + prefix + ":");
      for (const [k, v] of n.v) yield* emit(v, key(k), prefix ? depth + 1 : depth);
      return;
    }
    if (!n.v.length) {
      yield (pad + (prefix ? prefix + ": " : "") + "[]");
      return;
    }
    if (n.v.every(isAtom)) {
      yield (
        pad +
          prefix +
          "[" +
          n.v.length +
          options.marker +
          "]: " +
          n.v.map((x) => token((x as Extract<Node, { k: "a" }>).v, options)).join(options.delimiter)
      );
      return;
    }
    if (
      !n.v.every(
        (x) => x.k === "o" && x.v.length > 0 && x.v.every(([, v]) => isAtom(v)),
      )
    )
      throw new SyntaxError("Invalid or unsupported TOON subset input");
    const first = n.v[0] as Extract<Node, { k: "o" }>,
      names = first.v.map((x) => x[0]);
    if (
      !n.v.every(
        (x) =>
          x.k === "o" &&
          x.v.length === names.length &&
          x.v.every(([k], i) => k === names[i]),
      )
    )
      throw new SyntaxError("Invalid or unsupported TOON subset input");
    yield (
      pad + prefix + "[" + n.v.length + options.marker + "]{" + names.map(key).join(options.delimiter) + "}:"
    );
    for (const row of n.v as Extract<Node, { k: "o" }>[])
      yield (
        " ".repeat(options.indentSize * (depth + 1)) +
          row.v
            .map(([, v]) => token((v as Extract<Node, { k: "a" }>).v, options))
            .join(options.delimiter)
      );
  }
  yield* emit(root, "", 0);
}
function render(root: Node, options: ResolvedToonOptions): string {
  const out = Array.from(renderLines(root, options)).join("\n");
  if (utf8Bytes(out) > LIMIT)
    throw new SyntaxError("Invalid or unsupported TOON subset input");
  return out;
}
export type ToonStreamEvent =
  | { type: "startObject" } | { type: "endObject" }
  | { type: "startArray"; length: number } | { type: "endArray" }
  | { type: "key"; key: string } | { type: "primitive"; value: Atom }
  | { type: "complete"; json: string };
type ParseStep = { kind: "peek" } | { kind: "consume" } | { kind: "event"; event: Exclude<ToonStreamEvent, {type:"complete"}> };
function* nodeEvents(node: Node): Generator<ParseStep, void, string | undefined> {
  if (node.k === "a") { yield {kind:"event",event:{type:"primitive",value:node.v}}; return; }
  if (node.k === "l") {
    yield {kind:"event",event:{type:"startArray",length:node.v.length}};
    for (const item of node.v) yield* nodeEvents(item);
    yield {kind:"event",event:{type:"endArray"}}; return;
  }
  yield {kind:"event",event:{type:"startObject"}};
  for (const [key,value] of node.v) { yield {kind:"event",event:{type:"key",key}}; yield* nodeEvents(value); }
  yield {kind:"event",event:{type:"endObject"}};
}
/** Shared pull parser; events are provisional until the driver validates EOF and canonical output. */
function* parseToonMachine(options: ResolvedToonOptions): Generator<ParseStep, Node, string | undefined> {
  let count = 0;
  const split = (s: string, sep: string): string[] => {
    let inQuote = false,
      escape = false,
      start = 0,
      out: string[] = [];
    for (let i = 0; i < s.length; i++) {
      const c = s[i]!;
      if (inQuote) {
        if (escape) escape = false;
        else if (c === "\\") escape = true;
        else if (c === '"') inQuote = false;
      } else if (c === '"') inQuote = true;
      else if (c === sep) {
        out.push(s.slice(start, i));
        start = i + 1;
      }
    }
    if (inQuote || escape)
      throw new SyntaxError("Invalid or unsupported TOON subset input");
    out.push(s.slice(start));
    return out;
  };
  const unquote = (s: string) => {
    if (!s.startsWith('"')) return validateUnicodeScalar(s);
    for (let index = 1; index < s.length; index++) {
      if (s[index] !== "\\") continue;
      const escape = s[++index];
      if (escape === "u") {
        const code = s.slice(index + 1, index + 5);
        if (/^[dD][89a-fA-F][0-9a-fA-F]{2}$/.test(code))
          throw new SyntaxError("TOON surrogate escape is unsupported");
        index += 4;
      }
    }
    return validateUnicodeScalar(JSON.parse(s));
  };
  const atom = (s: string): Node => {
    if (++count > VALUES)
      throw new SyntaxError("Invalid or unsupported TOON subset input");
    if (s.startsWith('"')) return { k: "a", v: unquote(s) };
    if (s === "true" || s === "false") return { k: "a", v: s === "true" };
    if (s === "null") return { k: "a", v: null };
    if (/^-?(?:0|[1-9]\d*)$/.test(s)) {
      const n = Number(s);
      if (s === "-0" || !Number.isSafeInteger(n))
        throw new SyntaxError("Invalid or unsupported TOON subset input");
      return { k: "a", v: n };
    }
    return { k: "a", v: validateUnicodeScalar(s) };
  };
  function* lineAt(depth: number): Generator<ParseStep, string | undefined, string | undefined> {
    const line = yield { kind: "peek" };
    if (line === undefined) return;
    const prefix = " ".repeat(options.indentSize * depth);
    if (!line.startsWith(prefix) || line.slice(prefix.length).startsWith(" "))
      return;
    return line.slice(prefix.length);
  };
  function* entry(depth: number, root: boolean): Generator<ParseStep, [string | undefined, Node], string | undefined> {
    if (depth > DEPTH || ++count > VALUES)
      throw new SyntaxError("Invalid or unsupported TOON subset input");
    const line = yield* lineAt(depth);
    if (line === undefined)
      throw new SyntaxError("Invalid or unsupported TOON subset input");
    yield { kind: "consume" };
    let h = line,
      colon = -1,
      inQuote = false,
      esc = false,
      bracket = false;
    for (let j = 0; j < line.length; j++) {
      const c = line[j]!;
      if (inQuote) {
        if (esc) esc = false;
        else if (c === "\\") esc = true;
        else if (c === '"') inQuote = false;
      } else if (c === '"') inQuote = true;
      else if (c === "[") bracket = true;
      else if (c === "]") bracket = false;
      else if (c === ":" && !bracket) {
        colon = j;
        break;
      }
    }
    if (colon < 0) {
      if (root) return [undefined, atom(line)];
      throw new SyntaxError("Invalid or unsupported TOON subset input");
    }
    h = line.slice(0, colon);
    const tail = line.slice(colon + 1);
    if (tail && !tail.startsWith(" "))
      throw new SyntaxError("Invalid or unsupported TOON subset input");
    const body = tail.slice(1);
    const keyed = /^(.*?)\[([1-9]\d*):([|\t]?)\]\{(.*)\}$/.exec(h);
    if (keyed) {
      if (tail !== "" || keyed[3] !== options.marker) throw new SyntaxError("Invalid or unsupported TOON subset input");
      const name = keyed[1] ? unquote(keyed[1]) : undefined, n = Number(keyed[2]);
      const fields = split(keyed[4]!, options.delimiter).map(unquote), seen = new Set<string>();
      if (!Number.isSafeInteger(n) || n < 2 || n > VALUES || !fields.length || new Set(fields).size !== fields.length)
        throw new SyntaxError("Invalid or unsupported TOON subset input");
      if (name !== undefined) yield {kind:"event",event:{type:"key",key:name}};
      yield {kind:"event",event:{type:"startObject"}};
      const members: [string, Node][] = [];
      for (let r = 0; r < n; r++) {
        if (++count > VALUES) throw new SyntaxError("Invalid or unsupported TOON subset input");
        const row = yield* lineAt(depth + 1);
        if (row === undefined) throw new SyntaxError("Invalid or unsupported TOON subset input");
        yield { kind: "consume" };
        const parts = split(row, ":");
        if (parts.length !== 2 || !parts[1]!.startsWith(" ")) throw new SyntaxError("Invalid or unsupported TOON subset input");
        const entryKey = unquote(parts[0]!), cells = split(parts[1]!.slice(1), options.delimiter);
        if (seen.has(entryKey) || cells.length !== fields.length) throw new SyntaxError("Invalid or unsupported TOON subset input");
        seen.add(entryKey);
        const value: Node = { k: "o", v: fields.map((f, j) => [f, atom(cells[j]!)]) };
        members.push([entryKey,value]);
        yield {kind:"event",event:{type:"key",key:entryKey}}; yield* nodeEvents(value);
      }
      yield {kind:"event",event:{type:"endObject"}};
      return [name, { k: "o", v: members }];
    }
    const header = /^(.*?)\[([1-9]\d*)([|\t]?)\](?:\{(.*)\})?$/.exec(h);
    if (header) {
      const name = header[1] ? unquote(header[1]) : undefined,
        n = Number(header[2]);
      if (header[3] !== options.marker || !Number.isSafeInteger(n) || n > VALUES)
        throw new SyntaxError("Invalid or unsupported TOON subset input");
      if (name !== undefined) yield {kind:"event",event:{type:"key",key:name}};
      yield {kind:"event",event:{type:"startArray",length:n}};
      if (header[4] !== undefined) {
        if (tail !== "")
          throw new SyntaxError("Invalid or unsupported TOON subset input");
        const fields = split(header[4], options.delimiter).map(unquote);
        if (!fields.length || new Set(fields).size !== fields.length)
          throw new SyntaxError("Invalid or unsupported TOON subset input");
        const rows: Node[] = [];
        for (let r = 0; r < n; r++) {
          if (++count > VALUES)
            throw new SyntaxError("Invalid or unsupported TOON subset input");
          const row = yield* lineAt(depth + 1);
          if (row === undefined)
            throw new SyntaxError("Invalid or unsupported TOON subset input");
          yield { kind: "consume" };
          const cells = split(row, options.delimiter);
          if (cells.length !== fields.length)
            throw new SyntaxError("Invalid or unsupported TOON subset input");
          const value: Node = { k: "o", v: fields.map((f, j) => [f, atom(cells[j]!)]) };
          rows.push(value); yield* nodeEvents(value);
        }
        yield {kind:"event",event:{type:"endArray"}};
        return [name, { k: "l", v: rows }];
      }
      const cells = split(body, options.delimiter);
      if (!tail || cells.length !== n)
        throw new SyntaxError("Invalid or unsupported TOON subset input");
      const values=cells.map(atom);
      for (const value of values) yield* nodeEvents(value);
      yield {kind:"event",event:{type:"endArray"}};
      return [name, { k: "l", v: values }];
    }
    const name = h ? unquote(h) : undefined;
    if (name !== undefined) yield {kind:"event",event:{type:"key",key:name}};
    if (body === "[]") { const value: Node = { k:"l",v:[] }; yield* nodeEvents(value); return [name,value]; }
    if (tail) { const value=atom(body); yield* nodeEvents(value); return [name,value]; }
    yield {kind:"event",event:{type:"startObject"}};
    const members: [string, Node][] = [],
      seen = new Set<string>();
    while ((yield* lineAt(depth + 1)) !== undefined) {
      const [k, v] = yield* entry(depth + 1, false);
      if (k === undefined || seen.has(k))
        throw new SyntaxError("Invalid or unsupported TOON subset input");
      seen.add(k);
      members.push([k, v]);
    }
    yield {kind:"event",event:{type:"endObject"}};
    return [name, { k: "o", v: members }];
  };
  const first = yield {kind:"peek"};
  if (first === undefined || first === "") {
    if (first === "") { yield {kind:"consume"}; if ((yield {kind:"peek"}) !== undefined) throw new SyntaxError("Invalid or unsupported TOON subset input"); }
    const empty: Node = {k:"o",v:[]}; yield* nodeEvents(empty); return empty;
  }
  if (first === "[]") {
    yield {kind:"consume"};
    if ((yield {kind:"peek"}) !== undefined) throw new SyntaxError("Invalid or unsupported TOON subset input");
    const empty: Node = {k:"l",v:[]}; yield* nodeEvents(empty); return empty;
  }
  if (first.startsWith("[")) {
    const [,v] = yield* entry(0,true);
    if ((yield {kind:"peek"}) !== undefined) throw new SyntaxError("Invalid or unsupported TOON subset input");
    return v;
  }
  if (first.startsWith('"') || !first.includes(":")) {
    let inQuote=false, escaped=false, separator=false;
    for (const c of first) {
      if (inQuote) { if (escaped) escaped=false; else if (c === "\\") escaped=true; else if (c === '"') inQuote=false; }
      else if (c === '"') inQuote=true; else if (c === ":") {separator=true; break;}
    }
    if (!separator) {
      const v=atom(first); yield {kind:"consume"};
      if ((yield {kind:"peek"}) !== undefined) throw new SyntaxError("Invalid or unsupported TOON subset input");
      yield* nodeEvents(v); return v;
    }
  }
  const members: [string,Node][] = [], seen = new Set<string>();
  yield {kind:"event",event:{type:"startObject"}};
  while ((yield {kind:"peek"}) !== undefined) {
    const [k,v] = yield* entry(0,false);
    if (k === undefined || seen.has(k)) throw new SyntaxError("Invalid or unsupported TOON subset input");
    seen.add(k); members.push([k,v]);
  }
  yield {kind:"event",event:{type:"endObject"}};
  return {k:"o",v:members};
}
function parseToonInput(text: string, options: ResolvedToonOptions): Node {
  if (typeof text !== "string" || utf8Bytes(text)>LIMIT || text.includes("\r") || text.endsWith("\n")) throw new SyntaxError("Invalid or unsupported TOON subset input");
  validateUnicodeScalar(text);
  const lines = text === "" ? [] : text.split("\n"); let pos=0;
  const parser=parseToonMachine(options); let step=parser.next();
  while (!step.done) {
    if (step.value.kind === "peek") step=parser.next(lines[pos]);
    else if (step.value.kind === "consume") {pos++; step=parser.next();}
    else step=parser.next();
  }
  return step.value;
}
/** Demand-driven newline-free line input. Data events are provisional; only complete commits. */
export async function* decodeToonEvents(source: AsyncIterable<string>, options?: ToonOptions, signal?: AbortSignal): AsyncGenerator<ToonStreamEvent> {
  const resolved=resolveOptions(options), iterator=source[Symbol.asyncIterator](), parser=parseToonMachine(resolved);
  let lookahead: string | undefined, loaded=false, eof=false, closed=false, size=0;
  const lines:string[]=[];
  const cancelled=()=>{if(signal?.aborted) throw new Error("Cancelled");};
  const read = async (): Promise<IteratorResult<string>> => {
    cancelled();
    if (!signal) return await iterator.next();
    let listener: (()=>void) | undefined;
    const abort = new Promise<never>((_,reject)=>{listener=()=>reject(new Error("Cancelled")); signal.addEventListener("abort",listener,{once:true});});
    try { cancelled(); return await Promise.race([iterator.next(),abort]); }
    finally {if(listener) signal.removeEventListener("abort",listener);}
  };
  try {
    let step=parser.next();
    while (!step.done) {
      cancelled();
      if (step.value.kind === "peek") {
        if (!loaded && !eof) {
          const item=await read(); cancelled();
          if (item.done) eof=true;
          else {
            if (typeof item.value !== "string" || item.value.includes("\n") || item.value.includes("\r")) throw new SyntaxError("Invalid or unsupported TOON subset input");
            validateUnicodeScalar(item.value); size+=utf8Bytes(item.value)+(lines.length?1:0);
            if(size>LIMIT) throw new SyntaxError("Invalid or unsupported TOON subset input");
            lines.push(item.value); lookahead=item.value; loaded=true;
          }
        }
        step=parser.next(loaded?lookahead:undefined);
      } else if(step.value.kind === "consume") { if(!loaded) throw new SyntaxError("Invalid or unsupported TOON subset input"); loaded=false; lookahead=undefined; step=parser.next(); }
      else {cancelled(); yield step.value.event; cancelled(); step=parser.next();}
    }
    cancelled();
    const text=lines.join("\n"), canonical=render(step.value,resolved), value=json(step.value);
    if (text.endsWith("\n") || canonical !== text || utf8Bytes(value)>LIMIT) throw new SyntaxError("Invalid or unsupported TOON subset input");
    if (typeof iterator.return === "function") {
      closed=true;
      const closing=Promise.resolve(iterator.return());
      if (signal) {
        let listener: (()=>void) | undefined;
        const aborted=new Promise<never>((_,reject)=>{listener=()=>reject(new Error("Cancelled")); signal.addEventListener("abort",listener,{once:true});});
        try { cancelled(); await Promise.race([closing,aborted]); }
        finally { if(listener) signal.removeEventListener("abort",listener); void closing.catch(()=>{}); }
      } else await closing;
    }
    cancelled(); yield {type:"complete",json:value};
  } finally {
    if(!closed && typeof iterator.return === "function") {
      closed=true; const result=iterator.return();
      if(signal?.aborted) void Promise.resolve(result).catch(()=>{}); else await result;
    }
  }
}
export type ToonProjectionOperation =
  | { op: "omit"; path: (string | number)[] }
  | { op: "replace"; path: (string | number)[]; value: null | boolean | number | string };
export type ToonProjectionSummary = {
  requested: number; applied: number; omitted: number; replaced: number; changed: number;
  lossy: boolean; sourceUnchanged: true;
  operations: { op: "omit" | "replace"; path: (string | number)[] }[];
};

type ProjectionTarget = { parent: Node; key: string | number; operation: ToonProjectionOperation; changed: boolean };

/** Validate every closed operation against the original ordered tree before mutation. */
function projectNode(root: Node, projection: ToonProjectionOperation[]): ToonProjectionSummary {
  if (!Array.isArray(projection) || projection.length > 16 || Object.keys(projection).length !== projection.length)
    throw new SyntaxError("Invalid TOON projection");
  const targets: ProjectionTarget[] = [];
  const paths: (string | number)[][] = [];
  let pathBytes = 0;
  for (const operation of projection) {
    if (!operation || typeof operation !== "object" || Array.isArray(operation)) throw new SyntaxError("Invalid TOON projection");
    const keys = Reflect.ownKeys(operation).sort().join(",");
    if ((operation.op === "omit" && keys !== "op,path") || (operation.op === "replace" && keys !== "op,path,value") ||
        (operation.op !== "omit" && operation.op !== "replace") || !Array.isArray(operation.path) ||
        operation.path.length < 1 || operation.path.length > DEPTH || Object.keys(operation.path).length !== operation.path.length)
      throw new SyntaxError("Invalid TOON projection");
    const path = operation.path.slice();
    for (const segment of path) {
      if (typeof segment === "string") {
        validateUnicodeScalar(segment);
        const size = utf8Bytes(segment);
        if (size > 256 || (pathBytes += size) > 8192) throw new SyntaxError("Invalid TOON projection");
      } else if (typeof segment !== "number" || !Number.isSafeInteger(segment) || segment < 0 || Object.is(segment, -0)) {
        throw new SyntaxError("Invalid TOON projection");
      }
    }
    if (paths.some(previous => {
      const common = Math.min(previous.length, path.length);
      return previous.slice(0, common).every((segment, i) => segment === path[i]);
    })) throw new SyntaxError("Overlapping TOON projection");
    paths.push(path);
    let parent = root;
    for (let i = 0; i < path.length - 1; i++) {
      const segment = path[i]!;
      const next = parent.k === "o" && typeof segment === "string" ? parent.v.find(([key]) => key === segment)?.[1]
        : parent.k === "l" && typeof segment === "number" ? parent.v[segment] : undefined;
      if (!next) throw new SyntaxError("TOON projection target unavailable");
      parent = next;
    }
    const key = path[path.length - 1]!;
    const existing = parent.k === "o" && typeof key === "string" ? parent.v.find(([name]) => name === key)?.[1]
      : parent.k === "l" && typeof key === "number" ? parent.v[key] : undefined;
    if (!existing || (operation.op === "omit" && (parent.k !== "o" || typeof key !== "string")))
      throw new SyntaxError("TOON projection target unavailable");
    let changed = true;
    if (operation.op === "replace") {
      const value = operation.value;
      if (typeof value === "string") {
        validateUnicodeScalar(value);
        if (utf8Bytes(value) > LIMIT) throw new SyntaxError("Invalid TOON projection value");
      }
      else if (typeof value === "number") {
        if (!Number.isSafeInteger(value) || Object.is(value, -0)) throw new SyntaxError("Invalid TOON projection value");
      } else if (value !== null && typeof value !== "boolean") throw new SyntaxError("Invalid TOON projection value");
      changed = existing.k !== "a" || existing.v !== value;
    }
    targets.push({ parent, key, operation: { ...operation, path }, changed });
  }
  let omitted = 0, replaced = 0, changed = 0;
  for (const target of targets) {
    const { parent, key, operation } = target;
    if (parent.k === "o" && typeof key === "string") {
      const index = parent.v.findIndex(([name]) => name === key);
      if (operation.op === "omit") { parent.v.splice(index, 1); omitted++; }
      else { parent.v[index]![1] = { k: "a", v: operation.value }; replaced++; }
    } else if (parent.k === "l" && typeof key === "number" && operation.op === "replace") {
      parent.v[key] = { k: "a", v: operation.value }; replaced++;
    } else throw new SyntaxError("TOON projection target unavailable");
    if (target.changed) changed++;
  }
  return { requested: targets.length, applied: targets.length, omitted, replaced, changed,
    lossy: changed > 0, sourceUnchanged: true,
    operations: targets.map(({operation}) => ({ op: operation.op, path: operation.path.slice() })) };
}
/** Newline-free provisional lines from materialized, bounded JSON. Complete iteration validates the canonical round trip. */
export function* encodeToonLines(jsonText: string, options?: ToonOptions, projection?: ToonProjectionOperation[]): Generator<string> {
  const resolved = resolveOptions(options), n = parseJsonInput(jsonText);
  if (projection !== undefined) projectNode(n, projection);
  const canonicalJson = json(n), checked = projection === undefined ? n : parseJsonInput(canonicalJson);
  const lines: string[] = [];
  let size = 0;
  for (const line of renderLines(checked, resolved)) {
    size += utf8Bytes(line) + (lines.length ? 1 : 0);
    if (size > LIMIT) throw new SyntaxError("Invalid or unsupported TOON subset input");
    lines.push(line);
    yield line;
  }
  if (json(parseToonInput(lines.join("\n"), resolved)) !== canonicalJson)
    throw new SyntaxError("Invalid or unsupported TOON subset input");
}
export function encodeToonSubset(
  jsonText: string,
  options?: ToonOptions,
  projection?: ToonProjectionOperation[],
): { text: string; canonicalJson: string; projection?: ToonProjectionSummary } | undefined {
  try {
    const resolved = resolveOptions(options),
      n = parseJsonInput(jsonText),
      summary = projection === undefined ? undefined : projectNode(n, projection),
      canonicalJson = json(n),
      checked = summary === undefined ? n : parseJsonInput(canonicalJson),
      text = render(checked, resolved),
      round = parseToonInput(text, resolved);
    if (json(round) !== canonicalJson) return;
    return summary === undefined ? { text, canonicalJson } : { text, canonicalJson, projection: summary };
  } catch {
    return;
  }
}
export function decodeToonSubset(
  toonText: string,
  options?: ToonOptions,
): { json: string; canonicalToon: string } | undefined {
  try {
    const resolved = resolveOptions(options),
      n = parseToonInput(toonText, resolved),
      canonicalToon = render(n, resolved),
      output = json(n);
    if (canonicalToon !== toonText || utf8Bytes(output) > LIMIT) return;
    return { json: output, canonicalToon };
  } catch {
    return;
  }
}
