import { startBuddyExplanation } from './buddyExplain.js';
import { topPortalZ } from './toolWindowZOrder.js';
import Storage from './storage.js';
import { getWorkspaceId } from './workspace.js';

const LABELS = {
  alert: 'Alert',
  drowsy: 'Drowsy',
  sleeping: 'Sleeping',
  working: 'Work reported in this chat',
  waiting: 'Work is waiting',
  input: 'Waiting for your input',
  error: 'A worker reported an error',
  quiet: 'No active work in the observed rows',
  off: 'Work observation is off',
  unavailable: 'Work status unavailable',
};

let account = '';
let preferences = { show: false, look: 'standard' };
let presentation = null;
let settings = null;
const launchers = new Set();
let sendExplanation = null;
let authRevision = 0;

function storageKey() {
  return `openclank-buddy:scope:${encodeURIComponent(account)}`;
}

function node(tag, className, text = '') {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text) element.textContent = text;
  return element;
}

function button(label, className) {
  const element = node('button', className, label);
  element.type = 'button';
  return element;
}

function loadStyles() {
  if (document.getElementById('openclank-buddy-style')) return;
  const stylesheet = node('link');
  stylesheet.id = 'openclank-buddy-style';
  stylesheet.rel = 'stylesheet';
  stylesheet.href = '/static/css/interactive-buddy.css';
  document.head.append(stylesheet);
}

function focusedChat() {
  return window.sessionModule?.getCurrentSessionId?.() || '';
}

function observedPose(value) {
  if (value?.version !== 1 || value.scope?.kind !== 'verified_task_observation_page'
      || value.source?.connection !== 'connected' || !Number.isSafeInteger(value.source.observedAt)
      || !Array.isArray(value.actors) || value.actors.length > 32
      || !Array.isArray(value.tasks) || value.tasks.length > 32
      || !Array.isArray(value.requests?.items) || value.requests.items.length > 64) return 'unavailable';
  if (value.requests.items.some(item => item?.state === 'inputneeded')) return 'input';
  if (value.actors.some(item => item?.state === 'error')) return 'error';
  if ([...value.actors, ...value.tasks].some(item => item?.state === 'working')) return 'working';
  if ([...value.tasks, ...value.requests.items].some(item => item?.state === 'waiting')) return 'waiting';
  return ['partial', 'available'].includes(value.source.availability) ? 'quiet' : 'unavailable';
}

async function getJson(url, signal) {
  const response = await fetch(url, { signal, credentials: 'same-origin', cache: 'no-store' });
  if (!response.ok) throw new Error('Buddy work observation unavailable');
  return response.json();
}

function createPresentation(owner) {
  const lifetime = new AbortController();
  const options = { signal: lifetime.signal };
  const root = node('aside', 'bound-buddy');
  root.setAttribute('aria-label', 'Open Clank buddy');
  const play = button('', 'bound-buddy-play');
  play.setAttribute('aria-label', 'Buddy actions');
  play.setAttribute('aria-expanded', 'false');
  play.title = 'Click for Buddy actions. Drag to move.';
  const art = node('span', 'bound-buddy-art');
  art.setAttribute('aria-hidden', 'true');
  const eye = node('span', 'bound-buddy-eye');
  art.append(eye);
  const figure = node('span', 'bound-buddy-figure');
  figure.setAttribute('aria-hidden', 'true');
  figure.append(art);
  play.append(figure);
  const menu = node('div', 'bound-buddy-actions');
  menu.hidden = true;
  const explain = button('Explain this to me');
  menu.append(explain);
  const notice = node('span', 'bound-buddy-notice');
  notice.setAttribute('role', 'status');
  root.append(play, menu, notice);

  let sleepTimer = null;
  let animation = null;
  let pending = null;
  let refreshTimer = null;
  let refreshQueued = false;
  let streamChat = null;
  let pose = null;
  const important = () => ['input', 'error', 'working', 'waiting'].includes(pose);
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const setPose = next => {
    if (pose === next) return;
    pose = next;
    root.dataset.pose = next;
    notice.textContent = next === 'input' || next === 'error' ? LABELS[next] : '';
  };
  const resetRest = () => {
    clearTimeout(sleepTimer);
    if (important() || document.hidden) return;
    sleepTimer = setTimeout(() => {
      if (['alert', 'quiet'].includes(pose)) setPose('drowsy');
      sleepTimer = setTimeout(() => {
        if (pose === 'drowsy') setPose('sleeping');
      }, 20000);
    }, 28000);
  };
  const wake = () => {
    if (['drowsy', 'sleeping'].includes(pose)) setPose('alert');
    resetRest();
  };
  let drag = null, dragged = false, cancelPicker = null, pointer = null, lookFrame = 0;
  const raise = () => { root.style.zIndex = topPortalZ(); };
  const closeMenu = () => {
    menu.hidden = true;
    play.setAttribute('aria-expanded', 'false');
  };
  const position = (left, top) => {
    const bounds = root.getBoundingClientRect(), zoom = root.currentCSSZoom || 1;
    const x = Math.max(8,Math.min(innerWidth-bounds.width-8,left));
    const y = Math.max(8,Math.min(innerHeight-bounds.height-8,top));
    root.style.left = x/zoom+'px';
    root.style.top = y/zoom+'px';
    root.style.right = root.style.bottom = 'auto';
    return {x,y};
  };
  function aim() {
    lookFrame = 0;
    if (!pointer) return;
    const bounds = art.getBoundingClientRect();
    if (!art.offsetWidth || !art.offsetHeight) return;
    const x = pointer.x - bounds.left - bounds.width * (eye.offsetLeft + eye.offsetWidth / 2) / art.offsetWidth;
    const y = pointer.y - bounds.top - bounds.height * (eye.offsetTop + eye.offsetHeight / 2) / art.offsetHeight;
    const scale = 5 / (Math.hypot(x, y) + 1.75 * bounds.width);
    art.style.setProperty('--buddy-look-x', x*scale+'px');
    art.style.setProperty('--buddy-look-y', y*scale+'px');
  }
  function scheduleAim() {
    if (!lookFrame) lookFrame=requestAnimationFrame(aim);
  }
  play.addEventListener('click', event => {
    if (dragged && event.detail) { dragged=false; return; }
    wake(); raise();
    if (!menu.hidden) { closeMenu(); return; }
    const bounds=play.getBoundingClientRect();
    root.classList.toggle('bound-buddy-open-down',bounds.top<140);
    root.classList.toggle('bound-buddy-open-right',bounds.right<240);
    menu.hidden=false;
    play.setAttribute('aria-expanded','true');
    if (!event.detail) explain.focus({preventScroll:true});
    if (important() || reducedMotion.matches) return;
    animation?.cancel();
    animation = figure.animate?.([{translate:'0 0'},{translate:'0 -3px'},{translate:'0 0'}],
      {duration:260,easing:'ease-out'});
  },options);
  explain.addEventListener('click', () => {
    closeMenu();
    cancelPicker?.();
    cancelPicker = startBuddyExplanation(root,play,lifetime.signal,message => {
      if (account === owner && sendExplanation) {
        void sendExplanation(message,owner).catch(() => {
          if (account === owner) window.uiModule?.showToast('Buddy could not open an explanation chat.');
        });
      }
    });
  },options);
  document.addEventListener('pointerdown',event => {
    raise();
    if (!root.contains(event.target)) closeMenu();
  },{...options,capture:true});
  root.addEventListener('keydown',event => {
    if (event.key === 'Escape' && !menu.hidden) { event.stopPropagation(); closeMenu(); play.focus(); }
  },options);
  play.addEventListener('pointerdown',event => {
    if (event.button !== 0) return;
    const {left,top}=root.getBoundingClientRect();
    dragged=false;
    drag={id:event.pointerId,left,top,x:event.clientX,y:event.clientY};
  },options);
  document.addEventListener('pointermove',event => {
    if (event.pointerType !== 'touch') {
      pointer={x:event.clientX,y:event.clientY};
      scheduleAim();
    }
    if (drag?.id !== event.pointerId) return;
    const x=event.clientX-drag.x,y=event.clientY-drag.y;
    if (!dragged && Math.hypot(x,y)<5) return;
    if (!dragged) {
      dragged=true; closeMenu();
      play.setPointerCapture(event.pointerId);
    }
    drag.position=position(drag.left+x,drag.top+y);
  },{...options,capture:true,passive:true});
  document.addEventListener('pointerup',event => {
    if (drag?.id !== event.pointerId) return;
    if (drag.position) changePreferences({position:drag.position});
    drag=null;
  },{...options,capture:true});
  document.addEventListener('pointercancel',event => {
    if (drag?.id===event.pointerId) drag=null;
  },options);
  window.addEventListener('resize',() => {
    if (preferences.position) position(preferences.position.x,preferences.position.y);
    scheduleAim();
  },options);
  for (const type of ['odysseus:modal-opened','odysseus:modal-restored'])
    window.addEventListener(type,raise,options);

  function resetObservation() {
    pending?.abort();
    pending = null;
    clearTimeout(refreshTimer);
    refreshQueued = false;
    setPose('alert');
    resetRest();
  }

  function scheduleObservation(delay = 350, retry = true) {
    const chat = focusedChat();
    if (!chat || document.hidden) return;
    if (pending) {
      refreshQueued = true;
      return;
    }
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => {
      if (focusedChat() === chat) void refreshObservation({ retry });
    }, delay);
  }

  async function refreshObservation({ retry = false } = {}) {
    const chat = focusedChat();
    const workspace = getWorkspaceId();
    if (!chat || account !== owner) return;
    if (pending || document.hidden) return;
    const request = new AbortController();
    pending = request;
    const timeout = setTimeout(() => request.abort(), 8000);
    const current = () => !request.signal.aborted && !lifetime.signal.aborted && account === owner
      && pending === request && focusedChat() === chat && getWorkspaceId() === workspace;
    let resultPose = 'unavailable';
    try {
      const preference = await getJson('/api/prefs/companion_enabled', request.signal);
      if (!current()) return;
      if (preference?.account_id !== owner) {
        setPose('unavailable');
        return;
      }
      if (preference.value !== true) {
        setPose('off');
        return;
      }
      const result = await getJson(`/api/orchestration/${encodeURIComponent(chat)}/companion-observation`, request.signal);
      if (current()) {
        resultPose = observedPose(result);
        setPose(resultPose);
      }
    } catch (_) {
      if (current() || (pending === request && request.signal.aborted)) setPose('unavailable');
    } finally {
      clearTimeout(timeout);
      const stillCurrent = current();
      if (pending === request) {
        pending = null;
        resetRest();
      }
      // Opening a real stream can precede host-root publication. Retry that
      // observation once. Idle/missing chats never run a polling loop.
      if (stillCurrent && refreshQueued) {
        refreshQueued = false;
        scheduleObservation();
      } else if (retry && stillCurrent && resultPose === 'unavailable' && pose !== 'off'
          && window.chatModule?.hasActiveStream?.(chat)) {
        scheduleObservation(1250, false);
      }
    }
  }

  const sessionChanged = () => {
    resetObservation();
    const chat = focusedChat();
    streamChat = window.chatModule?.hasActiveStream?.(chat) ? chat : null;
    if (streamChat) scheduleObservation();
  };
  document.addEventListener('odysseus:session-selected', sessionChanged, options);
  window.addEventListener('workspace-change', () => { cancelPicker?.(); sessionChanged(); }, options);
  for (const type of ['odysseus:permission-pending', 'odysseus:interactive-resolved', 'openclank:work-attention']) {
    document.addEventListener(type, event => {
      if (event.detail?.sessionId === focusedChat()) scheduleObservation();
    }, options);
  }
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      animation?.cancel();
      resetObservation();
    } else sessionChanged();
  }, options);
  const chatLog = document.getElementById('chat-history');
  let busyState = chatLog?.getAttribute('aria-busy');
  const busy = new MutationObserver(() => {
    const next = chatLog.getAttribute('aria-busy');
    if (next === busyState) return;
    busyState = next;
    const chat = focusedChat();
    if (next === 'true' && window.chatModule?.hasActiveStream?.(chat)) {
      streamChat = chat;
      scheduleObservation();
    } else if (next === 'false' && streamChat === chat) {
      streamChat = null;
      scheduleObservation();
    }
  });
  if (chatLog) busy.observe(chatLog, { attributes: true, attributeFilter: ['aria-busy'] });

  setPose('alert');
  sessionChanged();
  return {
    mount() {
      document.body.append(root);
      if (preferences.position) position(preferences.position.x,preferences.position.y);
      raise();
    },
    setLook(look) { root.dataset.look = look; },
    remove() {
      lifetime.abort();
      busy.disconnect();
      if (lookFrame) cancelAnimationFrame(lookFrame);
      clearTimeout(refreshTimer);
      pending?.abort();
      clearTimeout(sleepTimer);
      animation?.cancel();
      root.remove();
    },
  };
}

function render() {
  for (const launcher of launchers) {
    launcher.disabled = !account;
    launcher.setAttribute('aria-pressed',String(Boolean(account && preferences.show)));
  }
  if (settings) {
    settings.show.disabled = !account;
    settings.show.checked = Boolean(account && preferences.show);
    settings.look.disabled = !account || !preferences.show;
    settings.look.value = preferences.look;
  }
  if (!account || !preferences.show) {
    presentation?.remove();
    presentation = null;
    return;
  }
  loadStyles();
  if (!presentation) {
    presentation = createPresentation(account);
    presentation.mount();
  }
  presentation.setLook(preferences.look);
}

function changePreferences(changes) {
  if (!account) return;
  preferences = { ...preferences, ...changes };
  Storage.setJSON(storageKey(), preferences);
  render();
}

function adoptAccount(detail) {
  const next = typeof detail?.accountId === 'string' ? detail.accountId.trim() : '';
  if (next === account) return;
  presentation?.remove();
  presentation = null;
  account = next;
  const saved = account ? Storage.getJSON(storageKey(), null) : null;
  preferences = { show: saved?.show === true, look: saved?.look === 'winter' ? 'winter' : 'standard' };
  if (Number.isFinite(saved?.position?.x) && Number.isFinite(saved?.position?.y)) preferences.position = saved.position;
  render();
}

export function bindBuddyLauncher(toggles, openChat) {
  sendExplanation = openChat;
  for (const toggle of toggles) {
    if (!toggle || launchers.has(toggle)) continue;
    launchers.add(toggle);
    toggle.addEventListener('click', () => changePreferences({show:!preferences.show}));
  }
  render();
}

export function mountBuddyAppearance(panel) {
  if (!panel) return;
  if (!panel.querySelector('[data-buddy-appearance]')) {
    const card = node('section', 'admin-card');
    card.dataset.buddyAppearance = '';
    const heading = node('h2', '', 'Buddy');
    const show = node('input');
    show.type = 'checkbox';
    show.id = 'settings-show-buddy';
    const showLabel = node('label', 'settings-label');
    showLabel.append(show, ' Show buddy');
    const look = node('select', 'settings-select');
    for (const [value, label] of [['standard', 'Standard'], ['winter', 'Winter']]) {
      const option = node('option', '', label);
      option.value = value;
      look.append(option);
    }
    const lookLabel = node('label', 'settings-label');
    lookLabel.append('Look ', look);
    show.addEventListener('change', () => changePreferences({ show: show.checked }));
    look.addEventListener('change', () => changePreferences({ look: look.value }));
    card.append(heading, showLabel, lookLabel);
    const rendering = panel.querySelector('[data-graphical-rendering-settings]');
    if (rendering) rendering.after(card);
    else panel.prepend(card);
    settings = { show, look };
  }
  render();
}

if (typeof document !== 'undefined' && typeof window !== 'undefined') {
  document.addEventListener('openclank:auth-context-changed', event => {
    authRevision++;
    adoptAccount(event.detail);
  });
  document.addEventListener('click', event => {
    if (event.target.closest?.('#settings-logout-btn')) {
      authRevision++;
      adoptAccount(null);
    }
  }, { capture: true });
  window.addEventListener('pagehide', () => { authRevision++; adoptAccount(null); });
  if (window.__odysseusAuthContextPromise) {
    const revision = authRevision;
    window.__odysseusAuthContextPromise.then(detail => {
      if (revision === authRevision) adoptAccount(detail);
    }).catch(() => {});
  }
}
