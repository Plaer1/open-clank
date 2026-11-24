// A saved investigation is an owner-authored Lore-backed note. Its original
// selectors are reauthorized by the History comparator whenever opened.
const SCHEMA = 'open-clank.code-investigation/v1';
const MAX_WINDOWS = 4;
const MAX_ROWS_PER_WINDOW = 20;
const MAX_DESCRIPTION_BYTES = 16384;
const SHA = /^[a-f0-9]{64}$/;
const FILE = /^file:[a-f0-9]{64}$/;
const MILLIS = /^(0|[1-9][0-9]{0,19})$/;
const LOCATOR = /^oc-history-action:v[12]:[A-Za-z0-9_-]{1,4075}$/;
const RECEIPT = ['workspace_id','registration_id','source_snapshot_id','checkout_id','head_generation','admission_epoch','policy_generation','config_fingerprint'];
const identity = value => typeof value === 'string' && value.length > 0 && value.length <= 256;
const millis = value => value===null || typeof value === 'string' && MILLIS.test(value) && BigInt(value)<=18446744073709551615n;

function validReceipt(value) {
  return value && typeof value === 'object' && Object.keys(value).length === RECEIPT.length && RECEIPT.every(key=>
    ['admission_epoch','policy_generation'].includes(key) ? Number.isSafeInteger(value[key]) && value[key]>=0 : identity(value[key]));
}

export function buildRecordedInvestigation(pages, receipt) {
  if (!validReceipt(receipt) || !Array.isArray(pages) || pages.length<1 || pages.length>MAX_WINDOWS) throw new Error('Invalid activity windows');
  const members=[], selectors=new Map(), windows=[];
  let revision=null;
  for (const page of pages) {
    const projection=page?.projection, scope=projection?.graph_scope;
    if (page?.version!==1 || page.coverage!=='partial_recorded_activity' || projection?.coverage!==page.coverage
        || scope?.workspace_id!==receipt.workspace_id || scope.checkout_id!==receipt.checkout_id || scope.policy_generation!==receipt.policy_generation
        || !Number.isSafeInteger(scope.workspace_revision) || scope.workspace_revision<0 || (revision!==null && revision!==scope.workspace_revision)
        || !Array.isArray(page.items) || page.items.length>MAX_ROWS_PER_WINDOW) throw new Error('Activity scope changed');
    revision=scope.workspace_revision;
    const window=[];
    for (const row of page.items) {
      if (row?.state!=='complete' || !FILE.test(row.code_resource_id) || !SHA.test(row.witness_sha256)
          || !identity(row.action_id) || !millis(row.recorded_millis) || !LOCATOR.test(row.locator)) throw new Error('Invalid activity row');
      if (!selectors.has(row.locator)) {
        selectors.set(row.locator,members.length);
        members.push({locator:row.locator});
      }
      window.push([selectors.get(row.locator),row.action_id,row.code_resource_id,row.witness_sha256,row.recorded_millis]);
    }
    windows.push(window);
  }
  if (!members.length) throw new Error('No recorded evidence to save');
  const manifest={schema:SCHEMA,receipt:Object.fromEntries(RECEIPT.map(key=>[key,receipt[key]])),revision,coverage:'partial_recorded_activity',windows};
  const description=JSON.stringify(manifest);
  if (new TextEncoder().encode(description).length>MAX_DESCRIPTION_BYTES) throw new Error('Activity evidence exceeds note budget');
  if (!replayRecordedInvestigation({description,members})) throw new Error('Conflicting activity evidence');
  return {description,members};
}

export function replayRecordedInvestigation(value) {
  if (typeof value?.description!=='string' || value.description.length>MAX_DESCRIPTION_BYTES || !value.description.startsWith(`{"schema":"${SCHEMA}"`)) return null;
  try {
    const manifest=JSON.parse(value.description), members=value.members;
    const {receipt,revision,windows}=manifest;
    if (manifest.schema!==SCHEMA || manifest.coverage!=='partial_recorded_activity' || Object.keys(manifest).length!==5
        || !Number.isSafeInteger(revision) || revision<0 || !validReceipt(receipt) || !Array.isArray(windows) || !windows.length || windows.length>MAX_WINDOWS
        || !Array.isArray(members) || !members.length || members.length>MAX_WINDOWS*MAX_ROWS_PER_WINDOW
        || members.some(member=>!LOCATOR.test(member?.locator)) || new Set(members.map(member=>member.locator)).size!==members.length) return null;
    const changes=new Map(), rows=new Map(), referenced=new Set();
    let earliest=null, latest=null;
    for (const window of windows) {
      if (!Array.isArray(window) || window.length>MAX_ROWS_PER_WINDOW) return null;
      for (const row of window) {
        if (!Array.isArray(row) || row.length!==5) return null;
        const [member,action,file,witness,time]=row;
        if (!Number.isInteger(member) || member<0 || member>=members.length || !identity(action) || !FILE.test(file) || !SHA.test(witness) || !millis(time)) return null;
        referenced.add(member);
        const key=JSON.stringify([action,file]);
        const prior=rows.get(key);
        if (prior && (prior[0]!==member || prior[3]!==witness || prior[4]!==time)) return null;
        rows.set(key,row);
        if (!changes.has(action)) changes.set(action,new Map());
        changes.get(action).set(file,member);
        if (time!==null) {
          const tick=BigInt(time);
          if (earliest===null || tick<earliest) earliest=tick;
          if (latest===null || tick>latest) latest=tick;
        }
      }
    }
    if (referenced.size!==members.length) return null;
    const pairs=new Map();
    for (const [action,files] of changes) {
      const ordered=[...files.keys()].sort();
      for (const [index,left] of ordered.entries()) {
        for (const right of ordered.slice(index+1)) {
          const key=JSON.stringify([left,right]);
          if (!pairs.has(key)) pairs.set(key,{left,right,witnesses:[]});
          pairs.get(key).witnesses.push({action,leftMember:files.get(left),rightMember:files.get(right)});
        }
      }
    }
    const recurring=[...pairs.values()].filter(pair=>pair.witnesses.length>=2).sort((a,b)=>b.witnesses.length-a.witnesses.length || a.left.localeCompare(b.left) || a.right.localeCompare(b.right));
    return {windows:windows.length,changesets:changes.size,observations:rows.size,earliest:earliest?.toString() ?? null,latest:latest?.toString() ?? null,
      cochanges:recurring.slice(0,20),omitted:Math.max(0,recurring.length-20)};
  } catch {
    return null;
  }
}
