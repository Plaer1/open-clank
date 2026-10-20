#!/usr/bin/env node

import assert from 'node:assert/strict';
import test from 'node:test';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const page = `<!doctype html><html><body><main id="treehouse"></main><script type="module">
(async () => { try {
  const { configureCopalStorage } = await import('/static/js/copal/storage.js');
  const { createTreeHouseFeature } = await import('/static/js/copal/treehouse.js');
  configureCopalStorage('treehouse-integrated');
  const h = (tag, attrs = {}, ...children) => { const node = document.createElement(tag); for (const [key, value] of Object.entries(attrs)) { if (key === 'text') node.textContent = value; else if (key === 'onclick') node.addEventListener('click', value); else if (key === 'class') node.className = value; else if (key === 'aria-label') node.setAttribute(key, value); else if (key !== 'selected' && key !== 'disabled') node[key] = value; } for (const child of children) node.append(child instanceof Node ? child : document.createTextNode(String(child))); return node; };
  const activity = { id:'activity:guide:lesson', fieldGuideKey:'fg-document-pilot', title:'Editor practice', activityType:'lesson', status:'published', points:10, content:'Edit the disposable note.', moduleId:'module:guide', skillIds:[], surface:{key:'editor',label:'Editor',href:'/copal/editor',locator:'[data-copal-view=notes]'}, practiceFixture:'field-guide/fg-editor', practice:{title:'Disposable note',seed:'# Practice\\n- [ ] Check',expectedEvidence:'The note reopens with the heading and checklist.'}, verifierSpec:{kind:'editor_markdown_revision',evidence:'The note reopens with the heading and checklist.'} };
  const course = { id:'course:guide', title:'Editor practice', description:'A disposable shared Field Guide course.', status:'published', moduleIds:['module:guide'] };
  const module = { id:'module:guide', courseId:'course:guide', title:'Practice', activityIds:[activity.id], assignmentIds:[] };
  const owner = { id:'acct-owner', displayName:'Owner', roles:['admin','instructor','learner'], active:true };
  const bob = { id:'acct-bob', displayName:'Bob', roles:['learner'], active:true };
  const progress = () => window.__complete ? { points:10, badges:[{badgeId:'badge:guide'}], quests:[], streak:1, completedActivityIds:[activity.id], courses:{'course:guide':{percent:100,modules:{'module:guide':{completed:1,total:1,percent:100}}}}, skills:{}, pointEvidence:[] } : { points:0,badges:[],quests:[],streak:0,completedActivityIds:[],courses:{},skills:{},pointEvidence:[] };
  const snapshot = () => { const isOwner = window.__actor === 'acct-owner'; const visible = isOwner || (window.__shared && !window.__revoked); const profile = isOwner ? owner : bob; const state = { revision:window.__revision, profiles:{[profile.id]:profile}, courses:visible ? {'course:guide':course}:{}, modules:visible ? {'module:guide':module}:{}, activities:visible ? {[activity.id]:activity}:{}, assignments:{}, skills:{}, badges:visible ? {'badge:guide':{id:'badge:guide',title:'Editor Practice',criteria:{type:'course',courseId:'course:guide'}}}:{}, quests:{}, courseGrants:isOwner && window.__shared ? {'grant:guide:bob':{id:'grant:guide:bob',courseId:'course:guide',recipientId:'acct-bob',ownerId:'acct-owner',capability:'learn'}}:{}, enrollments:visible ? {[profile.id === 'acct-bob' ? 'enrollment:bob' : 'enrollment:owner']:{id:'enrollment',courseId:'course:guide',profileId:profile.id}}:{}, submissions:{}, evidence:{}, events:[] }; return { accountId:profile.id, workspace:'school', actor:profile, permissions:{admin:isOwner,author:isOwner,learner:true,analytics:isOwner,grade:isOwner}, courseCapabilities:visible ? {'course:guide':{learn:true,edit:isOwner,owner:isOwner,author:isOwner}}:{}, recipientOptions:isOwner ? [{accountId:'acct-bob',username:'bob'}]:[], state, projection:{eventCount:window.__complete ? 1:0,learners:{[profile.id]:progress()},leaderboard:[],courses:{}} }; };
  window.__actor='acct-owner'; window.__shared=false; window.__revoked=false; window.__complete=false; window.__revision=1;
  window.__api = async (path, options = {}) => { if (!options.method || options.method === 'GET') return snapshot(); const body = JSON.parse(options.body || '{}'); const type = body.type; if (type === 'course.share') { window.__shared=true; window.__revision++; return {...snapshot(), result:{shareToken:'share-guide'}}; } if (type === 'course.accept_share') { window.__shared=true; window.__revision++; return {...snapshot(), result:{accepted:true}}; } if (type === 'activity.complete') { window.__complete=true; window.__revision++; return {...snapshot(), result:{completed:true}}; } if (type === 'progress.reset') { window.__complete=false; window.__revision++; return {...snapshot(), result:{reset:true}}; } if (type === 'course.revoke_share') { window.__revoked=true; window.__shared=false; window.__revision++; return {...snapshot(), result:{revoked:true}}; } return {...snapshot(), result:{}}; };
  window.__feature = createTreeHouseFeature({ h, api: window.__api, setStatus: text => { window.__status = text; }, renderMarkdown: text => document.createTextNode(text), openDocument() {} });
  window.__feature.loadState(); await window.__feature.render(document.querySelector('#treehouse'));
  window.__switchActor = async actor => { window.__actor=actor; window.__feature.suspendScope(); window.__feature.loadState(); await window.__feature.render(document.querySelector('#treehouse')); };
} catch (error) { window.__renderError = error.stack || String(error); } })();
</script></body></html>`;

test('real browser completes share, learning, badge, reset, revoke, and restart journey', async () => {
  await withCopalBrowser({ page }, async ({ evaluate, until }) => {
    await until("window.__renderError || document.querySelector('button')?.textContent === 'Learner'");
    assert.equal(await evaluate('window.__renderError'), undefined);
    await evaluate("[...document.querySelectorAll('button')].find(button => button.textContent === 'Admin').click()");
    await evaluate("[...document.querySelectorAll('button')].find(button => button.textContent === 'Open').click()");
    await evaluate("[...document.querySelectorAll('button')].find(button => button.textContent === 'Share').click()");
    await until("document.querySelector('dialog[open] select')");
    await evaluate("document.querySelector('dialog[open] button.primary').click()");
    await until("window.__shared === true && !document.querySelector('dialog[open]')");
    await evaluate("window.__switchActor('acct-bob')");
    await until("document.querySelector('button')?.textContent === 'Learner'");
    await evaluate("[...document.querySelectorAll('button')].find(button => button.textContent === 'Open').click()");
    await evaluate("[...document.querySelectorAll('button')].find(button => button.textContent === 'Mark complete').click()");
    await until("document.body.textContent.includes('Badges') && document.body.textContent.includes('1')");
    assert.equal(await evaluate('window.__complete'), true);
    await evaluate("[...document.querySelectorAll('button')].find(button => button.textContent === 'Reset my progress').click()");
    await until("document.querySelector('#styled-confirm-overlay:not(.hidden)')");
    assert.match(await evaluate("document.querySelector('#styled-confirm-msg').textContent"), /1 visible course/);
    await evaluate("document.querySelector('#styled-confirm-ok').click()");
    await until("window.__complete === false");
    assert.deepEqual(await evaluate("[...document.querySelectorAll('.copal-treehouse-summary strong')].map(node => node.textContent)"), ['0', '0', '0 days', '0']);
    await evaluate("window.__switchActor('acct-owner')");
    await until("document.querySelector('button')?.textContent === 'Learner'");
    await evaluate("[...document.querySelectorAll('button')].find(button => button.textContent === 'Admin').click(); [...document.querySelectorAll('button')].find(button => ['Open','Hide'].includes(button.textContent))?.click()");
    await until("[...document.querySelectorAll('button')].some(button => button.textContent === 'Revoke acct-bob')");
    await evaluate("[...document.querySelectorAll('button')].find(button => button.textContent === 'Revoke acct-bob').click()");
    await until("document.querySelector('#styled-confirm-overlay:not(.hidden)')");
    assert.match(await evaluate("document.querySelector('#styled-confirm-msg').textContent"), /Revoke .* access/);
    await evaluate("document.querySelector('#styled-confirm-ok').click()");
    await until("window.__revoked === true");
    await evaluate("window.__switchActor('acct-bob')");
    await until("document.querySelector('button')?.textContent === 'Learner'");
    assert.equal(await evaluate('document.body.textContent.includes("No published courses are available.")'), true);
  });
});
