"""Markdown display, math, links and reasoning extraction, using independent renderer state per case."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


# Case label, renderer operation, KaTeX availability, and source text.
_CASES = [
    (
        'ordered_lists_render_as_one_unwrapped_ol', 'mdToHtml', False,
        'Before\n'
        '\n'
        "1. **Check against the home page** — that's the visual reference for how things should feel.\n"
        '2. **Open DevTools** and inspect the element — check fonts, colors, and spacing against this guide.\n'
        "3. **Flag it** — note the page, the section, what's wrong, and what CSS rule you suspect.\n"
        '4. **Small fixes** — if you know the fix (e.g. wrong CSS variable, wrong font), go ahead and change it in the CSS Module file.\n'
        '5. **Big changes** — Talk it through before making wide changes across many pages.\n'
        '\n'
        'After'
    ),
    (
        'table_separator_row_not_rendered_as_data', 'mdToHtml', False,
        '| A | B |\n'
        '|---|---|\n'
        '| 1 | 2 |'
    ),
    (
        'process_with_thinking_handles_gemma4_thought_channel', 'processWithThinking', False,
        '<|channel>thought\n'
        'internal reasoning<channel|>Final answer.'
    ),
    (
        'process_with_thinking_strips_empty_gemma4_thought_channel', 'processWithThinking', False,
        '<|channel>thought\n'
        '<channel|>Final answer.'
    ),
    (
        'process_with_thinking_unwraps_gemma4_response_channel', 'processWithThinking', False,
        '<|channel>thought\n'
        'internal reasoning<channel|><|channel>response\n'
        'Final answer.<channel|>'
    ),
    (
        'extract_thinking_blocks_handles_thought_tag', 'extractThinkingBlocks', False,
        '<thought>internal reasoning</thought>Final answer.'
    ),
    (
        'url_inside_inline_code_is_not_autolinked', 'mdToHtml', False,
        'Run `$j = irm http://127.0.0.1:3000/x` to fetch.'
    ),
    (
        'url_outside_inline_code_is_still_autolinked', 'mdToHtml', False,
        'Use `irm` then visit https://example.com/page now.'
    ),
    (
        'inline_code_content_is_html_escaped', 'mdToHtml', False,
        "Render `<b>$1 & 'q'</b>` literally."
    ),
    (
        'currency_dollar_amounts_are_not_rendered_as_math', 'mdToHtml', True,
        'The price rose from $5 to $10 overnight.'
    ),
    (
        'inline_math_still_renders_through_katex', 'mdToHtml', True,
        'Pythagoras: $x^2 + y^2 = z^2$ holds.'
    ),
    (
        'display_math_still_renders_through_katex', 'mdToHtml', True,
        '$$\\frac{a}{b}$$'
    ),
    (
        'dotted_python_import_paths_are_not_autolinked', 'mdToHtml', False,
        'from imblearn.combine import SMOTETomek\n'
        'from sklearn.metrics import f1_score\n'
        'from sklearn.compose import ColumnTransformer\n'
        '\n'
        'See example.com/docs for normal domain autolinking.'
    ),
    (
        'issue_payload_copy_text_excludes_thinking', 'extractThinkingBlocks', False,
        '<think time="24.5">\n'
        "Here's a thinking process that leads to the desired summary:\n"
        '\n'
        '6.  **Generate the Output.** (This matches the final provided response.)</think>### Juxtaposition: Interweaving Cultural Norms in Lesson Design\n'
        'The most effective lesson structure is created by deliberately juxtaposing.'
    ),
    (
        'plain_reply_copy_text_is_unchanged', 'extractThinkingBlocks', False,
        '### Heading\n'
        'Just a normal reply with no reasoning markup.'
    ),
    (
        'minimax_namespaced_thinking_is_extracted', 'extractThinkingBlocks', False,
        '<mm:think>The user said "idk" - just casual.</mm:think>Haha fair. Well, I\'m here whenever you figure it out.'
    ),
    (
        'minimax_orphan_closing_tag_drops_leaked_reasoning', 'extractThinkingBlocks', False,
        '</mm:think>Hi! What can I do for you?'
    ),
    (
        'thinking_only_message_yields_empty_content', 'extractThinkingBlocks', False,
        '<think>only reasoning, no reply yet</think>'
    ),
]


@pytest.fixture(scope="module")
def rendered():
    if not shutil.which("node"):
        pytest.skip("node binary not on PATH")
    script = r"""
    import fs from 'node:fs';
    import { pathToFileURL } from 'node:url';

    let source = fs.readFileSync('./static/js/markdown.js', 'utf8');
    source = source.replace("import uiModule from './ui.js';", "");
    // Resolve the real pure helpers when loading the renderer via a data URL.
    source = source.replace(/from '(.+?)'/g, (_, specifier) =>
      "from '" + new URL(specifier, pathToFileURL(process.cwd() + '/static/js/markdown.js')).href + "'");
    source = source.replace(
      /var escapeHtml = uiModule\.esc;/,
      `var escapeHtml = (value) => String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');`
    );


    const moduleUrl = 'data:text/javascript;base64,' + Buffer.from(source).toString('base64');
    const results = {};
    for (const [name, operation, withKatex, input] of JSON.parse(process.argv[1])) {
      delete globalThis.katex;
      globalThis.window = { location: { origin: 'http://localhost' }, katex: null };
      if (withKatex) {
        // Minimal stand-in for the CDN katex global: wraps the source so tests
        // can assert what was (or wasn't) handed to KaTeX.
        const katexStub = {
          renderToString(src, opts) {
            const display = !!(opts && opts.displayMode);
            return `<span class="katex" data-display="${display}">${src}</span>`;
          },
        };
        globalThis.window.katex = katexStub;
        globalThis.katex = katexStub;
      }
      globalThis.document = {
        readyState: 'loading',
        addEventListener() {},
        createElement(tag) {
          if (tag !== 'template') throw new Error(`unsupported element: ${tag}`);
          return {
            _html: '',
            content: { querySelectorAll() { return []; } },
            set innerHTML(value) { this._html = value; },
            get innerHTML() { return this._html; },
          };
        },
      };
      globalThis.MutationObserver = class { observe() {} };

      try {
        const mod = await import(moduleUrl + '#' + name);
        results[name] = mod[operation](input);
      } catch (error) {
        throw new Error(name, { cause: error });
      }
    }
    console.log(JSON.stringify(results));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script, json.dumps(_CASES)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    results = json.loads(result.stdout.splitlines()[-1])
    return {name: (source, results[name]) for name, _, _, source in _CASES}


def test_ordered_lists_render_as_one_unwrapped_ol(rendered):
    _, html = rendered['ordered_lists_render_as_one_unwrapped_ol']
    assert html.count("<ol>") == 1
    assert html.count("</ol>") == 1
    assert html.count("<li>") == 5
    assert "<ul>" not in html
    assert "<oli>" not in html
    assert "<uli>" not in html
    assert "<p><ol>" not in html
    assert "<p><li>" not in html
    assert "<p>Before</p>" in html
    assert "<p>After</p>" in html


def test_table_separator_row_not_rendered_as_data(rendered):
    _, html = rendered['table_separator_row_not_rendered_as_data']
    assert html.count("<tr>") == 2
    assert "<th" in html
    assert "<td" in html
    assert "---" not in html


def test_process_with_thinking_handles_gemma4_thought_channel(rendered):
    _, html = rendered['process_with_thinking_handles_gemma4_thought_channel']
    assert "thinking-section" in html
    assert "internal reasoning" in html
    assert "Final answer." in html
    assert "&lt;|channel&gt;" not in html
    assert "<|channel>" not in html


def test_process_with_thinking_strips_empty_gemma4_thought_channel(rendered):
    _, html = rendered['process_with_thinking_strips_empty_gemma4_thought_channel']
    assert "thinking-section" not in html
    assert "Final answer." in html
    assert "&lt;|channel&gt;" not in html
    assert "<|channel>" not in html


def test_process_with_thinking_unwraps_gemma4_response_channel(rendered):
    _, html = rendered['process_with_thinking_unwraps_gemma4_response_channel']
    assert "thinking-section" in html
    assert "internal reasoning" in html
    assert "Final answer." in html
    assert "&lt;|channel&gt;" not in html
    assert "<|channel>" not in html


def test_extract_thinking_blocks_handles_thought_tag(rendered):
    _, result = rendered['extract_thinking_blocks_handles_thought_tag']
    assert result["thinkingBlocks"] == ["internal reasoning"]
    assert result["content"] == "Final answer."


def test_url_inside_inline_code_is_not_autolinked(rendered):
    _, html = rendered['url_inside_inline_code_is_not_autolinked']
    assert "<code>$j = irm http://127.0.0.1:3000/x</code>" in html
    assert "___ALLOWED_HTML_" not in html
    assert "<a " not in html
    assert 'href="http://127.0.0.1:3000/x"' not in html


def test_url_outside_inline_code_is_still_autolinked(rendered):
    _, html = rendered['url_outside_inline_code_is_still_autolinked']
    assert "<code>irm</code>" in html
    assert 'href="https://example.com/page"' in html


def test_inline_code_content_is_html_escaped(rendered):
    _, html = rendered['inline_code_content_is_html_escaped']
    assert "<code>&lt;b&gt;$1 &amp; &#39;q&#39;&lt;/b&gt;</code>" in html
    assert "<b>" not in html


def test_currency_dollar_amounts_are_not_rendered_as_math(rendered):
    _, html = rendered['currency_dollar_amounts_are_not_rendered_as_math']
    assert 'class="katex"' not in html
    assert "$5" in html
    assert "$10" in html


def test_inline_math_still_renders_through_katex(rendered):
    _, html = rendered['inline_math_still_renders_through_katex']
    assert '<span class="katex" data-display="false">x^2 + y^2 = z^2</span>' in html
    assert "$" not in html


def test_display_math_still_renders_through_katex(rendered):
    _, html = rendered['display_math_still_renders_through_katex']
    assert 'data-display="true"' in html
    assert "$$" not in html


def test_dotted_python_import_paths_are_not_autolinked(rendered):
    _, html = rendered['dotted_python_import_paths_are_not_autolinked']
    assert "___ALLOWED_HTML_" not in html
    assert "imblearn.combine" in html
    assert "sklearn.metrics" in html
    assert "sklearn.compose" in html
    assert 'href="https://imblearn.com' not in html
    assert 'href="https://sklearn.me' not in html
    assert 'href="https://example.com/docs"' in html


def test_issue_payload_copy_text_excludes_thinking(rendered):
    _, out = rendered['issue_payload_copy_text_excludes_thinking']
    assert out["content"].startswith("### Juxtaposition:"), out["content"]
    assert "thinking process" not in out["content"]
    assert "<think" not in out["content"]
    assert out["thinkingTime"] == "24.5"


def test_plain_reply_copy_text_is_unchanged(rendered):
    raw, out = rendered['plain_reply_copy_text_is_unchanged']
    assert out["content"] == raw


def test_minimax_namespaced_thinking_is_extracted(rendered):
    _, out = rendered['minimax_namespaced_thinking_is_extracted']
    assert out["thinkingBlocks"] == ['The user said "idk" - just casual.']
    assert out["content"] == "Haha fair. Well, I'm here whenever you figure it out."
    assert "mm:think" not in out["content"]


def test_minimax_orphan_closing_tag_drops_leaked_reasoning(rendered):
    _, out = rendered['minimax_orphan_closing_tag_drops_leaked_reasoning']
    assert out["thinkingBlocks"] == []
    assert out["content"] == "Hi! What can I do for you?"
    assert "mm:think" not in out["content"]


def test_thinking_only_message_yields_empty_content(rendered):
    _, out = rendered['thinking_only_message_yields_empty_content']
    assert out["content"] == ""
