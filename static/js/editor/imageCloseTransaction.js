/** Prepare all image close choices, then atomically validate before any scene closes. */
export async function closeImageTransaction({images, choose, resolveOthers, isCurrent,
  stageLayout = () => ({rollback(){},commit(){}}), onCommitted = () => {}, onBlocked = () => {}}) {
  const prepared = [];
  for (const image of images) {
    const choice = await choose(image);
    if (!choice || choice === 'cancel' || !isCurrent()) return false;
    const token = await image.prepareClose(choice);
    if (!isCurrent()) return false;
    if (token?.status === 'cancelled') return false;
    if (token?.status !== 'prepared') { onBlocked(image,token?.reason); return false; }
    prepared.push({image, token, choice});
  }
  if (!await resolveOthers() || !isCurrent()) return false;
  for (const {image,token} of prepared) if (!image.validateClose(token)) { onBlocked(image); return false; }
  const layout=stageLayout(prepared);
  try {
    for (const {image,token} of prepared) if (image.commitClose(token) === false) { layout.rollback(); onBlocked(image); return false; }
  } catch (error) {
    layout.rollback();
    throw error;
  }
  if (!isCurrent()) { layout.rollback(); return false; }
  layout.commit();
  for (const {image,choice} of prepared) onCommitted(image,choice);
  return true;
}

/** Capture exact image views; only the last leaves prepare session closure. */
export function captureImageCloseTargets({leaves, selected, imageMap}) {
  const shape = items => items.filter(leaf=>leaf.image).map(leaf=>[leaf.id,leaf.docId,leaf.image.viewId,leaf.image.sessionId]);
  const signature=JSON.stringify(shape(leaves)), selectedIds=new Set(selected.map(item=>item.id));
  const captured=leaves.filter(leaf=>leaf.image && selectedIds.has(leaf.id)).map(leaf=>{
    const image=imageMap?.get(leaf.docId), view=image?.views?.get(leaf.image.viewId);
    return {leaf,image,view,runtime:view?.runtime || null,attempt:view?.mountAttempt};
  });
  const last=captured.filter(({leaf})=>!leaves.some(other=>other.image?.sessionId===leaf.image.sessionId && !selectedIds.has(other.id)));
  const isPlaceholder=({leaf,image})=>{
    if (!leaf.image.recovery) return false;
    if (!image) return true;
    const session=image.session;
    return session.sessionId===leaf.image.sessionId && !image.initialAttachment && !image.initialProvenance && !image.initialLoad &&
      !session.hasOpenRuntime() && !session.getSceneAuthority() && !session.getPersistenceController() &&
      !session.canCloseUninitializedBlank();
  };
  const placeholders=last.filter(isPlaceholder).map(target=>{
    const saved=target.leaf.image.recovery;
    const recovery=target.image?.session.serializeRecovery() || saved;
    return {...target,saved,recovery,recoveryIdentity:JSON.stringify(recovery)};
  });
  const placeholderIds=new Set(placeholders.map(({leaf})=>leaf.id));
  const remaining=last.filter(({leaf})=>!placeholderIds.has(leaf.id));
  const unavailable=remaining.some(({leaf,image})=>image?.session.sessionId!==leaf.image.sessionId);
  const images=[...new Set(remaining.filter(({leaf,image})=>image?.session.sessionId===leaf.image.sessionId).map(({image})=>image))].map(image=>{
    const views=last.filter(target=>target.image===image);
    const active=views.find(({runtime})=>runtime?.isEditorOpen?.() === true);
    const viewId=active?.leaf.image.viewId;
    const session=image.session;
    const canCloseUninitializedBlank=()=>!image.initialAttachment && !image.initialProvenance && !image.initialLoad && session.canCloseUninitializedBlank();
    const canCommitClose=token=>canCloseUninitializedBlank() ||
      (active?.runtime.isEditorOpen() === true && (session.recoveryStatus().state === 'ready' ||
        token?.choice === 'close_recovery' && session.getPersistenceController()?.canCloseWithRecovery()));
    return {
      entry:image, docId:views[0].leaf.docId, leafIds:views.map(({leaf})=>leaf.id),
      canCloseUninitializedBlank,
      isOpen:()=>active?.runtime.isEditorOpen() === true,
      isDirty:()=>session.isDirty(),
      getPersistenceController:()=>session.getPersistenceController(),
      prepareClose:choice=>session.prepareClose(choice,viewId),
      validateClose:token=>canCommitClose(token) && session.validateClose(token,viewId),
      commitClose:token=>canCommitClose(token) && session.commitClose(token,viewId),
    };
  });
  return Object.freeze({
    images, placeholders, unavailable, ready:!captured.some(({view})=>view?.mounting),
    isCurrent(currentLeaves,currentMap) {
      return imageMap===currentMap && signature===JSON.stringify(shape(currentLeaves)) &&
        captured.every(({leaf,image,view,runtime,attempt})=>currentMap?.get(leaf.docId)===image &&
          image?.views?.get(leaf.image.viewId)===view && (view?.runtime || null)===runtime &&
          view?.mountAttempt===attempt && !view?.mounting) &&
        placeholders.every(target=>target.leaf.image.recovery===target.saved && isPlaceholder(target) &&
          JSON.stringify(target.image?.session.serializeRecovery() || target.saved)===target.recoveryIdentity);
    },
  });
}
