// highlighter.js — the app's single syntax highlighter.
//
// Backed by the vendored Shiki bundle (static/lib/shiki.bundle.js, RegExp
// engine — no WASM, no CSP change) using a theme whose colors are the app's
// own --hl-* CSS variables, so user themes keep driving code colors with zero
// repaint on switch.
//
// All highlighting is queue-until-ready: callers never check whether the
// engine has loaded — work submitted early is painted the moment the bundle
// finishes initializing. After ready, highlighting is synchronous, so callers
// can paint detached fragments BEFORE inserting them into the live DOM (no
// unpainted/white code is ever shown).
//
// Replaces the retired global highlight.js API (element/auto highlighting).
(function () {
  const queue = new Set();
  const painterQueue = new Set();
  let engine = null; // resolved shiki highlighter
  let failed = false;

  // highlight.js-isms → shiki grammar ids. Shiki's own registrations already
  // cover the common aliases (js, sh, py, ts, yml, c++, ...); this table only
  // carries what they don't.
  const LANG_ALIASES = {
    docker: 'dockerfile',
    mk: 'makefile',
    'obj-c': 'objective-c',
    plaintext: '',
    text: '',
    none: '',
    mmd: 'mermaid',
  };

  const LANG_RE = /(?:^|\s)language-([A-Za-z0-9_+#-]+)/;

  function langOf(el) {
    const raw =
      (el.dataset && (el.dataset.lang || el.dataset.language)) ||
      (el.className && (el.className.match(LANG_RE) || [])[1]) ||
      '';
    const key = String(raw).toLowerCase();
    if (!key) return '';
    if (Object.prototype.hasOwnProperty.call(LANG_ALIASES, key)) {
      return LANG_ALIASES[key];
    }
    return key;
  }

  // Highlighted inner-HTML for `code`, or null when the language is unknown /
  // unset (caller leaves the block plain). Runs only after ready.
  function codeHtml(code, lang) {
    if (!engine || !lang) return null;
    try {
      const html = engine.codeToHtml(code, { lang, theme: 'open-clank' });
      const match = html.match(/<code[^>]*>([\s\S]*)<\/code>/);
      return match ? match[1] : null;
    } catch (_) {
      return null; // unknown grammar — plain text fallback
    }
  }

  function paint(el) {
    if (!engine || !el || el.dataset.hlDone === '1') return;
    const lang = langOf(el);
    if (!lang) return;
    const html = codeHtml(el.textContent, lang);
    if (html === null) { el.dataset.hlFailed = '1'; return; }
    el.innerHTML = html;
    el.dataset.hlDone = '1'; delete el.dataset.hlFailed;
    // The .hljs class keeps the historical block styling (background, padding)
    // that was applied by highlight.js itself; span colors come from Shiki.
    el.classList.add('hljs');
  }

  const workQueue = new Set();
  let workFrame = null;
  function schedulePaint(run) {
    workQueue.add(run);
    if (workFrame !== null) return;
    const drain = () => {
      workFrame = null;
      const run = workQueue.values().next().value;
      if (run) { workQueue.delete(run); run(); }
      if (workQueue.size) workFrame = requestAnimationFrame(drain);
    };
    workFrame = requestAnimationFrame(drain);
  }
  function flush() {
    const elements = [...queue]; queue.clear();
    elements.forEach(el => schedulePaint(() => paint(el)));
    const painters = [...painterQueue]; painterQueue.clear();
    painters.forEach(schedulePaint);
  }

  function highlight(el) {
    if (!el) return;
    if (engine) paint(el);
    else if (!failed) queue.add(el);
  }

  function highlightAll(root) {
    if (!root) return;
    if (!engine) {
      if (!failed) root.querySelectorAll('pre code:not([data-hl-done])').forEach(el => queue.add(el));
      return;
    }
    root.querySelectorAll('pre code:not([data-hl-done])').forEach(paint);
  }

  // Debounced painter for the streaming open-fence path: hands back a stable
  // paint(codeEl, sourceText) that never leaves a long unpainted interval and
  // never paints more often than `interval` ms.
  function createStreamingPainter(interval = 100) {
    let timer = null;
    let pendingArgs = null;
    const run = () => {
      timer = null;
      const args = pendingArgs;
      pendingArgs = null;
      if (!args) return;
      const [codeEl, source] = args;
      if (!codeEl || !codeEl.isConnected) return;
      if (!engine) {
        if (!codeEl.dataset.hlDone) codeEl.textContent = source;
        return;
      }
      const html = codeHtml(source, langOf(codeEl));
      if (html !== null) {
        codeEl.innerHTML = html;
        codeEl.dataset.hlDone = '1';
        codeEl.classList.add('hljs');
      } else if (!codeEl.dataset.hlDone) {
        codeEl.textContent = source;
      }
    };
    return {
      paint(codeEl, source) {
        pendingArgs = [codeEl, source];
        if (!engine) {
          if (!failed) painterQueue.add(run);
          return;
        }
        if (timer === null) timer = setTimeout(run, interval);
      },
      flush() {
        if (timer !== null) clearTimeout(timer);
        run();
      },
    };
  }

  // Minimal language auto-detect for the document editor's dropdown suggestion
  // (replaces hljs.highlightAuto, which Shiki has no equivalent for). Weighted
  // tells on a small sample; returns '' when nothing is convincing.
  const DETECTORS = [
    ['mermaid', [/^\s*(flowchart|graph|sequenceDiagram|classDiagram|stateDiagram|erDiagram|gantt|pie|mindmap|timeline|gitGraph)\b/im, /(?:-->|->>|==>)/], 1],
    ['python', [/^\s*(def|class)\s+\w+/m, /^\s*(import|from)\s+\w+/m, /:\s*$/m], 2],
    ['javascript', [/\b(const|let|var)\s+\w+\s*=/, /=>/, /\bconsole\.log\b/], 2],
    ['typescript', [/:\s*(string|number|boolean|any)\b/, /\binterface\s+\w+/], 2],
    ['bash', [/^#!.*\b(bash|sh|zsh)\b/m, /^\s*(echo|cd|export)\s/m], 2],
    ['html', [/<!doctype html/i, /<\/?(div|span|html|head|body)[\s>]/], 2],
    ['xml', [/<\?xml\s/, /<\/?\w+:[\w-]+[\s>]/], 2],
    ['css', [/^\s*[.#]?\w[\w-]*\s*\{[^}]*:/m], 1],
    ['json', [/^\s*[[{][\s\S]*["\w]\s*:?[\s\S]*[\]}]\s*$/], 1],
    ['c', [/#include\s*</, /\bprintf\s*\(/], 1],
    ['cpp', [/#include\s*<iostream>/, /\bstd::/], 1],
    ['java', [/\b(?:public\s+)?class\s+\w+/, /\bSystem\.out\.println\b/], 1],
    ['kotlin', [/\b(fun|val|var)\s+\w+/, /:\s*(String|Int|Boolean)\b/, /\bwhen\s*\(/], 1],
    ['swift', [/\b(import\s+Foundation|func\s+\w+|let\s+\w+)/, /\bguard\s+let\b/], 1],
    ['toml', [/^\s*\[[A-Za-z0-9_.-]+\]\s*$/m, /^\s*[A-Za-z0-9_.-]+\s*=\s*(?:"|true|false|\d)/m], 2],
    ['ini', [/^\s*\[[^\]]+\]\s*$/m, /^\s*[A-Za-z0-9_.-]+\s*=\s*\S+/m], 2],
    ['dockerfile', [/^\s*(FROM|RUN|COPY|ENTRYPOINT|CMD|WORKDIR)\b/im], 1],
    ['makefile', [/^\s*[A-Za-z0-9_.-]+\s*:\s*(?:\S.*)?$/m, /^\s*\$\(/m], 1],
    ['sql', [/\b(SELECT|INSERT|UPDATE|DELETE)\b.*\b(FROM|INTO|SET)\b/i], 1],
    ['go', [/^package\s+\w+/m, /\bfunc\s+\w+\(/], 2],
    ['rust', [/\bfn\s+\w+\(/, /\blet\s+mut\s+\w+/], 2],
    ['ruby', [/^\s*(def|end)\b/m, /\bputs\s+/], 2],
    ['php', [/<\?php/], 1],
    ['yaml', [/^\s*\w[\w-]*:\s+\S/m, /^---\s*$/m], 2],
    ['markdown', [/^#{1,6}\s+\S/m, /\[.+\]\(.+\)/], 2],
  ];

  function detect(sample) {
    const text = String(sample || '').slice(0, 4000);
    if (!text.trim()) return '';
    let best = '';
    let bestScore = 0;
    for (const [lang, patterns, needed] of DETECTORS) {
      let hits = 0;
      for (const re of patterns) if (re.test(text)) hits += 1;
      if (hits >= needed && hits > bestScore) {
        best = lang;
        bestScore = hits;
      }
    }
    return best;
  }

  function releasePlain() {
    const elements = [...queue];
    queue.clear();
    elements.forEach(el => { if (el && !el.dataset.hlDone) el.dataset.hlFailed = '1'; });
    const painters = [...painterQueue];
    painterQueue.clear();
    painters.forEach(run => run());
  }

  const ready = new Promise((resolve) => {
    const deadline = setTimeout(() => { failed = true; releasePlain(); resolve(false); }, 4000);
    const settle = value => { clearTimeout(deadline); resolve(value); };
    const api = window.__odysseusShiki;
    if (!api || !api.ready) {
      failed = true;
      releasePlain();
      console.warn('highlighter: shiki bundle missing; code renders plain');
      settle(false);
      return;
    }
    api.ready
      .then((h) => {
        engine = h;
        flush();
        failed = false; settle(true);
      })
      .catch((err) => {
        failed = true;
        releasePlain();
        console.warn('highlighter: shiki failed to initialize', err);
        settle(false);
      });
  });

  const visibleTasks = new WeakMap();
  const blockObserver = typeof IntersectionObserver === 'function' ? new IntersectionObserver(entries => {
    for (const entry of entries) if (entry.isIntersecting) {
      blockObserver.unobserve(entry.target);
      const run = visibleTasks.get(entry.target); visibleTasks.delete(entry.target);
      if (run) schedulePaint(run);
    }
  }) : null;

  // Rendered blocks only; never call this on a CodeMirror editable surface.
  function prepareElement(el, language) {
    if (!el) return Promise.resolve(false);
    el.dataset.lang = String(language || '');
    if (!langOf(el)) { el.dataset.syntaxReady = 'plain'; return Promise.resolve(false); }
    el.style.visibility = 'hidden'; el.setAttribute('aria-busy', 'true');
    el.dataset.syntaxReady = 'loading';
    const finish = highlighted => {
      el.style.visibility = ''; el.setAttribute('aria-busy', 'false');
      el.dataset.syntaxReady = highlighted ? 'ready' : 'plain';
      if (!highlighted) el.dataset.hlFailed = '1';
      return highlighted;
    };
    if (el.textContent.length > 131072) return Promise.resolve(finish(false));
    return new Promise(resolve => {
      let finished = false;
      const settle = highlighted => {
        if (finished) return; finished = true; clearTimeout(deadline);
        blockObserver?.unobserve(el); visibleTasks.delete(el); resolve(finish(highlighted));
      };
      // Includes queued/never-mounted work: no retained observer can conceal
      // or hold a discarded block forever. This is a failure bound, not a delay.
      const deadline = setTimeout(() => settle(false), 4000);
      (engine ? Promise.resolve(true) : ready).then(available => {
        if (finished) return;
        if (!available || failed) { settle(false); return; }
        const run = () => { if (finished) return; if (el.isConnected) paint(el); settle(el.dataset.hlDone === '1'); };
        if (blockObserver) { visibleTasks.set(el, run); blockObserver.observe(el); }
        else schedulePaint(run);
      }).catch(() => settle(false));
    });
  }

  window.odysseusHighlight = {
    highlight,
    highlightAll, prepareElement,
    createStreamingPainter,
    detect,
    codeHtml: (code, lang) => codeHtml(code, lang),
    ready,
  };
})();
