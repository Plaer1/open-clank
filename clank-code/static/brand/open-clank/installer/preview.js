// Local simulation for design review. The install host replaces this file.
(() => {
  const controls = document.querySelector('.preview-controls');
  let state, timer, failed;
  const ui = window.OpenClankSetup(document.querySelector('#setup'), handle);
  function reset() {
    clearInterval(timer); failed = false;
    const platform = controls.elements.platform.value;
    const scenario = controls.elements.scenario.value;
    const destinations = {linux:'~/Applications/Open-Clank.AppImage',mac:'/Applications/Open Clank.app',windows:'%LOCALAPPDATA%\\Programs\\Open Clank Beta\\windows-x64'};
    state = {phase:'welcome',platform,mode:'integrated',destination:destinations[platform],existing:scenario === 'update',
      options:platform === 'windows' ? [{id:'shortcut',label:'Create a desktop shortcut',checked:false}] : []};
    ui.render(state);
  }
  function run() {
    let step = 0;
    const stages = ['Checking package…','Preparing application files…',({mac:'Copying Open Clank to Applications…',linux:'Copying the AppImage and registering the launcher…',windows:'Installing application files and shortcuts…'})[state.platform]];
    Object.assign(state,{phase:'working',message:stages[0],percent:null,canCancel:true}); ui.render(state);
    timer = setInterval(() => {
      step++;
      if (controls.elements.scenario.value === 'error' && !failed && step === 3) {
        clearInterval(timer); failed = true;
        Object.assign(state,{phase:'error',message:'Setup could not write to the destination. Check folder permissions and retry.',details:state.destination,canRetry:true});
      } else if (step >= 9) {
        clearInterval(timer);
        Object.assign(state,{phase:'complete',message:state.platform === 'mac' ? 'Open Clank was copied to Applications.' : 'Open Clank is installed for this account.'});
      } else Object.assign(state,{percent:step*11,message:stages[Math.min(2,Math.floor(step/3))],canCancel:step<6});
      ui.render(state);
    },450);
  }
  function handle(event) {
    switch (event.type) {
      case 'mode': state.mode = event.id; break;
      case 'option': state.options.find(o=>o.id===event.id).checked=event.checked; break;
      case 'terms': case 'credits': case 'review': state.phase=event.type; break;
      case 'back': state.phase=({terms:'welcome',credits:'terms',review:'credits'})[state.phase]; break;
      case 'install': case 'retry': run(); return;
      case 'cancel': clearInterval(timer); Object.assign(state,{phase:'cancelled',message:'The preview stopped before the final copy.'}); break;
      case 'launch': case 'portable': Object.assign(state,{phase:'closed',message:'Open Clank launch preview'}); break;
      case 'close': Object.assign(state,{phase:'closed',message:'Setup closed'}); break;
    }
    ui.render(state);
  }
  controls.onchange=reset; document.querySelector('#restart').onclick=reset; reset();
})();
