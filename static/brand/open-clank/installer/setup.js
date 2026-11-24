// The host supplies display values and handles every intent. This view has no OS access.
window.OpenClankSetup = function mountSetup(root, send) {
  const make = (tag, text, cls) => {
    const el = document.createElement(tag);
    if (text != null) el.textContent = text;
    if (cls) el.className = cls;
    return el;
  };
  const bar = make('header', null, 'oc-bar');
  const mark = make('img');
  mark.src = '../SVG/open-clank-pyramid-eye.svg'; mark.alt = '';
  const platform = make('span', '', 'oc-platform');
  bar.append(mark, make('span','Open Clank Setup'), platform);
  const layout = make('div', null, 'oc-layout');
  const steps = make('ol',null,'oc-steps');
  steps.setAttribute('aria-label','Installation progress');
  const rail = make('aside',null,'oc-rail');
  const eye = make('img',null,'oc-sidebar-eye');
  eye.alt = ''; eye.src = '../../../icons/open-clank-appimage-eye.png';
  rail.append(eye,steps);
  const body = make('section',null,'oc-content');
  const title = make('h1'); title.tabIndex = -1;
  const area = make('div');
  const status = make('p'); status.setAttribute('role','status');
  const progress = make('progress',null,'oc-progress');
  progress.max = 100; progress.setAttribute('aria-label','Install progress');
  const actions = make('footer',null,'oc-actions');
  body.append(title,area,status,progress); layout.append(rail,body); root.replaceChildren(bar,layout,actions);
  let lastPhase;
  function button(label,type,primary=false) {
    const el = make('button',label,primary ? 'primary' : '');
    el.type = 'button'; el.onclick = () => send({type}); actions.append(el);
  }
  function choice(label,id,checked,radio=false) {
    const el = make('label',null,'oc-choice'), input = make('input');
    input.type = radio ? 'radio' : 'checkbox';
    input.name = radio ? 'install-mode' : id; input.value = id; input.checked = checked;
    input.onchange = () => send({type:radio ? 'mode' : 'option',id,checked:input.checked});
    el.append(input,document.createTextNode(label)); area.append(el);
  }
  function render(s) {
    const oldFocus = root.contains(document.activeElement) ? document.activeElement : null;
    const changed = s.phase !== lastPhase;
    const copy = s.platform === 'mac', linux = s.platform === 'linux';
    platform.textContent = ({mac:'macOS',windows:'Windows',linux:'Linux · AppImage'})[s.platform];
    const stages = ['Welcome','Terms of use','Licenses & credits','Install'];
    const active = s.phase === 'welcome' ? 0 : s.phase === 'terms' ? 1 : s.phase === 'credits' ? 2 : 3;
    if (changed) {
      steps.replaceChildren(...stages.map((name,i) => {
        const item = make('li',name);
        if (i === active) item.setAttribute('aria-current','step');
        if (i < active || s.phase === 'complete') item.className = 'finished';
        return item;
      }));
    }
    // Keep the progress control and Cancel button mounted across progress updates.
    if (!changed && s.phase === 'working' && actions.childElementCount === Number(Boolean(s.canCancel))) {
      updateProgress(s); return;
    }
    lastPhase = s.phase;
    title.classList.toggle('oc-visually-hidden',s.phase === 'welcome');
    area.replaceChildren(); actions.replaceChildren(); status.textContent = ''; status.className = ''; progress.hidden = true;
    const paragraph = text => area.append(make('p',text));
    if (s.phase === 'welcome') {
      title.textContent = 'Set up Open Clank';
      const art = make('img',null,'oc-art'); art.alt = ''; art.src = '../SVG/open-clank-wordmark-wide.svg'; area.append(art);
      paragraph(linux ? 'Run from this AppImage or add Open Clank to your applications menu.' : 'Install the workspace for your chats, journal and projects.');
      if (linux) {
        choice('Add to my applications','integrated',s.mode === 'integrated',true);
        choice('Run this AppImage once','portable',s.mode === 'portable',true);
      }
      button('Close','close'); button('Continue','terms',true);
    } else if (s.phase === 'terms') {
      title.textContent = 'Terms of use';
      if (window.setupDocuments) {
        for (const part of window.setupDocuments.terms.trim().split(/\n\s*\n/)) paragraph(part);
      } else {
        const text = make('iframe',null,'oc-terms');
        text.src = 'terms.txt'; text.title = 'Terms of use'; area.append(text);
      }
      button('Back','back'); button('Accept & continue','credits',true);
    } else if (s.phase === 'credits') {
      title.textContent = 'Licenses & credits';
      paragraph('Thank you to the contributors behind Odysseus, Copal, MiMo Code, opencode, Lore and the tools, fonts and artwork that make Open Clank possible.');
      if (window.setupDocuments) {
        const {notices, markdown} = window.setupDocuments;
        const rows = new Map();
        function display(name) {
          const row = rows.get(name);
          if (row.childElementCount > 1) return;
          if (!markdown?.[name]) {
            row.append(make('pre', notices[name]));
            return;
          }
          const article = make('article', null, 'oc-notice');
          // Generated at packaging time with raw HTML and image loading disabled.
          article.innerHTML = markdown[name];
          for (const link of article.querySelectorAll('a[href]')) {
            const url = new URL(link.getAttribute('href'), new URL(name, 'https://notices.invalid/'));
            link.onclick = event => {
              event.preventDefault();
              if (url.origin !== 'https://notices.invalid') {
                if (['http:', 'https:'].includes(url.protocol)) send({type:'notice-link', url:url.href});
                return;
              }
              const target = decodeURIComponent(url.pathname.slice(1));
              const next = rows.get(target);
              if (!next) return;
              display(target);
              next.open = true;
              const heading = [...next.querySelectorAll('h1,h2,h3,h4')].find(el =>
                el.textContent.toLowerCase().replace(/[^a-z0-9 -]/g,'').replace(/ +/g,'-') === url.hash.slice(1));
              (heading || next).scrollIntoView({block:'start'});
            };
          }
          row.append(article);
        }
        for (const name of Object.keys(notices)) {
          const row = make('details');
          row.append(make('summary',name === 'LICENSE' ? 'Open Clank · AGPL-3.0-or-later' : name === 'ACKNOWLEDGMENTS.md' ? 'Acknowledgments' : name));
          rows.set(name,row);
          row.ontoggle = () => { if (row.open) display(name); };
          area.append(row);
        }
        display('ACKNOWLEDGMENTS.md');
        rows.get('ACKNOWLEDGMENTS.md').open = true;
      } else {
        for (const [label,path] of [['Acknowledgments','ACKNOWLEDGMENTS.md'],['Open Clank license','LICENSE'],['Component notices','licenses/BUNDLED-COMPONENTS.md']]) {
          const link = make('a',label); link.href = '../../../../' + path; link.target = '_blank'; area.append(link);
        }
      }
      button('Back','back'); button('Continue','review',true);
    } else if (s.phase === 'review') {
      if (linux && s.mode === 'portable') {
        title.textContent = 'Run Open Clank once';
        paragraph('Open Clank will run from this AppImage without adding an applications menu entry.');
        button('Back','back'); button('Run once','portable',true);
      } else {
      title.textContent = s.existing ? 'Review update' : 'Review installation';
      paragraph(s.existing ? 'An installation already exists at this destination. Choose Update to replace its application files.' : copy ? 'Setup will copy Open Clank to Applications for you.' : 'Open Clank will be available from this location.');
      area.append(make('code',s.destination,'oc-path'));
      if (linux) paragraph('Adds a launcher under Education in your applications menu.');
      for (const option of s.options || []) choice(option.label,option.id,option.checked);
      button('Back','back');
      button(s.existing ? 'Update Open Clank' : copy ? 'Copy to Applications' : 'Install Open Clank','install',true);
      }
    } else if (s.phase === 'working') {
      title.textContent = 'Setting up Open Clank'; updateProgress(s);
      if (s.canCancel) button('Cancel','cancel');
    } else if (s.phase === 'error') {
      title.textContent = 'Setup needs attention'; status.className = 'error'; status.textContent = s.message;
      if (s.details) { const d = make('details'); d.append(make('summary','Technical details'),make('code',s.details,'oc-path')); area.append(d); }
      button('Close','close'); if (s.canRetry) button('Try again','retry',true);
    } else if (s.phase === 'cancelled') {
      title.textContent = 'Setup cancelled'; paragraph(s.message); button('Close','close'); button('Review setup','review',true);
    } else if (s.phase === 'complete') {
      title.textContent = 'Open Clank is ready'; paragraph(s.message); area.append(make('code',s.destination,'oc-path'));
      button('Done','close'); button('Open Clank','launch',true);
    } else if (s.phase === 'closed') {
      title.textContent = s.message || 'Setup closed';
    }
    if (changed && oldFocus) title.focus({preventScroll:true});
    else if (oldFocus?.matches('input')) [...area.querySelectorAll('input')].find(input=>input.value===oldFocus.value)?.focus();
  }
  function updateProgress(s) {
    progress.hidden = false;
    if (Number.isFinite(s.percent)) progress.value = Math.max(0,Math.min(100,s.percent));
    else progress.removeAttribute('value');
    if (status.textContent !== s.message) status.textContent = s.message;
  }
  return {render};
};
