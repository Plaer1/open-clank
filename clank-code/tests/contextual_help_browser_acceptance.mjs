#!/usr/bin/env node

import assert from 'node:assert/strict';
import test from 'node:test';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const page = `<!doctype html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/style.css"></head>
<body data-account-id="acct-a">
  <section id="copal-notes-modal" class="copal-view-window" aria-hidden="false">
    <header class="copal-workspace-header"><div class="copal-window-actions"></div></header>
    <div class="copal-view" data-resource-id="note-1" data-resource-ref="rr1.public-resource-token" aria-selected="true">Selected note</div>
  </section>
  <section class="chat-input-bar"><div class="chat-input-row"><input id="message" aria-label="Message"></div></section>
  <script type="module">
    window.__odysseusGetActiveCopalContext = () => ({ accountId:'acct-a', workspace:'workspace-a', view:'notes', resourceKind:'note', resourceId:'note-1' });
    window.__odysseusGetActiveFilesContext = () => ({ accountId:'acct-a', workspace:'files-a', view:'files' });
    window.__helpModule = await import('/static/js/contextualHelp.js?browser-acceptance=1');
    window.__helpReady = true;
  </script>
</body></html>`;

const allSurfacesPage = `<!doctype html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/style.css"></head>
<body data-account-id="acct-a">
  <section id="copal-notes-modal" class="copal-view-window" aria-hidden="false"><header class="copal-workspace-header"><div class="copal-window-actions"></div></header><div data-resource-id="editor-resource" data-resource-ref="ref.editor" aria-selected="true"></div></section>
  <section id="files-window" class="files-window" aria-hidden="false"><header class="copal-workspace-header"><div class="copal-window-actions"></div></header><div data-resource-id="files-resource" data-resource-ref="ref.files" aria-selected="true"></div></section>
  <section id="copal-timeline-modal" class="copal-view-window" aria-hidden="false"><header class="copal-workspace-header"><div class="copal-window-actions"></div></header><div data-resource-id="timeline-resource" data-resource-ref="ref.timeline" aria-selected="true"></div></section>
  <section id="copal-wiki-modal" class="copal-view-window" aria-hidden="false"><header class="copal-workspace-header"><div class="copal-window-actions"></div></header><div data-resource-id="wiki-resource" data-resource-ref="ref.wiki" aria-selected="true"></div></section>
  <section id="copal-graph-modal" class="copal-view-window" aria-hidden="false"><header class="copal-workspace-header"><div class="copal-window-actions"></div></header><div data-resource-id="graph-resource" data-resource-ref="ref.graph" aria-selected="true"></div></section>
  <section id="copal-todo-modal" class="copal-view-window" aria-hidden="false"><header class="copal-workspace-header"><div class="copal-window-actions"></div></header><div data-resource-id="tasks-resource" data-resource-ref="ref.tasks" aria-selected="true"></div></section>
  <section id="copal-assistant-modal" class="copal-view-window" aria-hidden="false"><header class="copal-workspace-header"><div class="copal-window-actions"></div></header><div data-resource-id="assistant-resource" data-resource-ref="ref.assistant" aria-selected="true"></div></section>
  <section id="copal-bases-modal" class="copal-view-window" aria-hidden="false"><header class="copal-workspace-header"><div class="copal-window-actions"></div></header><div data-resource-id="bases-resource" data-resource-ref="ref.bases" aria-selected="true"></div></section>
  <section id="copal-settings-modal" class="copal-view-window" aria-hidden="false"><header class="copal-workspace-header"><div class="copal-window-actions"></div></header><div data-resource-id="settings-resource" data-resource-ref="ref.settings" aria-selected="true"></div></section>
  <section id="copal-continuity-modal" class="copal-view-window" aria-hidden="false"><header class="copal-workspace-header"><div class="copal-window-actions"></div></header><div data-resource-id="continuity-resource" data-resource-ref="ref.continuity" aria-selected="true"></div></section>
  <section id="copal-teaching-modal" class="copal-view-window" aria-hidden="false"><header class="copal-workspace-header"><div class="copal-window-actions"></div></header><div data-resource-id="teaching-resource" data-resource-ref="ref.teaching" aria-selected="true"></div></section>
  <section id="copal-models-modal" class="copal-view-window" aria-hidden="false"><header class="copal-workspace-header"><div class="copal-window-actions"></div></header><div data-resource-id="models-resource" data-resource-ref="ref.models" aria-selected="true"></div></section>
  <section id="copal-automation-modal" class="copal-view-window" aria-hidden="false"><header class="copal-workspace-header"><div class="copal-window-actions"></div></header><div data-resource-id="automation-resource" data-resource-ref="ref.automation" aria-selected="true"></div></section>
  <section id="copal-research-modal" class="copal-view-window" aria-hidden="false"><header class="copal-workspace-header"><div class="copal-window-actions"></div></header><div data-resource-id="research-resource" data-resource-ref="ref.research" aria-selected="true"></div></section>
  <section id="copal-communications-modal" class="copal-view-window" aria-hidden="false"><header class="copal-workspace-header"><div class="copal-window-actions"></div></header><div data-resource-id="communications-resource" data-resource-ref="ref.communications" aria-selected="true"></div></section>
  <section id="copal-operations-modal" class="copal-view-window" aria-hidden="false"><header class="copal-workspace-header"><div class="copal-window-actions"></div></header><div data-resource-id="operations-resource" data-resource-ref="ref.operations" aria-selected="true"></div></section>
  <section id="copal-treehouse-modal" class="copal-view-window" aria-hidden="false"><header class="copal-workspace-header"><div class="copal-window-actions"></div></header><div data-resource-id="treehouse-resource" data-resource-ref="ref.treehouse" aria-selected="true"></div></section>
  <section class="chat-input-bar"><div class="chat-input-row"><input id="message" aria-label="Message"></div></section>
  <script type="module">
    const kinds = { editor:'note', files:'file', timeline:'event', wiki:'wiki', graph:'note', tasks:'task', assistant:'assistant-context', bases:'base', settings:'setting', continuity:'checkpoint', teaching:'treehouse-lesson', models:'model', automation:'automation', research:'research-source', communications:'communication-draft', operations:'diagnostic', treehouse:'treehouse-lesson' };
    window.__odysseusGetActiveCopalHelpContext = (surface = 'editor') => ({ accountId:'acct-a', workspace:'workspace-a', view:surface === 'editor' ? 'notes' : surface, resourceKind:kinds[surface] || 'note', resourceId:surface + '-resource', resourceRef:'ref.' + surface, selection:surface + '-selection' });
    window.__odysseusGetActiveFilesContext = () => ({ accountId:'acct-a', workspace:'workspace-a', view:'files', resourceKind:'file', resourceId:'files-resource', resourceRef:'ref.files', selection:'files-selection' });
    window.__helpModule = await import('/static/js/contextualHelp.js?all-surfaces=1');
    window.__helpReady = true;
  </script>
</body></html>`;

test('real browser covers help focus, pin replacement, one-shot chip, and scope revocation', async () => {
  await withCopalBrowser({ page }, async ({ evaluate, until }) => {
    await until('window.__helpReady && document.querySelector("[data-contextual-help-button]")');
    const helpButton = '[data-contextual-help-button]';

    await evaluate(`document.querySelector(${JSON.stringify(helpButton)}).focus(); document.querySelector(${JSON.stringify(helpButton)}).click()`);
    await until('document.querySelector("dialog.openclank-contextual-help[open]")');
    assert.equal(await evaluate('document.activeElement.matches("[data-help-close]")'), true);
    assert.equal(await evaluate('document.querySelector("[data-help-resource]").textContent'), 'note-1');

    // Pin reopens the dialog while its old instance is closing. The original
    // surface button remains the return target and only one dialog survives.
    await evaluate('document.querySelector("dialog[open] [data-help-pin]").click()');
    await until('document.querySelectorAll("dialog.openclank-contextual-help[open]").length === 1');
    await evaluate('document.querySelector("dialog[open]").dispatchEvent(new Event("cancel", { cancelable:true }))');
    await until('!document.querySelector("dialog.openclank-contextual-help")');
    assert.equal(await evaluate('document.activeElement.matches("[data-contextual-help-button]")'), true);

    await evaluate(`document.querySelector(${JSON.stringify(helpButton)}).focus(); document.querySelector(${JSON.stringify(helpButton)}).click()`);
    await until('document.querySelector("dialog.openclank-contextual-help[open]")');
    await evaluate('document.querySelector("dialog[open] [data-help-ask]").click()');
    await until('document.querySelector("[data-copal-help-attachment]")');
    const attached = await evaluate(`(() => {
      const chip = document.querySelector('[data-copal-help-attachment]');
      return { active: document.activeElement.id, context: chip.__openClankHelpContext,
        bodyLeak: chip.textContent.includes('Selected note') || JSON.stringify(chip.__openClankHelpContext).includes('/Users/') };
    })()`);
    assert.equal(attached.active, 'message');
    assert.equal(attached.context.resourceId, 'note-1');
    assert.equal(attached.context.resourceRef, 'rr1.public-resource-token');
    assert.equal(attached.bodyLeak, false);

    // A Copal flush can reject before the request is accepted. Releasing the
    // claim in that path leaves the chip retryable and does not duplicate it.
    const flushFailure = await evaluate(`(async () => {
      const claim = window.__helpModule.claimAssistantContext();
      window.__odysseusFlushActiveCopalResource = async () => { throw new Error('flush rejected'); };
      let message = '';
      try { await window.__odysseusFlushActiveCopalResource(); } catch (error) { message = error.message; window.__helpModule.restoreAssistantContext(claim); }
      const retryClaim = window.__helpModule.claimAssistantContext();
      const retryReleased = window.__helpModule.restoreAssistantContext(retryClaim);
      return { message, pending:!!window.__helpModule.peekAssistantContext(), claimToken:claim?.token || null, claimAgain:!!retryClaim, retryReleased:!!retryReleased };
    })()`);
    assert.equal(flushFailure.message, 'flush rejected');
    assert.equal(flushFailure.pending, true);
    assert.equal(flushFailure.claimAgain, true);
    assert.match(flushFailure.claimToken || '', /^help-claim-/);
    assert.equal(flushFailure.retryReleased, true);

    // Removing a chip while its request is in flight is a revocation. A
    // failed request must not resurrect the explicitly removed attachment.
    const claimResult = await evaluate(`(() => {
      const claim = window.__helpModule.claimAssistantContext();
      document.querySelector('[data-copal-help-attachment] .openclank-assistant-context-remove').click();
      return { token:claim?.token || null, restored:window.__helpModule.restoreAssistantContext(claim) };
    })()`);
    assert.match(claimResult.token || '', /^help-claim-/);
    assert.equal(claimResult.restored, null);
    assert.equal(await evaluate('document.querySelector("[data-copal-help-attachment]")'), null);

    // Stage a fresh attachment for the scope-change check below.
    await evaluate(`window.__helpModule.stageAssistantContext({ surface:'editor', view:'notes', workspace:'workspace-a', accountId:'acct-a', resourceId:'note-1' })`);

    // Provider revocation clears the chip even when the account/workspace
    // generation is unchanged.
    await evaluate(`window.dispatchEvent(new CustomEvent('openclank:resource-revoked'));`);
    await until('!document.querySelector("[data-copal-help-attachment]")', 'revoked help attachment');
    assert.equal(await evaluate('window.__openClankConsumeAssistantContext?.() || null'), null);

    await evaluate(`window.__helpModule.stageAssistantContext({ surface:'editor', view:'notes', workspace:'workspace-a', accountId:'acct-a', resourceId:'note-1' })`);

    // An account/workspace generation change revokes the visible chip before
    // it can be consumed by Chat.
    await evaluate(`window.__odysseusGetActiveCopalContext = () => ({ accountId:'acct-a', workspace:'workspace-b', view:'notes', resourceKind:'note', resourceId:'note-1' }); window.dispatchEvent(new CustomEvent('workspace-change'))`);
    await until('!document.querySelector("[data-copal-help-attachment]")');
    assert.equal(await evaluate('window.__openClankConsumeAssistantContext?.() || null'), null);
  });
});

test('real browser keeps contextual help usable in narrow reduced-motion layout', async () => {
  await withCopalBrowser({ page }, async ({ cdp, evaluate, until }) => {
    await cdp('Emulation.setDeviceMetricsOverride', { width:390, height:844, deviceScaleFactor:1, mobile:true });
    await cdp('Emulation.setEmulatedMedia', { features:[{ name:'prefers-reduced-motion', value:'reduce' }] });
    await until('window.__helpReady && document.querySelector("[data-contextual-help-button]")');
    await evaluate('document.querySelector("[data-contextual-help-button]").click()');
    await until('document.querySelector("dialog.openclank-contextual-help[open]")');
    const layout = await evaluate(`(() => {
      const dialog = document.querySelector('dialog.openclank-contextual-help[open]');
      const rect = dialog.getBoundingClientRect();
      const style = getComputedStyle(dialog);
      return { width:rect.width, right:rect.right, viewport:innerWidth, animation:style.animationName,
        closeVisible:!!dialog.querySelector('[data-help-close]')?.offsetParent };
    })()`);
    assert(layout.width > 0 && layout.right <= layout.viewport + 1, JSON.stringify(layout));
    assert.equal(layout.animation, 'none');
    assert.equal(layout.closeVisible, true);
  });
});

test('shared help reaches every Copal surface with active resource and lesson destination', async () => {
  await withCopalBrowser({ page: allSurfacesPage }, async ({ evaluate, until }) => {
    const surfaces = ['editor', 'files', 'timeline', 'wiki', 'graph', 'tasks', 'assistant', 'bases', 'settings', 'continuity', 'teaching', 'models', 'automation', 'research', 'communications', 'operations', 'treehouse'];
    await until('window.__helpReady && document.querySelectorAll("[data-contextual-help-button]").length === 17', 'all contextual help buttons', 30000);
    for (const surface of surfaces) {
      await evaluate(`document.querySelector('[data-contextual-help-button][data-help-surface="${surface}"]').click()`);
      await until('document.querySelector("dialog.openclank-contextual-help[open]")', `${surface} help dialog`, 30000);
      const details = await evaluate(`(() => { const dialog=document.querySelector('dialog.openclank-contextual-help[open]'); const lesson=dialog.querySelector('[data-help-lesson]'); return { resource:dialog.querySelector('[data-help-resource]').textContent, href:lesson.href, labelled:dialog.getAttribute('aria-labelledby'), described:dialog.getAttribute('aria-describedby'), close:!!dialog.querySelector('[data-help-close]') }; })()`);
      assert.equal(details.resource, `${surface}-resource`);
      assert.match(details.href, /\/copal\/treehouse\?lesson=fg-/);
      assert.ok(details.labelled && details.described && details.close, `${surface} dialog is not labelled/dismissible`);
      await evaluate('document.querySelector("dialog[open] [data-help-ask]").click()');
      await until('document.querySelector("[data-copal-help-attachment]")', `${surface} assistant attachment`, 30000);
      assert.equal(await evaluate('document.querySelector("[data-copal-help-attachment]").__openClankHelpContext.resourceId'), `${surface}-resource`);
      await evaluate('document.querySelector("[data-copal-help-attachment] .openclank-assistant-context-remove").click()');
      await until('!document.querySelector("dialog.openclank-contextual-help")', `${surface} help close`, 30000);
    }

    // On a standard US layout, the primary `?` key includes Shift.
    await evaluate('const button=document.querySelector("[data-contextual-help-button][data-help-surface=editor]"); button.focus(); document.dispatchEvent(new KeyboardEvent("keydown", {key:"?", shiftKey:true, bubbles:true, cancelable:true}))');
    await until('document.querySelector("dialog.openclank-contextual-help[open]")', 'keyboard help dialog', 30000);
    assert.equal(await evaluate('document.querySelector("dialog[open]").matches("[aria-labelledby][aria-describedby]")'), true);
    await evaluate('document.querySelector("dialog[open] [data-help-close]").click()');
    await until('!document.querySelector("dialog.openclank-contextual-help")', 'keyboard help close', 30000);
    assert.equal(await evaluate('document.activeElement.matches("[data-contextual-help-button][data-help-surface=editor]")'), true);
  });
});
