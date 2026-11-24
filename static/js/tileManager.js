import { applyEdgeDock, clearRightDock, chatVisiblePlacementRect, inlineDesktopChat, suspendDock, resumeDock } from './modalSnap.js';
import { nextToolWindowZ } from './toolWindowZOrder.js';
import { fitTileSplits } from './tileWidths.js';
import { windowViewportBounds, observeWindowWorkspace, clampFloatingWindow, isBlockingAppletFrame } from './windowResize.js';

/**
 * tileManager.js — desktop window tiling for tool modals.
 *
 * Hooks into any modal whose `.modal-header` is dragged (each tool wires its
 * own drag; we just watch pointer moves). Shows a translucent ghost preview
 * when the cursor is near a snap zone. On release, snaps the modal-content
 * to fill that zone with a springy animation.
 *
 * Snap zones:
 *   - top strip                   → maximize
 *   - top edge                    → top half
 *   - left edge                   → left half
 *   - right edge                  → right half
 *   - bottom edge                 → bottom half
 *
 * Mobile (≤768px) is excluded — the swipe-dismiss UX takes precedence.
 *
 * Each modal-content remembers its pre-snap geometry so dragging away restores
 * the original size.
 */

const EDGE_THRESHOLD_PX = 24;     // how close to an edge counts as "near"
const TOP_FULL_STRIP_PX = 8;      // top strip → maximize

let _ghost = null;
let _activeZone = null;
let _tracking = null;
let chatDrag = null;
let chatAnchor = null;
let floatingChatZ = null;
let tileOrder = null;
const splitPreferences = {x:new Map(), y:new Map()};
const splitHandles = new Map();
const DRAG_CONTROLS = 'button, input, select, textarea, a, summary, dialog, [role="button"], [contenteditable="true"], .export-dropdown-menu';

function _isDesktop() { return window.innerWidth > 768; }

function _ensureGhost() {
  if (_ghost) return _ghost;
  _ghost = document.createElement('div');
  _ghost.id = 'tile-ghost';
  document.body.appendChild(_ghost);
  return _ghost;
}

function _hideGhost() {
  if (_ghost) _ghost.classList.remove('visible');
}

function _showGhost(rect) {
  const g = _ensureGhost();
  const zoom = g.currentCSSZoom || 1;
  g.style.left = (rect.left / zoom) + 'px';
  g.style.top = (rect.top / zoom) + 'px';
  g.style.width = (rect.width / zoom) + 'px';
  g.style.height = (rect.height / zoom) + 'px';
  g.classList.add('visible');
}

const MAXIMIZE = 'maximize';
const FULL_WORKSPACE = 'full-workspace';
const fullWorkspaceStates = new Map();
const tileGeometryProperties = [
  'position', 'left', 'top', 'width', 'height', 'max-height', 'max-width',
  'min-width', 'min-height', 'right', 'bottom', 'margin', 'transform',
];
const MIN_CHAT_WIDTH = 380;
const MIN_CHAT_HEIGHT = 240;
const MIN_APPLET_WIDTH = 240;
const MIN_APPLET_HEIGHT = 160;
const TILE_SELECTOR = '.oc-applet-frame[data-_tile-zone]';
const DOCK_SELECTOR = '.modal-left-docked, .modal-right-docked, .email-snap-left';
const ROOT_SELECTOR = '.modal, .research-overlay, .notes-pane';

function rectangle(left, top, width, height) {
  return {left, top, width, height, right:left + width, bottom:top + height};
}

function zoneRectangle(bounds, name) {
  const {left, top, width, height} = bounds;
  const halfWidth = width / 2, halfHeight = height / 2;
  switch (name) {
    case 'fullscreen': case MAXIMIZE: return bounds;
    case 'left-half': return rectangle(left,top,halfWidth,height);
    case 'right-half': return rectangle(left+halfWidth,top,halfWidth,height);
    case 'top-half': return rectangle(left,top,width,halfHeight);
    case 'bottom-half': return rectangle(left,top+halfHeight,width,halfHeight);
    case 'top-left': return rectangle(left,top,halfWidth,halfHeight);
    case 'top-right': return rectangle(left+halfWidth,top,halfWidth,halfHeight);
    case 'bottom-left': return rectangle(left,top+halfHeight,halfWidth,halfHeight);
    case 'bottom-right': return rectangle(left+halfWidth,top+halfHeight,halfWidth,halfHeight);
    default: return null;
  }
}

function _zoneForPointer(x, y) {
  const bounds = windowViewportBounds();
  const nearLeft = x <= bounds.left + EDGE_THRESHOLD_PX;
  const nearRight = x >= bounds.right - EDGE_THRESHOLD_PX;
  const nearTop = y <= bounds.top + EDGE_THRESHOLD_PX;
  const nearBottom = y >= bounds.bottom - EDGE_THRESHOLD_PX;
  let name;
  if (y <= bounds.top + TOP_FULL_STRIP_PX) name = MAXIMIZE;
  else if (nearTop) name = nearLeft ? 'top-left' : nearRight ? 'top-right' : 'top-half';
  else if (nearBottom) name = nearLeft ? 'bottom-left' : nearRight ? 'bottom-right' : 'bottom-half';
  else if (nearLeft) name = 'left-half';
  else if (nearRight) name = 'right-half';
  return name ? {name,rect:zoneRectangle(bounds,name)} : null;
}

function overlaps(a, b) {
  return a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;
}

// The requested zones share the same half/quadrant boundaries. Search the
// exact empty rectangles they define so a corner can leave a whole lane.
function largestChatPane(bounds, occupied, point) {
  let nearest = Infinity;
  const xs = [...new Set([bounds.left,bounds.right,...occupied.flatMap(rect=>[rect.left,rect.right])])].sort((a,b)=>a-b);
  const ys = [...new Set([bounds.top,bounds.bottom,...occupied.flatMap(rect=>[rect.top,rect.bottom])])].sort((a,b)=>a-b);
  let best = null;
  for (const left of xs) for (const right of xs) for (const top of ys) for (const bottom of ys) {
    const width = right-left, height = bottom-top;
    if (width < MIN_CHAT_WIDTH || height < MIN_CHAT_HEIGHT) continue;
    const pane = rectangle(left,top,width,height);
    if (occupied.some(rect=>overlaps(pane,rect))) continue;
    const distance = point ? Math.hypot(Math.max(left-point.x,0,point.x-right), Math.max(top-point.y,0,point.y-bottom)) : 0;
    if (distance < nearest || (distance === nearest &&
        (!best || width*height > best.width*best.height || (width*height === best.width*best.height && width > best.width)))) {
      nearest = distance;
      best = pane;
    }
  }
  return best;
}

function tiledWorkspace(bounds, zones, anchor = chatAnchor) {
  const tiles = zones.map(zone=>zoneRectangle(bounds,zone));
  const point = anchor && { x:bounds.left + anchor.x * bounds.width, y:bounds.top + anchor.y * bounds.height };
  const chat = largestChatPane(bounds,tiles,point);
  if (chat) return {chat,tiles};

  // Maximizing or filling every usable region still leaves a chat band.
  // Reapply the same requested zones inside the remaining applet area.
  const horizontal = bounds.width >= MIN_CHAT_WIDTH + MIN_APPLET_WIDTH
    || bounds.height < MIN_CHAT_HEIGHT + MIN_APPLET_HEIGHT;
  const zone = zones.length === 1 ? zones[0] : '';
  if (horizontal) {
    const width = Math.min(MIN_CHAT_WIDTH,Math.max(bounds.width/2,bounds.width-MIN_APPLET_WIDTH));
    const opposingSides = zones.includes('left-half') && zones.includes('right-half')
      && zones.every(name => name === 'left-half' || name === 'right-half');
    if (opposingSides && (!anchor || (anchor.x >= 1/3 && anchor.x <= 2/3))) {
      const sideWidth = (bounds.width-width)/2;
      const center = rectangle(bounds.left+sideWidth,bounds.top,width,bounds.height);
      return {chat:center, tiles:zones.map(name =>
        rectangle(name === 'left-half' ? bounds.left : center.right,bounds.top,sideWidth,bounds.height))};
    }
    const appletsOnLeft = anchor ? anchor.x > .5 : zone.includes('left');
    const left = bounds.left;
    const applets = rectangle(appletsOnLeft ? left : left+width,bounds.top,bounds.width-width,bounds.height);
    return {chat:rectangle(appletsOnLeft ? applets.right : left,bounds.top,width,bounds.height),
      tiles:zones.map(name=>zoneRectangle(applets,name))};
  }
  const height = Math.min(MIN_CHAT_HEIGHT,bounds.height-MIN_APPLET_HEIGHT);
  const appletsOnTop = anchor ? anchor.y > .5 : zone.includes('top');
  const top = bounds.top;
  const applets = rectangle(bounds.left,appletsOnTop ? top : top+height,bounds.width,bounds.height-height);
  return {chat:rectangle(bounds.left,appletsOnTop ? applets.bottom : top,bounds.width,height),
    tiles:zones.map(name=>zoneRectangle(applets,name))};
}

function visibleFrame(frame) {
  if (!frame?.isConnected || frame.closest('.hidden,[hidden],.modal-minimized') || !frame.getClientRects().length) return false;
  const style = getComputedStyle(frame);
  return style.visibility !== 'hidden' && style.display !== 'none';
}

function tileZone(frame) {
  return frame.dataset._tileZone || (_tracking?.content === frame ? _tracking.sourceZone : null);
}

function activeTiles(bounds) {
  const frames = [...document.querySelectorAll(TILE_SELECTOR)];
  // Reserve the origin slot while its applet is being dragged as a floater.
  const origin = _tracking?.sourceZone ? _tracking.content : null;
  if (origin && !frames.includes(origin)) frames.splice(_tracking.sourceIndex,0,origin);
  return frames.filter(frame => visibleFrame(frame) && !isBlockingAppletFrame(frame)
    && zoneRectangle(bounds,tileZone(frame)));
}

function dockFrames() {
  return [...document.querySelectorAll(DOCK_SELECTOR)].map(root=>({
    root,frame:root.matches('.oc-applet-frame') ? root : root.querySelector('.oc-applet-frame, .modal-content') || root,
  }));
}

function workspaceBetweenDocks(bounds, docks) {
  let {left,right,bottom} = bounds;
  for (const {root,frame} of docks) {
    if (!visibleFrame(frame) || isAppletMaximized(frame) || frame._dockSuspended || isBlockingAppletFrame(frame)) continue;
    const rect = frame.getBoundingClientRect();
    if (!overlaps(rect,bounds)) continue;
    if (root.matches('.modal-left-docked,.email-snap-left')) left = Math.max(left,rect.right);
    else right = Math.min(right,rect.left);
  }
  const dock = document.getElementById('minimized-dock');
  if (visibleFrame(dock) && dock.childElementCount) bottom = Math.min(bottom,dock.getBoundingClientRect().top-4);
  return rectangle(left,bounds.top,right-left,bottom-bounds.top);
}

function _zoneForContent(content, x, y) {
  const zone = _zoneForPointer(x,y);
  if (!zone) return null;
  const root = content.closest(ROOT_SELECTOR);
  if (root?.dataset.chatVisibleMaximize === 'true') {
    const side = zone.name.includes('left') ? 'left' : 'right';
    return {...zone,rect:chatVisiblePlacementRect(root,side,zone.name === MAXIMIZE)};
  }
  if (isBlockingAppletFrame(content)) return zone;
  const bounds = windowViewportBounds();
  const frames = activeTiles(bounds).filter(frame => frame !== content);
  frames.push(content);
  const zones = frames.map(frame => frame === content ? zone.name : tileZone(frame));
  const chat = inlineDesktopChat();
  const available = chat && workspaceBetweenDocks(bounds,dockFrames().filter(({root:peer})=>peer !== root));
  const layout = layoutTilePanes(bounds,available,zones,chatAnchor);
  return {...zone,rect:orderedTiles(layout,frames,zones,chat).tiles.at(-1)};
}

function _floatingSnapshot(content, rect = content.getBoundingClientRect()) {
  if (content._preDockSnapshot?.rect) rect = content._preDockSnapshot.rect;
  const style = content._preDockSnapshot?.style || content.style;
  return { position:'fixed', left:rect.left + 'px', top:rect.top + 'px',
    width:rect.width + 'px', height:rect.height + 'px',
    maxHeight:style.maxHeight, maxWidth:style.maxWidth,
    minWidth:style.minWidth, minHeight:style.minHeight,
    right:style.right, bottom:style.bottom,
    transform:'none', margin:'0' };
}

function placeTile(content, rect) {
  const zoom = content.currentCSSZoom || 1;
  const pixels = value => (value / zoom) + 'px';
  const geometry = {position:'fixed', left:pixels(rect.left), top:pixels(rect.top), width:pixels(rect.width),
    height:pixels(rect.height), 'max-height':pixels(rect.height), 'max-width':pixels(rect.width),
    'min-width':'0', 'min-height':'0', right:'auto', bottom:'auto', margin:'0', transform:'none'};
  // Do not tween a snapped frame across the chat while the chat changes pane.
  for (const [property,value] of Object.entries(geometry)) {
    if (content.style.getPropertyValue(property) !== value || content.style.getPropertyPriority(property) !== 'important') {
      content.style.setProperty(property,value,'important');
    }
  }
}

function _applySnap(content, rect, zoneName, previous = null) {
  const modal = content.closest(ROOT_SELECTOR);
  if (!content.classList.contains('oc-applet-frame') || isBlockingAppletFrame(content)) return;
  if (modal?.dataset.chatVisibleMaximize === 'true') {
    const side = zoneName.includes('left') ? 'left' : 'right';
    applyEdgeDock(modal,side,undefined,{maximized:zoneName === MAXIMIZE});
    return;
  }
  // Preserve the original floating restore point across dock → tile and
  // tile → tile moves. The actual dock owner releases its old reservation.
  if (!content.dataset._tilePreSnap) {
    content.dataset._tilePreSnap = JSON.stringify(previous || _floatingSnapshot(content));
  }
  if (modal?.matches(DOCK_SELECTOR)) clearRightDock(modal);
  content.dataset._tileZone = zoneName;
  modal?.dispatchEvent(new Event('openclank:tile-placement'));
  placeTile(content,rect);
  _reclampAll();
}

function _unsnap(content) {
  if (content.dataset._tileZone === 'chat-visible') {
    clearRightDock(content.closest('.modal')); return;
  }
  const pre = content.dataset._tilePreSnap;
  if (!pre) return;
  // Clear the !important snap props first — Object.assign can't override them.
  for (const property of tileGeometryProperties) content.style.removeProperty(property);
  try {
    const r = JSON.parse(pre);
    Object.assign(content.style, r);
  } catch {}
  // Keep it a fixed floating window so the restored left/top actually take
  // effect — without position:fixed the .modal flex parent re-centers it.
  if (!content.style.position) content.style.position = 'fixed';
  delete content.dataset._tilePreSnap;
  delete content.dataset._tileZone;
  content.closest(ROOT_SELECTOR)?.dispatchEvent(new Event('openclank:tile-placement'));
  clampFloatingWindow(content);
  _reclampAllThrottled();
}

function _findDragTarget(e) {
  const header = e.target.closest('.modal-header, .notes-pane-header');
  if (!header) return null;
  if (e.target.closest(DRAG_CONTROLS)) return null;
  const frame = header.closest('.oc-applet-frame');
  return frame && !isBlockingAppletFrame(frame) ? frame : null;
}

if (typeof document !== 'undefined') document.addEventListener('pointerdown', (e) => {
  if (!_isDesktop() || !e.isPrimary || e.button !== 0 || _tracking || chatDrag) return;
  const titleBar = e.target.closest('#chat-container .chat-top-bar');
  const chat = titleBar && inlineDesktopChat();
  if (chat && chat.contains(titleBar) && !e.target.closest(DRAG_CONTROLS)) {
    chatDrag = { chat, titleBar, pointerId:e.pointerId, startX:e.clientX, startY:e.clientY, anchor:null };
    return;
  }
  const content = _findDragTarget(e);
  if (!content) return;
  _activeZone = null;

  // If we're already snapped, dragging away should unsnap immediately so the
  // user can move freely.
  if (isAppletMaximized(content)) {
    _tracking = {content, pointerId:e.pointerId, startX:e.clientX, startY:e.clientY,
      fullWorkspace:fullWorkspaceStates.get(content)};
  } else if (content.dataset._tileZone) {
    // Defer slightly so pointermove threshold is met before unsnap kicks in
    const bounds = windowViewportBounds();
    const sourceIndex = activeTiles(bounds).indexOf(content);
    _tracking = {content, pointerId:e.pointerId, startX:e.clientX, startY:e.clientY, willUnsnap:true,
      previous:JSON.parse(content.dataset._tilePreSnap || 'null'),sourceIndex,
      sourceZone:sourceIndex >= 0 ? content.dataset._tileZone : null};
  } else {
    _tracking = { content, pointerId:e.pointerId, startX:e.clientX, startY:e.clientY, willUnsnap:false, previous:_floatingSnapshot(content) };
  }
});

if (typeof document !== 'undefined') document.addEventListener('pointermove', (e) => {
  if (chatDrag?.pointerId === e.pointerId) {
    const drag = chatDrag;
    if (inlineDesktopChat() !== drag.chat) { finishChatDrag(e, false); return; }
    if (!drag.anchor && Math.hypot(e.clientX-drag.startX,e.clientY-drag.startY) < 6) return;
    if (!drag.titleBar.hasPointerCapture(e.pointerId)) drag.titleBar.setPointerCapture(e.pointerId);
    drag.anchor = chatDropAnchor(e);
    if (!drag.anchor) { _hideGhost(); return; }
    const swap = tileSwapAt(drag.chat,e.clientX,e.clientY);
    const rect = swap?.rect || chatAndTileLayout(drag.anchor).chat;
    if (rect) _showGhost(rect);
    else _hideGhost();
    return;
  }
  if (_tracking?.pointerId !== e.pointerId || !_isDesktop()) return;
  const dx = e.clientX - _tracking.startX;
  const dy = e.clientY - _tracking.startY;
  if (Math.hypot(dx, dy) < 6) return;

  // Unsnap on first significant move.
  if (_tracking.fullWorkspace && isAppletMaximized(_tracking.content)) {
    const content = _tracking.content;
    _tracking.previous = _tracking.fullWorkspace.previous;
    leaveFullWorkspace(content, {resume:false});
    if (content.dataset._tileZone) _unsnap(content);
  }
  if (_tracking.willUnsnap) {
    _unsnap(_tracking.content);
    _tracking.willUnsnap = false;
  }

  // Detect snap zone under cursor
  const zone = tileSwapAt(_tracking.content,e.clientX,e.clientY) || _zoneForContent(_tracking.content,e.clientX,e.clientY);
  if (zone) {
    _showGhost(zone.rect);
    _activeZone = zone;
  } else {
    _hideGhost();
    _activeZone = null;
  }
});

function finishChatDrag(event, commit) {
  const drag = chatDrag;
  if (drag?.pointerId !== event.pointerId) return;
  chatDrag = null;
  _hideGhost();
  if (!commit || !drag.anchor || inlineDesktopChat() !== drag.chat) return;
  const anchor = chatDropAnchor(event);
  if (!anchor) return;
  const swap = tileSwapAt(drag.chat,event.clientX,event.clientY);
  if (swap) swapTiledPanes(swap);
  else {
    chatAnchor = anchor;
    tileOrder = null;
    floatingChatZ = nextToolWindowZ();
    _reclampAll();
  }

  // A tap on the title still opens its menu. A drag ends with a click too.
  const swallow = click => { click.preventDefault(); click.stopPropagation(); };
  drag.titleBar.addEventListener('click', swallow, {capture:true,once:true});
  setTimeout(() => drag.titleBar.removeEventListener('click',swallow,true),50);
}

if (typeof document !== 'undefined') {
  document.addEventListener('pointerup', event => {
    finishChatDrag(event,true);
    if (_tracking?.pointerId !== event.pointerId) return;
    const tracking = _tracking;
    const zone = _activeZone && _isDesktop() &&
      (tileSwapAt(tracking.content,event.clientX,event.clientY) || _zoneForContent(tracking.content,event.clientX,event.clientY));
    _tracking = null;
    clearPreview();
    if (zone?.target) {
      tracking.content.dataset._tilePreSnap = JSON.stringify(tracking.previous || _floatingSnapshot(tracking.content));
      tracking.content.dataset._tileZone = tracking.sourceZone;
      swapTiledPanes(zone);
    } else if (zone) _applySnap(tracking.content,zone.rect,zone.name,tracking.previous);
    else if (tracking.sourceZone) _reclampAllThrottled();
  });
  const cancelDrag = event => {
    finishChatDrag(event,false);
    if (_tracking?.pointerId !== event.pointerId) return;
    const {content,sourceZone,previous,fullWorkspace} = _tracking;
    _tracking = null;
    clearPreview();
    if (fullWorkspace && !isAppletMaximized(content) && visibleFrame(content)) {
      if (fullWorkspace.dockSide) {
        applyEdgeDock(content.closest(ROOT_SELECTOR),fullWorkspace.dockSide,undefined,
          {maximized:fullWorkspace.chatVisibleMaximized});
      } else if (fullWorkspace.zone) {
        _applySnap(content,content.getBoundingClientRect(),fullWorkspace.zone,previous);
      } else {
        restoreFullWorkspaceStyle(content,fullWorkspace.style);
      }
      enterFullWorkspace(content);
    } else if (sourceZone && !content.dataset._tileZone && visibleFrame(content)) {
      _applySnap(content,content.getBoundingClientRect(),sourceZone,previous);
    }
  };
  document.addEventListener('pointercancel',cancelDrag);
  document.addEventListener('lostpointercapture',event => finishChatDrag(event,false));
}

export function resetInlineChatPosition() {
  chatAnchor = null;
  tileOrder = null;
  floatingChatZ = null;
  _reclampAll();
}

let _chatPane = null;
let _observedFrames = [];
const layoutMutation = typeof MutationObserver !== 'undefined' ? new MutationObserver(() => _reclampAllThrottled()) : null;

function watchLayoutFrames(frames) {
  if (!layoutMutation || (frames.length === _observedFrames.length && frames.every((frame,index)=>frame === _observedFrames[index]))) return;
  layoutMutation.disconnect();
  _observedFrames = frames;
  for (const frame of frames) layoutMutation.observe(frame,{attributes:true,attributeFilter:['class','style','hidden','data-_tile-zone']});
}

function placeChat(chat, rect, floating) {
  if (_chatPane && (_chatPane !== chat || !rect)) {
    delete _chatPane.dataset.tiledWorkspace;
    delete _chatPane.dataset.chatFloating;
    _chatPane.style.removeProperty('--floating-chat-z');
    for (const key of ['left','top','width','height']) _chatPane.style.removeProperty('--tiled-chat-'+key);
    _chatPane = null;
  }
  if (!chat || !rect) return;
  for (const key of ['left','top','width','height']) {
    const value = (rect[key] / (chat.currentCSSZoom || 1))+'px';
    const property = '--tiled-chat-'+key;
    if (chat.style.getPropertyValue(property) !== value) chat.style.setProperty(property,value);
  }
  if (!chat.hasAttribute('data-tiled-workspace')) chat.dataset.tiledWorkspace = '';
  chat.toggleAttribute('data-chat-floating',floating);
  if (floating) chat.style.setProperty('--floating-chat-z',floatingChatZ);
  else chat.style.removeProperty('--floating-chat-z');
  _chatPane = chat;
}

function chatDropAnchor(event) {
  const bounds = workspaceBetweenDocks(windowViewportBounds(),dockFrames());
  if (bounds.width <= 0 || bounds.height <= 0) return null;
  const unit = (value, start, length) => Math.min(1,Math.max(0,(value-start)/length));
  const edge = event.clientX <= bounds.left + EDGE_THRESHOLD_PX ? 'left'
    : event.clientX >= bounds.right - EDGE_THRESHOLD_PX ? 'right' : null;
  return { x:unit(event.clientX,bounds.left,bounds.width), y:unit(event.clientY,bounds.top,bounds.height),edge };
}

function floatingChatRect(bounds, anchor) {
  const width = Math.min(bounds.width,Math.max(MIN_CHAT_WIDTH,Math.min(920,bounds.width*.65)));
  const height = Math.min(bounds.height,Math.max(MIN_CHAT_HEIGHT,bounds.height*.8));
  const left = bounds.left + Math.max(0,Math.min(bounds.width-width,bounds.width*anchor.x-width/2));
  const top = bounds.top + Math.max(0,Math.min(bounds.height-height,bounds.height*anchor.y-16));
  return rectangle(left,top,width,height);
}

function chatAndTileLayout(anchor, bounds = windowViewportBounds(), frames = activeTiles(bounds), docks = dockFrames()) {
  const zones = frames.map(tileZone);
  const chat = inlineDesktopChat();
  const available = chat && workspaceBetweenDocks(bounds,docks);
  const layout = layoutTilePanes(bounds,available,zones,anchor);
  return anchor === chatAnchor ? orderedTiles(layout,frames,zones,chat) : layout;
}

function orderFor(frames, zones, chat) {
  if (tileOrder?.chat !== chat || tileOrder.zones.size !== frames.length
      || !frames.every((frame,index) => tileOrder.zones.get(frame) === zones[index])) return null;
  return tileOrder.slots;
}

function orderedTiles(layout, frames, zones, chat) {
  const order = orderFor(frames,zones,layout.chat && !layout.floating ? chat : null);
  if (!order) return layout;
  const slots = new Map(frames.map((frame,index) => [frame,layout.tiles[index]]));
  if (layout.chat && chat) slots.set(chat,layout.chat);
  return {...layout,tiles:frames.map(frame => slots.get(order.get(frame))),
    chat:layout.chat ? slots.get(order.get(chat)) : null};
}

function tileSwapAt(source,x,y) {
  const bounds = windowViewportBounds();
  // A horizontal drag can stay at title-bar height. Give the actual
  // workspace perimeter to edge snapping and the pane interiors to swapping.
  if (source !== inlineDesktopChat() && Math.min(x-bounds.left,bounds.right-x,y-bounds.top,bounds.bottom-y) <= TOP_FULL_STRIP_PX) return null;
  const frames = activeTiles(bounds);
  const zones = frames.map(tileZone);
  const layout = chatAndTileLayout(chatAnchor,bounds,frames);
  const chat = layout.chat && !layout.floating ? inlineDesktopChat() : null;
  const panes = frames.map((frame,index) => [frame,layout.tiles[index]]);
  if (chat) panes.push([chat,layout.chat]);
  if (!panes.some(([pane]) => pane === source)) return null;
  const target = panes.find(([pane,rect]) => pane !== source && x >= rect.left && x < rect.right && y >= rect.top && y < rect.bottom);
  return target ? {source,target:target[0],rect:target[1],frames,zones,chat} : null;
}

function swapTiledPanes({source,target,frames,zones,chat}) {
  const panes = chat ? [...frames,chat] : frames;
  const slots = new Map(orderFor(frames,zones,chat) || panes.map(pane => [pane,pane]));
  const sourceSlot = slots.get(source);
  slots.set(source,slots.get(target));
  slots.set(target,sourceSlot);
  tileOrder = {chat,slots,zones:new Map(frames.map((frame,index) => [frame,zones[index]]))};
  _reclampAll();
}

function layoutTilePanes(bounds, available, zones, anchor) {
  // The live dock owner can briefly report an empty rectangle during restore.
  const hasSpace = available?.width > 0 && available?.height > 0;
  if (!zones.length && hasSpace && anchor) {
    if (anchor.edge) {
      const width = Math.min(available.width,Math.max(MIN_CHAT_WIDTH,available.width/2));
      const left = anchor.edge === 'right' ? available.right-width : available.left;
      return {chat:rectangle(left,available.top,width,available.height),tiles:[],floating:false};
    }
    return {chat:floatingChatRect(available,anchor),tiles:[],floating:true};
  }
  const base = hasSpace && zones.length ? tiledWorkspace(available,zones,anchor)
    : {chat:null,tiles:zones.map(zone=>zoneRectangle(bounds,zone))};
  if (!_isDesktop()) return {...base,floating:false};
  return {...fitTileSplits(base,hasSpace ? available : bounds,zones,splitPreferences,{
    width:MIN_APPLET_WIDTH, height:MIN_APPLET_HEIGHT, chatHeight:MIN_CHAT_HEIGHT,
  }),floating:false};
}

function setTileSplit(seam, value) {
  const axisPreferences = splitPreferences[seam.axis];
  let preferences = axisPreferences.get(seam.layout);
  if (!preferences) axisPreferences.set(seam.layout,preferences = new Map());
  const position = Math.max(seam.min,Math.min(seam.max,value));
  preferences.delete(seam.edge);
  preferences.set(seam.edge,(position-seam.origin)/seam.extent);
  _reclampAllThrottled();
}

function makeSplitHandle(axis) {
  const column = axis === 'x';
  const coordinate = column ? 'clientX' : 'clientY';
  const dimension = column ? 'width' : 'height';
  const node = document.createElement('div');
  node.className = 'tile-split-handle';
  node.dataset.axis = axis;
  node.tabIndex = 0;
  node.setAttribute('role','separator');
  node.setAttribute('aria-orientation',column ? 'vertical' : 'horizontal');
  node.setAttribute('aria-label','Tile ' + dimension);
  node.title = 'Drag to resize tiles. Double-click to reset ' + dimension + '.';
  const handle = {node,seam:null,drag:null};
  node.addEventListener('pointerdown',event => {
    if (!event.isPrimary || event.button !== 0) return;
    event.preventDefault();
    node.focus({preventScroll:true});
    handle.drag?.abort();
    const drag = handle.drag = new AbortController();
    const offset = event[coordinate]-handle.seam.position;
    const move = next => {
      if (next.pointerId === event.pointerId) setTileSplit(handle.seam,next[coordinate]-offset);
    };
    const end = next => {
      if (next.pointerId !== event.pointerId) return;
      if (next.type === 'pointerup') move(next);
      drag.abort();
    };
    const options = {signal:drag.signal};
    document.addEventListener('pointermove',move,options);
    document.addEventListener('pointerup',end,options);
    document.addEventListener('pointercancel',end,options);
    window.addEventListener('blur',() => drag.abort(),options);
    node.setPointerCapture(event.pointerId);
  });
  node.addEventListener('keydown',event => {
    const {position,min,max} = handle.seam;
    const step = event.shiftKey ? 48 : 12;
    const previous = column ? 'ArrowLeft' : 'ArrowUp';
    const following = column ? 'ArrowRight' : 'ArrowDown';
    const next = {[previous]:position-step,[following]:position+step,Home:min,End:max}[event.key];
    if (!Number.isFinite(next)) return;
    event.preventDefault();
    setTileSplit(handle.seam,next);
  });
  node.addEventListener('dblclick',() => {
    const {layout,edge} = handle.seam;
    splitPreferences[axis].get(layout)?.delete(edge);
    _reclampAllThrottled();
  });
  document.body.append(node);
  return handle;
}

function syncSplitHandles(seams, frames) {
  const active = new Set();
  let layer = 250;
  for (const frame of frames) {
    const root = frame.closest(ROOT_SELECTOR);
    layer = Math.max(layer,Number.parseInt(getComputedStyle(root || frame).zIndex,10) || 0);
  }
  for (const seam of seams) {
    const key = seam.axis + ':' + seam.layout + ':' + seam.edge;
    active.add(key);
    if (!splitHandles.has(key)) splitHandles.set(key,makeSplitHandle(seam.axis));
    const handle = splitHandles.get(key);
    handle.seam = seam;
    const {node} = handle;
    const zoom = node.currentCSSZoom || 1;
    const column = seam.axis === 'x';
    node.style.left = ((column ? seam.position : seam.across) / zoom) + 'px';
    node.style.top = ((column ? seam.across : seam.position) / zoom) + 'px';
    node.style[column ? 'height' : 'width'] = (seam.span / zoom) + 'px';
    node.style.zIndex = String(layer+1);
    for (const [key,value] of Object.entries({min:seam.min,now:seam.position,max:seam.max})) {
      node.setAttribute('aria-value'+key,String(Math.round(value-seam.origin)));
    }
  }
  for (const [key,handle] of splitHandles) {
    if (!active.has(key)) {
      handle.drag?.abort();
      handle.node.remove();
      splitHandles.delete(key);
    }
  }
}

function _reclampAll() {
  const bounds = windowViewportBounds();
  const frames = activeTiles(bounds);
  const docks = dockFrames();
  const dockBar = document.getElementById('minimized-dock');
  const tiled = [...document.querySelectorAll(TILE_SELECTOR)];
  watchLayoutFrames([...new Set([...tiled,...tiled.map(frame=>frame.closest(ROOT_SELECTOR)),
    ...docks.flatMap(({root,frame})=>[root,frame]),dockBar])].filter(Boolean));

  const layout = chatAndTileLayout(chatAnchor,bounds,frames,docks);
  const fullBounds = fullWorkspaceStates.size && windowViewportBounds({gutter:0});
  for (const frame of fullWorkspaceStates.keys()) {
    if (visibleFrame(frame)) placeTile(frame,fullBounds);
    else if (!frame.closest('.modal-minimized')) leaveFullWorkspace(frame,{resume:false,reflow:false});
  }
  for (const [index,frame] of frames.entries()) {
    if (frame.dataset._tileZone) placeTile(frame,layout.tiles[index]);
  }
  placeChat(inlineDesktopChat(),layout.chat,layout.floating);
  syncSplitHandles(layout.seams || [],frames);
}

let _reclampPending = false;
function _reclampAllThrottled() {
  if (_reclampPending) return;
  _reclampPending = true;
  requestAnimationFrame(() => {
    try { _reclampAll(); } finally { _reclampPending = false; }
  });
}

if (typeof window !== 'undefined') {
  for (const event of ['resize','chat-workspace-change','odysseus:modal-opened','odysseus:modal-closed',
    'odysseus:modal-minimized','odysseus:modal-restored']) window.addEventListener(event,_reclampAllThrottled);
  window.addEventListener('odysseus:modal-closed', event => {
    const root = event.detail?.modal;
    const frame = root?.matches('.oc-applet-frame') ? root : root?.querySelector(':scope > .oc-applet-frame');
    if (isAppletMaximized(frame)) leaveFullWorkspace(frame,{resume:false,reflow:false});
  });
}

if (typeof document !== 'undefined') {
  observeWindowWorkspace(_reclampAllThrottled);
  const afterAnimation = event => { if (_observedFrames.includes(event.target)) _reclampAllThrottled(); };
  document.addEventListener('transitionend',afterAnimation);
  document.addEventListener('animationend',afterAnimation);
  const observeWorkspaceLifecycle = () => {
    const observer = new MutationObserver(_reclampAllThrottled);
    observer.observe(document.body,{attributes:true,attributeFilter:['class'],childList:true});
    observer.observe(document.documentElement,{attributes:true,attributeFilter:['style']});
  };
  if (document.body) observeWorkspaceLifecycle();
  else document.addEventListener('DOMContentLoaded',observeWorkspaceLifecycle,{once:true});
}

// ── Public API for other drag sources (e.g. dragging a minimized dock chip
// to a screen edge) to reuse the same snap zones + ghost preview + apply. ──

// Show the snap-zone ghost for a point and return the zone (or null).
export function previewZoneAt(x, y, target = null) {
  if (!_isDesktop()) { _hideGhost(); _activeZone = null; return null; }
  const content = target && target.querySelector
    ? (target.matches?.('.oc-applet-frame') ? target : target.querySelector('.oc-applet-frame, .modal-content, .research-pane') || target)
    : null;
  const zone = content ? _zoneForContent(content, x, y) : _zoneForPointer(x, y);
  if (zone) { _showGhost(zone.rect); _activeZone = zone; }
  else { _hideGhost(); _activeZone = null; }
  return zone;
}

export function clearPreview() {
  _hideGhost();
  _activeZone = null;
}

export function _zoneForPointerForTests(x, y) {
  return _zoneForPointer(x, y);
}

export function _zoneForContentForTests(content, x, y) {
  return _zoneForContent(content, x, y);
}

export function isAppletMaximized(content) {
  return content?.dataset._tileZone === FULL_WORKSPACE;
}

function restoreFullWorkspaceStyle(content, style) {
  for (const [property,value,priority] of style) content.style.setProperty(property,value,priority);
}

function enterFullWorkspace(content) {
  const root = content.closest(ROOT_SELECTOR);
  const zone = content.dataset._tileZone;
  const previous = JSON.parse(content.dataset._tilePreSnap || 'null') || _floatingSnapshot(content);
  const chatVisibleMaximized = content._chatVisibleMaximized === true;
  const dockSide = root.matches('.modal-left-docked, .modal-right-docked') ? suspendDock(root) : null;
  const properties = [...tileGeometryProperties.filter(property => property !== 'margin'),
    'margin-top', 'margin-right', 'margin-bottom', 'margin-left', 'transition', 'box-sizing'];
  const style = properties.map(property =>
    [property,content.style.getPropertyValue(property),content.style.getPropertyPriority(property)]);
  fullWorkspaceStates.set(content,{zone,previous,dockSide,chatVisibleMaximized,style});
  content.dataset._tileZone = FULL_WORKSPACE;
  content.style.setProperty('box-sizing','border-box','important');
  content.style.setProperty('transition','none','important');

  // The measured workspace starts after navigation and spans behind the
  // ordinary tiles and inline chat. Raise its owning window as a unit.
  const owner = root.closest('.notes-pane-backdrop') || root;
  // Tile and dock divider handles sit one level above their windows.
  const z = nextToolWindowZ({exclude:owner,current:getComputedStyle(owner).zIndex,
    floor:Math.max(300,floatingChatZ || 0)}) + 1;
  owner.style.setProperty('z-index',String(z),'important');
  root.dispatchEvent(new Event('openclank:tile-placement'));
  _reclampAll();
}

function leaveFullWorkspace(content, {resume = true, reflow = true} = {}) {
  const state = fullWorkspaceStates.get(content);
  if (!state) return;
  fullWorkspaceStates.delete(content);
  restoreFullWorkspaceStyle(content,state.style);
  if (state.zone) content.dataset._tileZone = state.zone;
  else delete content.dataset._tileZone;

  const root = content.closest(ROOT_SELECTOR);
  if (state.dockSide && root) {
    if (resume) resumeDock(root);
    else clearRightDock(root);
  }
  if (resume && !content.dataset._tileZone) clampFloatingWindow(content);
  root?.dispatchEvent(new Event('openclank:tile-placement'));
  if (reflow) _reclampAll();
}

export function maximizeApplet(content) {
  if (!content?.classList.contains('oc-applet-frame') || isBlockingAppletFrame(content)
      || isAppletMaximized(content) || !visibleFrame(content)) return;
  enterFullWorkspace(content);
}

export function restoreAppletPlacement(content) {
  if (isAppletMaximized(content)) leaveFullWorkspace(content);
}

export function toggleAppletMaximize(content) {
  if (isAppletMaximized(content)) restoreAppletPlacement(content);
  else maximizeApplet(content);
}

// Snap a modal (its .modal-content) into a previously-detected zone.
export function snapModalToZone(modal, zone) {
  if (!modal || !zone) return;
  const content = modal.querySelector ? (modal.querySelector('.modal-content, .research-pane') || modal) : modal;
  if (!content) return;
  const name = zone.name === 'fullscreen' ? MAXIMIZE : zone.name;
  const rect = zoneRectangle(windowViewportBounds(),name);
  if (rect && _isDesktop()) _applySnap(content,rect,name);
}

export {};
