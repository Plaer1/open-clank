// Keep navigation local to the loaded history. Prepending a history page updates
// its ticks; streaming tokens never rebuild the index.
const turnStart = '.msg-user, .chat-buddy-system';
const history = document.getElementById('chat-history');
if (history) mount(history);

function excerpt(body, limit) {
  if (!body) return '';
  const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
  let text = '';
  for (let node; text.length < limit && (node = walker.nextNode());) {
    if (node.parentElement.closest('button,script,style,[hidden],.thinking-block,details:not([open])')) continue;
    text += node.data.slice(0, limit - text.length) + ' ';
  }
  return text.replace(/\s+/g, ' ').trim();
}

function mount(history) {
  const host = history.parentElement;
  const rail = document.createElement('nav');
  rail.className = 'chat-turn-rail';
  rail.setAttribute('aria-label', 'Conversation turns');
  rail.hidden = true;
  const list = document.createElement('div');
  list.className = 'chat-turn-list';
  rail.append(list);
  const card = document.createElement('aside');
  card.className = 'chat-turn-card';
  card.hidden = true;
  card.id = 'chat-turn-preview';
  card.setAttribute('role', 'tooltip');
  const title = document.createElement('strong');
  const reply = document.createElement('p');
  const tools = document.createElement('small');
  for (const excerptNode of [title, reply, tools]) excerptNode.dataset.userContent = '';
  const soundHint = document.createElement('small');
  card.append(title, reply, tools, soundHint);
  host.append(rail, card);

  const colors = ['#62c7e8','#f6be48','#a8de53','#ff776e','#ed6ab0','#bcace8'];
  let turns = [], buttons = new Map(), active = null, preview = null, frame = 0;
  let audio, lastTick = -Infinity, dwell;

  function tick(button, click = false) {
    if (!click && performance.now() - lastTick < 45) return;
    lastTick = performance.now();
    audio ??= new Audio('/static/audio/turn-tick.wav');
    audio.preservesPitch = false;
    audio.playbackRate = [1, 1.125, 1.25, 1.5, 1.667][Number(button.dataset.index) % 5];
    audio.volume = click ? 0.95 : 0.85;
    audio.currentTime = 0;
    audio.play().then(() => { soundHint.textContent = ''; }).catch(error => {
      if (error.name === 'NotAllowedError') soundHint.textContent = 'Click a turn to enable sound';
    });
  }
  function dismiss() {
    clearTimeout(dwell);
    preview?.removeAttribute('aria-describedby');
    preview = null;
    card.hidden = true;
  }
  function show(button) {
    if (!button || preview === button) return;
    dismiss();
    const user = turns[Number(button.dataset.index)];
    if (!user) return;
    preview = button;
    title.textContent = excerpt(user.matches('.chat-buddy-system') ? user.querySelector('span') : user.querySelector('.body'), 180) || button.ariaLabel;
    let answer = '';
    const labels = new Set();
    for (let row = user.nextElementSibling; row && !row.matches(turnStart); row = row.nextElementSibling) {
      if (!answer && row.matches('.msg-ai')) answer = excerpt(row.querySelector('.body'), 240);
      if (labels.size < 6) {
        for (const label of row.getElementsByClassName('agent-thread-tool')) {
          if (label.textContent.trim()) labels.add(label.textContent.trim());
          if (labels.size === 6) break;
        }
      }
      if (answer && labels.size === 6) break;
    }
    reply.textContent = answer;
    tools.textContent = [...labels].join(' · ');
    card.style.setProperty('--tick', button.style.getPropertyValue('--tick'));
    card.hidden = false;
    placeCard();
    button.setAttribute('aria-describedby', card.id);
    dwell = setTimeout(() => { if (preview === button) tick(button); }, 65);
  }
  function placeCard() {
    if (!preview) return;
    const rect = preview.getBoundingClientRect();
    const bounds = list.getBoundingClientRect();
    if (rect.bottom <= bounds.top || rect.top >= bounds.bottom) { dismiss(); return; }
    const y = rect.top - host.getBoundingClientRect().top;
    card.style.top = Math.max(0, Math.min(host.clientHeight - card.offsetHeight, y - card.offsetHeight / 2 + 6)) + 'px';
  }
  function position() {
    rail.style.top = history.offsetTop + 'px';
    rail.style.height = history.clientHeight + 'px';
    placeCard();
    schedule();
  }
  function schedule() {
    if (!frame && turns.length) frame = requestAnimationFrame(markCurrent);
  }
  function markCurrent() {
    frame = 0;
    if (!turns.length) return;
    const top = history.getBoundingClientRect().top + 12;
    let lo = 0, hi = turns.length;
    while (lo + 1 < hi) {
      const mid = (lo + hi) >> 1;
      if (turns[mid].getBoundingClientRect().top <= top) lo = mid;
      else hi = mid;
    }
    if (history.scrollTop > 0 && history.scrollTop + history.clientHeight >= history.scrollHeight - 2) lo = turns.length - 1;
    const next = buttons.get(turns[lo]);
    if (next === active) return;
    if (active) { active.removeAttribute('aria-current'); active.tabIndex = -1; }
    active = next;
    active.setAttribute('aria-current','true');
    active.tabIndex = 0;
    if (!preview && !list.contains(document.activeElement)) {
      const y = active.offsetTop - list.offsetTop;
      if (y < list.scrollTop || y + active.offsetHeight > list.scrollTop + list.clientHeight) list.scrollTop = y - list.clientHeight / 2;
    }
  }
  function index() {
    dismiss();
    turns = Array.from(history.children).filter(row => row.matches(turnStart));
    const next = new Map();
    turns.forEach((turn, i) => {
      const button = buttons.get(turn) || document.createElement('button');
      button.type = 'button';
      button.tabIndex = button === active ? 0 : -1;
      button.dataset.index = i;
      button.setAttribute('aria-label', 'Turn ' + (i + 1));
      button.style.setProperty('--tick', colors[i % colors.length]);
      next.set(turn, button);
      if (list.children[i] !== button) list.insertBefore(button, list.children[i] || null);
    });
    for (const [turn, button] of buttons) if (!next.has(turn)) button.remove();
    buttons = next;
    rail.hidden = !turns.length;
    position();
  }
  list.addEventListener('pointerover', e => { if (e.pointerType !== 'touch') show(e.target.closest('button')); });
  let closeTimer;
  function leave() {
    clearTimeout(closeTimer);
    closeTimer = setTimeout(dismiss, 100);
  }
  for (const element of [list,card]) {
    element.addEventListener('pointerenter', () => clearTimeout(closeTimer));
    element.addEventListener('pointerleave', leave);
  }
  list.addEventListener('focusin', e => show(e.target.closest('button')));
  list.addEventListener('focusout', dismiss);
  list.addEventListener('scroll', placeCard, {passive:true});
  function jump(button) {
    if (!button) return;
    const turn = turns[Number(button.dataset.index)];
    history.scrollTo({
      top: history.scrollTop + turn.getBoundingClientRect().top - history.getBoundingClientRect().top,
      behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth'
    });
    tick(button, true);
    dismiss();
  }
  list.addEventListener('click', e => jump(e.target.closest('button')));
  card.addEventListener('pointerdown', e => e.preventDefault());
  card.addEventListener('click', () => jump(preview));
  list.addEventListener('keydown', e => {
    if (e.key === 'Escape') dismiss();
    const sibling = e.key === 'ArrowDown' ? e.target.nextElementSibling : e.key === 'ArrowUp' ? e.target.previousElementSibling : null;
    if (sibling) { e.preventDefault(); sibling.focus(); }
  });
  history.addEventListener('scroll', schedule, {passive:true});
  new ResizeObserver(position).observe(history);
  new MutationObserver(records => {
    if (records.some(r => [...r.addedNodes,...r.removedNodes].some(n => n.nodeType === 1 && n.matches(turnStart)))) index();
  }).observe(history, {childList:true});
  index();
}
