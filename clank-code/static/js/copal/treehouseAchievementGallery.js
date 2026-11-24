import { registerTreeHouseView } from './treehouseViews.js';
import { achievementArtManifest, mountAchievementArtwork, applyAchievementArtPack, currentAchievementArtPack, clearAchievementArtPack } from './treehouseAchievementArt.js';
import { renderTreeHouseMilestoneTrack } from './treehouseProgression.js';
import { drainAchievementNotifications } from '../achievementClient.js';
function installStyles() {
  if (document.getElementById('treehouse-gallery-styles')) return;
  const link = document.createElement('link'); link.id = 'treehouse-gallery-styles'; link.rel = 'stylesheet'; link.href = new URL('./treehouseAchievementGallery.css',import.meta.url).href; document.head.append(link);
}
function safeDate(value) { const date = new Date(value); return value && Number.isFinite(date.getTime()) ? date.toLocaleDateString(undefined,{year:'numeric',month:'short',day:'numeric'}) : null; }
function concealed(entry) { return !entry.earned && entry.rarity !== 'normal'; }
function displayTitle(entry) { return concealed(entry) ? '???' : String(entry.title || 'Achievement'); }
function artwork(h,entry,accountId,className='') {
  const host = h('div',{class:`th-achievement-art ${className}`},h('span',{class:'th-art-loading','aria-hidden':'true',text:concealed(entry) ? '?' : '✦'}));
  void mountAchievementArtwork(host,entry,{accountId}); return host;
}
export function renderTreeHouseAchievementGallery(root,context) {
  installStyles(); const { h,ui,api,setStatus } = context;
  const host = h('div',{class:'th-achievement-workspace copal-treehouse-workspace','aria-label':'Achievement collection'});
  root.append(host); const loading = h('div',{class:'copal-empty',role:'status',text:'Opening your achievement collection…'}); host.append(loading);
  api('/treehouse/achievements?admin=false').then(presentation => {
    if (!host.isConnected) return;
    const accountId = presentation.accountId;
    // Server remains the authority. Defense in depth keeps unearned ultras out
    // even if an author catalogue response were accidentally passed here.
    const entries = (presentation.entries || []).filter(entry => entry.earned || entry.rarity !== 'ultra');
    ui.achievementFilter ||= 'all'; ui.achievementSearch ||= '';
    const state = {detail:ui.achievementDetail || null};
    const feedback = h('p',{class:'th-gallery-feedback',role:'status'});
    const galleryHead = h('header',{class:'th-gallery-head'},h('div',{},h('span',{class:'th-eyebrow',text:'YOUR OPEN CLANK COLLECTION'}),h('h1',{text:'A house full of achievements'}),h('p',{text:'Small discoveries. Real accomplishments. A collection that grows with you.'})),h('div',{class:'th-collection-count'},h('strong',{text:presentation.counter || '0/34'}),h('span',{text:'earned'})));
    const content = h('div',{class:'th-gallery-content'});
    const collectionMeter = h('div',{class:'th-collection-meter',role:'progressbar','aria-label':'Visible achievement collection','aria-valuemin':'0','aria-valuemax':String(presentation.normalTotal || 34),'aria-valuenow':String(presentation.normalEarned || 0)},h('span',{style:`width:${Math.max(0,Math.min(100,(Number(presentation.normalEarned) || 0)/(Number(presentation.normalTotal) || 34)*100))}%`}));
    const customization = h('details',{class:'th-art-customization'},h('summary',{text:'Customize your collection artwork'}));
    const customizeActions = h('div',{class:'th-gallery-actions'});
    const download = h('a',{class:'copal-btn',text:'Download mascot kit',href:'/static/icons/treehouse-achievements/mascot-kit.zip',download:''});
    achievementArtManifest().then(manifest => { if (download.isConnected) download.href = manifest.download; }).catch(() => {});
    const input = h('input',{type:'file',accept:'.json,application/json',hidden:true,'aria-label':'Import achievement artwork pack'});
    input.addEventListener('change',async () => {
      const file = input.files?.[0]; if (!file) return;
      try { if (file.size > 4*1024*1024) throw new Error('Artwork packs must be under 4 MiB.'); const result = await applyAchievementArtPack(await file.text(),accountId); setStatus(`Artwork updated · ${result.count} icon${result.count === 1 ? '' : 's'}`); }
      catch (error) { feedback.textContent = error.message || 'Artwork pack could not be imported.'; }
      finally { input.value = ''; }
    });
    customizeActions.append(download,h('button',{class:'copal-btn',text:'Import artwork pack',onclick:() => input.click()}),h('button',{class:'copal-btn',text:'Restore default artwork',onclick:() => { try { clearAchievementArtPack(accountId); setStatus('Default artwork restored'); } catch (error) { feedback.textContent = error.message; } }}),input);
    let packName = 'Default Open Clank artwork'; try { packName = currentAchievementArtPack(accountId)?.name || packName; } catch (_) { /* Auth setup can finish before art owner is initialized. */ }
    customization.append(h('p',{text:`${packName}. Custom PNG packs stay with this account in this browser. The kit includes editable mascot sources, recipes and export tools.`}),customizeActions);
    host.replaceChildren(galleryHead,collectionMeter,content,customization,feedback);
    const showDetail = entry => { state.detail = entry.id; ui.achievementDetail = entry.id; draw(); host.querySelector('.th-achievement-detail h2')?.focus({preventScroll:true}); };
    const draw = () => {
      content.replaceChildren();
      const selected = entries.find(entry => entry.id === state.detail);
      if (selected) {
        const back = h('button',{class:'copal-btn',icon:'back',text:'Back to collection',onclick:() => {state.detail = null;ui.achievementDetail = null;draw();host.querySelector('.th-gallery-filter button')?.focus({preventScroll:true});}});
        const date = safeDate(selected.earnedAt); const isHidden = concealed(selected);
        const body = h('div',{class:'th-achievement-detail-copy'},h('span',{class:'th-eyebrow',text:isHidden ? 'MYSTERY ACHIEVEMENT' : selected.rarity === 'ultra' ? 'ULTRA RARE ACHIEVEMENT' : selected.earned ? 'ACHIEVEMENT EARNED' : 'YOUR NEXT DISCOVERY'}),h('h2',{text:displayTitle(selected),tabindex:'-1'}),h('p',{text:isHidden ? 'Keep exploring the House. Its name, artwork and requirements are revealed when you earn it.' : selected.summary || 'Earned through your activity in Open Clank.'}),h('span',{class:`th-award-state ${selected.earned ? 'earned' : 'locked'}`,text:selected.earned ? date ? `Earned ${date}` : 'Earned' : 'Not yet earned'}));
        if (!isHidden) body.append(h('section',{class:'th-award-requirement'},h('h3',{text:selected.earned ? 'Completion evidence' : 'How to earn it'}),h('p',{text:selected.earned ? `${selected.evidenceRefs?.length || 0} saved evidence reference${selected.evidenceRefs?.length === 1 ? '' : 's'}${selected.evidenceRefs?.length ? ' support this award.' : '. This award was granted by the achievement engine.'}` : selected.summary || 'Complete the relevant activity in Open Clank. Its saved evidence is checked automatically.'})));
        if (selected.earned && selected.evidenceRefs?.length) body.append(h('details',{class:'th-diagnostics'},h('summary',{text:'Evidence reference details'}),h('p',{text:'Reference identifiers are diagnostic records. Access to their source still follows its normal permissions.'}),h('ul',{},selected.evidenceRefs.map(ref => h('li',{text:String(ref)})))));
        content.append(back,h('article',{class:'th-achievement-detail','aria-label':'Achievement details'},artwork(h,selected,accountId,'th-detail-art'),body));
        return;
      }
      const reached = entries.filter(entry => entry.earned); const pending = entries.filter(entry => !entry.earned);
      const trail = h('details',{class:'th-gallery-trail',open:!!ui.achievementTrailOpen},h('summary',{},h('span',{},h('strong',{text:'Your achievement trail'}),h('small',{text:`${reached.length} earned milestone${reached.length === 1 ? '' : 's'} · expand to explore`})),h('span',{class:'th-trail-preview','aria-hidden':'true'},[...reached,...pending].slice(0,6).map(entry => h('span',{class:entry.earned ? 'achieved' : '',text:entry.earned ? '✓' : '○'})))));
      trail.addEventListener('toggle',() => {ui.achievementTrailOpen = trail.open;});
      const trailBody = h('div');trail.append(trailBody);content.append(trail);
      renderTreeHouseMilestoneTrack(trailBody,{h,title:'Your achievement trail',description:'Awards unlock independently through real activity. Explore any visible objective; this order adds no completion rules.',milestones:[...reached,...pending].map(entry => ({id:entry.id,label:displayTitle(entry),complete:entry.earned,unlocked:entry.rarity === 'normal',onOpen:() => showDetail(entry)}))});
      const toolbar = h('div',{class:'th-gallery-toolbar'}); const filters = h('div',{class:'th-gallery-filter',role:'group','aria-label':'Achievement filters'});
      const grid = h('div',{class:'th-achievement-grid',role:'list','aria-label':'Achievement collection'});
      const search = h('input',{class:'copal-search',type:'search',placeholder:'Find an achievement…','aria-label':'Search visible achievement names',value:ui.achievementSearch});
      const drawCards = () => {
        grid.replaceChildren();
        const visible = entries.filter(entry => ui.achievementFilter === 'earned' ? entry.earned : ui.achievementFilter === 'locked' ? !entry.earned : true).filter(entry => !ui.achievementSearch || displayTitle(entry).toLowerCase().includes(ui.achievementSearch.toLowerCase()) || !concealed(entry) && String(entry.summary || '').toLowerCase().includes(ui.achievementSearch.toLowerCase()));
        for (const entry of visible) {
          const isHidden = concealed(entry);
          const card = h('article',{class:`th-achievement-card ${entry.earned ? 'earned' : 'locked'} ${isHidden ? 'concealed' : ''}`,role:'listitem'},h('button',{class:'th-achievement-open','aria-label':isHidden ? 'View mystery achievement' : `View ${entry.title}`,onclick:() => showDetail(entry)},artwork(h,entry,accountId),h('div',{class:'th-achievement-card-copy'},h('span',{class:`th-award-state ${entry.earned ? 'earned' : 'locked'}`,text:entry.earned ? '✓ Earned' : isHidden ? 'Mystery' : 'Locked'}),h('h2',{text:displayTitle(entry)}),h('p',{text:isHidden ? 'A discovery waiting to happen.' : entry.summary || ''}),h('small',{text:entry.earned && safeDate(entry.earnedAt) ? safeDate(entry.earnedAt) : entry.earned ? 'Accomplishment saved' : 'View requirements'}))));
          grid.append(card);
        }
        if (!visible.length) grid.append(h('div',{class:'copal-empty',text:ui.achievementSearch ? 'No achievements match that search.' : ui.achievementFilter === 'earned' ? 'Your first achievement is waiting. Explore All to find a starting point.' : 'No achievements in this filter.'}));
      };
      for (const [id,label] of [['earned','Earned'],['all','All'],['locked','Locked']]) filters.append(h('button',{class:`copal-btn${ui.achievementFilter === id ? ' primary' : ''}`,text:label,'aria-pressed':String(ui.achievementFilter === id),onclick:() => {ui.achievementFilter = id;draw();}}));
      search.addEventListener('input',() => {ui.achievementSearch = search.value;drawCards();}); toolbar.append(filters,search);content.append(toolbar,grid);drawCards();
    };
    draw(); void drainAchievementNotifications();
  }).catch(error => { if (host.isConnected) loading.replaceChildren(h('p',{text:error.message || 'Your achievements are unavailable right now.'}),h('button',{class:'copal-btn',text:'Try again',onclick:() => {root.replaceChildren();renderTreeHouseAchievementGallery(root,context);}})); });
}
registerTreeHouseView('achievements',renderTreeHouseAchievementGallery);
