/** Render actions only from a completed Host grep result with sealed Files references. */
export function searchHitButtons(event, { live = false } = {}) {
  if (event?.exit_code !== 0 || (live && (event.status !== 'completed' || !event.id))) return null;
  const call = live ? event.data : event.structured;
  if (event.id && call?.toolCallId && call.toolCallId !== event.id) return null;
  const receipt = call?.rawOutput?.metadata?.mcp?._meta?.['openclank.grep-links.v1'];
  if (receipt?.version !== 1 || !Array.isArray(receipt.hits)) return null;
  const hits = receipt.hits.filter(hit =>
    typeof hit?.label === 'string' && hit.label.length <= 255 &&
    typeof hit.ref === 'string' && hit.ref.length <= 16384 && /^rr1\.[a-z0-9_-]+={0,2}$/i.test(hit.ref) &&
    Number.isSafeInteger(hit.line) && hit.line > 0 &&
    typeof hit.fingerprint === 'string' && /^sha256:[a-f0-9]{64}:[0-9]{1,7}$/.test(hit.fingerprint),
  ).slice(0, 12);
  if (!hits.length) return null;

  const section = document.createElement('div');
  const status = document.createElement('p');
  status.setAttribute('role', 'status');
  const addAction = (hit, label, open) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'copal-btn';
    button.textContent = label;
    button.addEventListener('click', async click => {
      click.stopPropagation();
      const origin = window.chatModule?.ratingAccountScope?.();
      const current = () => {
        const scope = window.chatModule?.ratingAccountScope?.();
        return Boolean(button.isConnected && origin?.owner && origin.owner === document.body?.dataset?.accountId &&
          scope?.owner === origin.owner && scope.sid === origin.sid && scope.authEpoch === origin.authEpoch);
      };
      if (!current()) return;
      button.disabled = true;
      status.textContent = '';
      try {
        const expectedRevision = { kind: 'hostFingerprint', value: hit.fingerprint };
        const opened = await open({expectedRevision, isCurrent: current});
        if (current() && !opened) status.textContent = 'The captured match is unavailable.';
      } catch (error) {
        if (current()) status.textContent = error?.message || 'The captured match is unavailable.';
      } finally {
        if (button.isConnected) button.disabled = false;
      }
    });
    section.append(button);
  };
  for (const hit of hits) {
    addAction(hit, `Open ${hit.label} · line ${hit.line}`, async options => {
      const editor = window.copalModule || await import('./copal.js');
      return options.isCurrent() && editor.openResource(hit.ref, {...options, sourceLine: hit.line});
    });
    addAction(hit, `Show ${hit.label} in Files`, async options => {
      const files = await import('./files.js');
      return options.isCurrent() && files.revealResource(hit.ref, {...options, workspaceMatch: true});
    });
  }
  section.append(status);
  return section;
}
