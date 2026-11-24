// Public metadata selection only; no download or credential authority.
export function createHubDetailController({fetchImpl=fetch, getOwner, isVisible, render}) {
  let epoch=0, controller=null;
  function clear() { epoch++; controller?.abort(); controller=null; render(null); }
  async function select(repoID) {
    clear();
    const owner=String(getOwner() || ''), selected=String(repoID || ''), captured=epoch;
    if (!owner || !/^[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+$/.test(selected)) throw new Error('Public repository is unavailable.');
    controller=new AbortController();
    const owned=controller;
    const valid=()=>captured===epoch && owner===String(getOwner() || '') && isVisible() && !owned.signal.aborted;
    try {
      const response=await fetchImpl(`/api/cookbook/hf-public-manifest?repo_id=${encodeURIComponent(selected)}`,{credentials:'same-origin',signal:owned.signal});
      if (!response.ok) throw new Error('Public Hub metadata is unavailable.');
      const data=await response.json();
      if (!valid()) return false;
      if (data?.repoID !== selected || !/^[a-f0-9]{40}$/.test(data.revision || '') || !Array.isArray(data.files)
          || data.files.length > 100 || !['partial','unknown'].includes(data.coverage?.state)
          || data.coverage?.returned !== data.files.length || (data.coverage.omitted != null && (!Number.isSafeInteger(data.coverage.omitted) || data.coverage.omitted < 0))
          || data.files.some(file => !file || typeof file.path !== 'string' || file.path.length > 1024
            || (file.size != null && (!Number.isSafeInteger(file.size) || file.size < 0))
            || (file.blobID != null && !/^[a-f0-9]{40}$/.test(file.blobID))
            || (file.lfsSHA256 != null && !/^[a-f0-9]{64}$/.test(file.lfsSHA256)))) throw new Error('Public Hub metadata is invalid.');
      render(data); return true;
    } catch (error) {
      if (!valid()) return false;
      render({error:'Public Hub metadata is unavailable.'}); return false;
    }
  }
  return {select,clear};
}
