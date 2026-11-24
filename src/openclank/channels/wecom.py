"""WeCom AI Bot long connection: bounded text and outbound file adapter.

Protocol source: WecomTeam/wecom-aibot-python-sdk aibot/ws.py, client.py.
No vendor code or dependency. Group mention interpretation is not implemented; no live service/account qualification is implied.
"""
from __future__ import annotations
import asyncio
import contextvars
import json
import uuid
from typing import Callable
from src.openclank.channels.base import Attachment, Inbound, Limits
from src.openclank.channels.ws_client import WebSocket, reconnecting

_REQUEST = contextvars.ContextVar('wecom_callback_request', default=None)
URL = 'wss://openws.work.weixin.qq.com'

class WeComFatal(Exception): pass

class WeComAdapter:
    name = 'wecom'
    # 5000 Unicode code points are at most 20000 UTF-8 bytes, under 20480.
    limits = Limits(max_text=5000, max_file_bytes=10 * 1024 * 1024, send_gap_seconds=.3)
    def __init__(self, bot_id: Callable[[],str], secret: Callable[[],str], *, connect=WebSocket.connect, sleep=asyncio.sleep):
        self._bot_id, self._secret, self._connect, self._sleep = bot_id, secret, connect, sleep
        self._ws = None; self._pending = {}; self._tasks = set(); self._send_lock = asyncio.Lock()
    def __repr__(self): return 'WeComAdapter()'
    async def run(self, on_inbound):
        await reconnecting(lambda:self._connect(URL),lambda ws:self._session(ws,on_inbound),sleep=self._sleep,fatal=lambda exc:isinstance(exc,WeComFatal))
    async def _send(self, frame):
        if self._ws is None: raise RuntimeError('WeCom connection unavailable')
        async with self._send_lock: await self._ws.send_text(json.dumps(frame,separators=(',',':')))
    async def _request(self, cmd, req_id, body=None):
        if req_id in self._pending or len(self._pending)>=64: raise RuntimeError('WeCom request unavailable')
        future=asyncio.get_running_loop().create_future(); self._pending[req_id]=future
        try:
            frame={'cmd':cmd,'headers':{'req_id':req_id}}
            if body is not None: frame['body']=body
            await self._send(frame); result=await asyncio.wait_for(future,10)
            if result.get('errcode') != 0: raise RuntimeError('WeCom request rejected')
            return result
        finally: self._pending.pop(req_id,None)
    async def _session(self, ws, on_inbound):
        self._ws=ws; auth='aibot_subscribe_'+uuid.uuid4().hex
        await self._send({'cmd':'aibot_subscribe','headers':{'req_id':auth},'body':{'bot_id':self._bot_id(),'secret':self._secret()}})
        try:
            response=json.loads(await asyncio.wait_for(ws.recv(),10))
            if (response.get('headers') or {}).get('req_id') != auth or response.get('errcode') != 0: raise WeComFatal('WeCom authentication rejected')
            heartbeat=asyncio.create_task(self._heartbeat(ws))
            try:
                while True:
                    frame=json.loads(await ws.recv()); req=(frame.get('headers') or {}).get('req_id')
                    if isinstance(req,str) and req in self._pending and 'errcode' in frame:
                        future=self._pending[req]
                        if not future.done(): future.set_result(frame)
                        continue
                    if frame.get('cmd') != 'aibot_msg_callback' or not isinstance(req,str) or not req or len(req)>256: continue
                    inbound=self._parse(frame.get('body'))
                    if inbound is None or len(self._tasks)>=32: continue
                    async def handle(item=inbound,request_id=req):
                        token=_REQUEST.set(request_id)
                        try: await on_inbound(item)
                        finally: _REQUEST.reset(token)
                    task=asyncio.create_task(handle());self._tasks.add(task)
                    def finished(done):
                        self._tasks.discard(done)
                        if not done.cancelled(): done.exception()
                    task.add_done_callback(finished)
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat,return_exceptions=True)
                for task in tuple(self._tasks): task.cancel()
                if self._tasks: await asyncio.gather(*self._tasks,return_exceptions=True)
                self._tasks.clear()
        finally:
            self._ws=None
            for future in self._pending.values():
                if not future.done(): future.cancel()
            self._pending.clear()
    async def _heartbeat(self, ws):
        while True:
            await self._sleep(30)
            try: await self._request('ping','ping_'+uuid.uuid4().hex)
            except Exception:
                await ws.close(4000,'heartbeat unavailable'); return
    def _parse(self, body):
        if not isinstance(body,dict): return None
        chat_type=body.get('chattype'); sender=(body.get('from') or {}).get('userid'); mid=body.get('msgid'); kind=body.get('msgtype')
        if chat_type not in ('single','group') or not all(isinstance(v,str) and v and len(v)<=512 for v in (sender,mid)): return None
        chat=sender if chat_type=='single' else body.get('chatid')
        if not isinstance(chat,str) or not chat or len(chat)>512:return None
        text=''; attachments=()
        if kind=='text':
            text=(body.get('text') or {}).get('content')
            if not isinstance(text,str) or not text or len(text)>100000:return None
        elif kind in ('image','file'):
            media=body.get(kind)
            if not isinstance(media,dict): return None
            url,key=media.get('url'),media.get('aeskey')
            if not isinstance(url,str) or not isinstance(key,str) or not url or not key:return None
            async def fetch():
                from src.openclank.channels.wecom_media import fetch_attachment
                return await fetch_attachment(url,key)
            attachments=(Attachment('wecom-attachment','application/x-wecom-encrypted',None,fetch),)
        else: return None
        # Do not infer a group mention from an unverified callback shape.
        return Inbound(channel='wecom',conv_id=chat,sender_id=sender,text=text,message_id=mid,is_direct=chat_type=='single',mentioned=False,attachments=attachments)
    async def send_text(self,conv_id,thread_id,text):
        request_id=_REQUEST.get()
        if not request_id or len(text.encode('utf-8'))>20480: raise ValueError('WeCom reply unavailable')
        await self._request('aibot_respond_msg',request_id,{'msgtype':'stream','stream':{'id':'stream_'+uuid.uuid4().hex,'finish':True,'content':text}})
    async def send_file(self,conv_id,thread_id,name,data,mime):
        import base64, hashlib, posixpath
        request_id=_REQUEST.get()
        if not request_id or not isinstance(data,bytes) or not 0 < len(data) <= self.limits.max_file_bytes:
            raise ValueError('WeCom file unavailable')
        filename=posixpath.basename(str(name).replace('\\','/'))[:200] or 'file'
        kind='image' if mime in ('image/png','image/jpeg','image/gif','image/webp') else 'file'
        chunk_size=512*1024; chunks=(len(data)+chunk_size-1)//chunk_size
        init=await self._request('aibot_upload_media_init','upload_init_'+uuid.uuid4().hex,
            {'type':kind,'filename':filename,'total_size':len(data),'total_chunks':chunks,
             'md5':hashlib.md5(data,usedforsecurity=False).hexdigest()})
        upload=(init.get('body') or {}).get('upload_id')
        if not isinstance(upload,str) or not upload or len(upload)>512: raise RuntimeError('WeCom upload unavailable')
        for index in range(chunks):
            part=data[index*chunk_size:(index+1)*chunk_size]
            await self._request('aibot_upload_media_chunk','upload_chunk_'+uuid.uuid4().hex,
                {'upload_id':upload,'chunk_index':index,'base64_data':base64.b64encode(part).decode('ascii')})
        done=await self._request('aibot_upload_media_finish','upload_finish_'+uuid.uuid4().hex,{'upload_id':upload})
        media=(done.get('body') or {}).get('media_id')
        if not isinstance(media,str) or not media or len(media)>512: raise RuntimeError('WeCom upload unavailable')
        await self._request('aibot_respond_msg',request_id,{'msgtype':kind,kind:{'media_id':media}})
    async def aclose(self):
        if self._ws is not None: await self._ws.close()
