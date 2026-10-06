import { uiIcon } from '../uiIcons.js';
// compare/icons.js — SVG icons, prompt templates, and constants

// ── SVG Icons ──

export const EYE_OPEN = uiIcon("eye", 18);
export const EYE_CLOSED = uiIcon("eye-off", 18);
export const SAVE_ICON = uiIcon("save", 18);
export const CHAT_ICON = uiIcon("chat", 18);
export const ICON_COPY = uiIcon("copy", 14);
export const ICON_REROLL = uiIcon("refresh", 14);
export const ICON_EXPAND = uiIcon("expand", 14);
export const ICON_COLLAPSE = uiIcon("collapse", 14);
export const ICON_DICE = uiIcon("dice", 18);
export const ICON_PLAY = uiIcon("play", 14);
export const ICON_CODE = uiIcon("code", 14);
export const ICON_CLOSE = uiIcon("close", 14);
// Parallel = lines side by side, Sequential = numbered list
export const ICON_PARALLEL = uiIcon("menu", 18);
export const ICON_SEQUENTIAL = uiIcon("list", 18);
export const SEND_SVG = uiIcon("up", 16);

// ── Animation ──

export const WAVE_FRAMES = ['▁▂▃', '▂▃▄', '▃▄▅', '▄▅▆', '▅▆▇', '▆▅▄', '▅▄▃', '▄▃▂'];

// ── Storage keys & limits ──

export const VOTES_STORAGE_KEY = 'odysseus-compare-votes';
export const VOTES_MAX = 200;
export const POOL_STORAGE_KEY = 'odysseus-shuffle-pool-excluded';

// ── Evaluation prompt templates ──
//
// Five high-signal prompts per category — each picked to differentiate models
// on a distinct capability. The Visual / SVG-render prompt in `chat` ends with
// the subject as the last words, so swapping "a pelican riding a bicycle" for
// anything else is a one-line edit.

export const EVAL_PROMPTS = {
  chat: [
    // ── ★ Featured — prompts that have actually broken frontier models ──
    { sub: '★ Featured', label: 'Sum digits 2^100', answer: '115', prompt: 'Compute the sum of the decimal digits of 2^100. Do NOT use code execution — work it out by reasoning about the number. Show every step, then end with the final number on its own line.' },
    { sub: '★ Featured', label: 'Three jugs',       answer: '2 pours: 7→5, 7→3', prompt: 'You have three jugs of capacities 7, 5, and 3 liters. The 7-liter jug starts full; the others empty. Using only pouring (no markings), produce the shortest sequence of pours that leaves exactly 2 liters in the 3-liter jug. Output each step as `pour A → B` on its own line. Then state the total number of pours on a final line.' },

    { sub: 'Visual',         label: 'Draw SVG',         prompt: 'Output a complete self-contained HTML file (```html block, no explanation, no other text) that centers a single SVG illustration on a simple background. The SVG must use only inline shapes — no <img>, no external assets, no JavaScript. Make it expressive and detailed. The SVG should depict: a friendly robot' },
    { sub: 'Visual explain', label: 'Black hole HTML',  prompt: 'Output a complete HTML file (```html block, no explanation outside the code) that visually explains how a black hole forms. Use four labeled "frames" laid out left-to-right (or stacked on small screens) showing: 1) a glowing massive star, 2) the star going supernova with shockwave rings, 3) collapse into a singularity, 4) the final black hole with a curved accretion disk and bent light around it. Use only vanilla HTML, CSS, and inline SVG — no JavaScript, no images. Each frame should have a one-sentence caption.' },
    { sub: 'Visual explain', label: 'Butterfly ASCII',  prompt: 'Explain the butterfly lifecycle using ASCII art. Produce four separate frames in fenced code blocks, in order: egg, caterpillar, chrysalis, adult butterfly. Each frame must be drawn with monospace ASCII characters only and be visually recognizable as the creature/stage. Below each frame add one playful one-line caption (no longer than 15 words) describing what is happening at that stage.' },
  ],
  code: [
    { sub: 'Algorithms',   label: 'LRU cache',       prompt: 'Implement an LRU cache with O(1) get and put operations. Support a configurable max capacity. Write it in any language with full comments.' },
    { sub: 'Debugging',    label: 'Race condition',  prompt: 'This Go code has a race condition. Find it, explain why it happens, and fix it:\n\nvar counter int\nfunc increment(wg *sync.WaitGroup) {\n    defer wg.Done()\n    for i := 0; i < 1000; i++ {\n        counter++\n    }\n}' },
    { sub: 'Debugging',    label: 'Security review', prompt: 'Review this code for bugs, security issues, and performance problems:\n\napp.get("/user/:id", (req, res) => {\n  const query = `SELECT * FROM users WHERE id = ${req.params.id}`;\n  db.query(query, (err, result) => {\n    res.json(result[0]);\n  });\n});' },
    { sub: 'Architecture', label: 'URL shortener',   prompt: 'Design a URL shortener service. Cover the API, database schema, and how you would handle 1000 requests per second.' },
    { sub: 'Refactoring',  label: 'Clean up',        prompt: 'Refactor this code to be more idiomatic and efficient:\n\nresults = []\nfor i in range(len(data)):\n    if data[i]["status"] == "active":\n        if data[i]["score"] > 50:\n            results.append(data[i]["name"].upper())' },
  ],
  agent: [
    { sub: 'Web tasks',  label: 'Multi-step',     prompt: 'Search the web for the current population of the 3 largest cities in the world, then calculate what percentage of the world\'s total population lives in those cities.', toggles: ['web'] },
    { sub: 'Web tasks',  label: 'Fact check',     prompt: 'Fact-check these claims: 1) The Great Wall of China is visible from space. 2) Humans only use 10% of their brains. 3) Lightning never strikes the same place twice. Cite sources.', toggles: ['web'] },
    { sub: 'Web tasks',  label: 'Compare prices', prompt: 'Find and compare the pricing, features, and limitations of the top 3 cloud GPU providers for machine learning training. Create a markdown comparison table.', toggles: ['web'] },
    { sub: 'Code tasks', label: 'Script + run',   prompt: 'Write a Python script that generates a bar chart of the 5 most common programming languages in 2025 and save it as chart.png. Then run it.' },
    { sub: 'Math',       label: 'Proof + verify', prompt: 'Prove that the square root of 2 is irrational. Then write a Python program that approximates it using Newton\'s method to 50 decimal places and verify.' },
  ],
  html: [
    { sub: 'Games',      label: 'Snake',         prompt: 'Output a complete HTML file (```html block) for a Snake game. ONLY use vanilla HTML, CSS, and JavaScript — no libraries, no Python, no imports, no external files. Canvas-based, neon green snake on dark grid, glowing food, score counter, speed increases, game over + restart. Skip any explanation, just output the code.' },
    { sub: 'Games',      label: 'Breakout',      prompt: 'Output a complete HTML file (```html block) for a Breakout brick breaker game. ONLY use vanilla HTML, CSS, and JavaScript — no libraries, no Python, no imports, no external files. Canvas-based, colorful gradient brick rows, glowing paddle, ball with trail, score + lives, particle explosions on break. Skip any explanation, just output the code.' },
    { sub: 'Animation',  label: 'Solar system',  prompt: 'Output a complete HTML file (```html block) for an animated solar system. ONLY use vanilla HTML, CSS, and JavaScript — no libraries, no Python, no imports, no external files. Canvas-based, glowing Sun center, 8 planets orbiting at correct relative speeds with real colors, orbit trails, starfield background, labels on hover. Skip any explanation, just output the code.' },
    { sub: 'Animation',  label: 'Matrix rain',   prompt: 'Output a complete HTML file (```html block) for the Matrix digital rain effect. ONLY use vanilla HTML, CSS, and JavaScript — no libraries, no Python, no imports, no external files. Full-screen canvas, green katakana characters falling at varying speeds, glowing heads, fading trails, scan-line overlay. Skip any explanation, just output the code.' },
    { sub: 'Generative', label: 'Fractal tree',  prompt: 'Output a complete HTML file (```html block) for an interactive fractal tree. ONLY use vanilla HTML, CSS, and JavaScript — no libraries, no Python, no imports, no external files. Canvas-based, tree grows from bottom with recursive branches, sliders for angle/depth/length/wind, gradient colors from brown trunk to green leaves. Skip any explanation, just output the code.' },
  ],
  search: [
    { sub: 'Factual',    label: 'Current events', prompt: 'latest AI regulation news 2025' },
    { sub: 'Technical',  label: 'Programming',    prompt: 'Rust vs Go performance benchmarks 2025' },
    { sub: 'Research',   label: 'Academic',       prompt: 'transformer architecture improvements since attention is all you need' },
    { sub: 'Comparison', label: 'GPU providers',  prompt: 'cloud GPU providers pricing comparison 2025' },
    { sub: 'Factual',    label: 'Science',        prompt: 'CRISPR gene therapy breakthroughs' },
  ],
};
