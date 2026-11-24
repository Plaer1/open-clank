"""Finite four-frame sprite sheet: original pixels and reviewed annotations."""
import hashlib, io, struct
from PIL import Image

MAX_FRAME=8*1024*1024
MAX_SHEET=32*1024*1024

def _check_png(data):
    """Accept untagged 8-bit RGB/RGBA only; preserve decoded sample values."""
    if not isinstance(data,bytes) or not 0<len(data)<=MAX_FRAME or not data.startswith(b'\x89PNG\r\n\x1a\n'):
        raise ValueError('unsupported frame')
    offset=8; seen=[]
    while offset<len(data):
        if offset+12>len(data): raise ValueError('invalid PNG chunks')
        length=struct.unpack_from('>I',data,offset)[0]; end=offset+12+length
        if end>len(data): raise ValueError('invalid PNG chunks')
        kind=data[offset+4:offset+8]
        if kind not in (b'IHDR',b'IDAT',b'IEND'): raise ValueError('tagged or extended PNG unsupported')
        seen.append(kind)
        if len(seen)>16384 or (kind==b'IEND' and length!=0): raise ValueError('invalid PNG chunks')
        if len(seen)==1 and (kind!=b'IHDR' or length!=13 or data[offset+16]!=8 or data[offset+17] not in (2,6)):
            raise ValueError('only 8-bit RGB/RGBA PNG is supported')
        offset=end
        if kind==b'IEND': break
    if offset!=len(data) or len(seen)<3 or seen[0]!=b'IHDR' or seen[-1]!=b'IEND' or any(k!=b'IDAT' for k in seen[1:-1]):
        raise ValueError('invalid PNG chunks')

def annotations(value):
    if not isinstance(value,dict) or set(value)!={'fps','facing','pivots','palette','style'}: raise ValueError('sprite annotations unavailable')
    if type(value['fps']) is not int or not 1<=value['fps']<=60: raise ValueError('invalid fps')
    for key in ('facing','palette','style'):
        v=value[key]
        if v is not None and (not isinstance(v,str) or not v.strip() or len(v.encode('utf-8'))>128 or any(ord(c)<32 or ord(c)==127 for c in v)): raise ValueError('invalid sprite annotation')
    pivots=value['pivots']
    if not isinstance(pivots,list) or len(pivots)!=4 or any(not isinstance(p,dict) or set(p)!={'x','y'} or any(type(p[k]) is not int or not 0<=p[k]<=512 for k in ('x','y')) for p in pivots): raise ValueError('invalid pivots')
    return {'fps':value['fps'],'facing':value['facing'],'pivots':[dict(p) for p in pivots],'palette':value['palette'],'style':value['style']}


def inspect_frames(frames, metadata):
    metadata=annotations(metadata)
    if not isinstance(frames,list) or len(frames)!=4: raise ValueError('four frames required')
    sizes=[]
    for data in frames:
        _check_png(data)
        with Image.open(io.BytesIO(data)) as image:
            if image.format!='PNG' or image.mode not in ('RGB','RGBA') or image.width!=image.height or not 1<=image.width<=512: raise ValueError('unsupported frame')
            image.load(); sizes.append(image.size)
    side=sizes[0][0]
    if any(size!=(side,side) for size in sizes) or any(p['x']>side or p['y']>side for p in metadata['pivots']): raise ValueError('frame size or pivot changed')
    return side


def pack(frames, metadata):
    metadata=annotations(metadata)
    if not isinstance(frames,list) or len(frames)!=4: raise ValueError('four frames required')
    images=[]; sheet=None
    try:
        for data in frames:
            _check_png(data)
            with Image.open(io.BytesIO(data)) as image:
                if image.format!='PNG' or image.mode not in ('RGB','RGBA') or image.width!=image.height or not 1<=image.width<=512: raise ValueError('unsupported frame')
                image.load(); images.append(image.convert('RGBA'))
        side=images[0].width
        if any(im.size!=(side,side) for im in images): raise ValueError('frame sizes differ')
        if any(p['x']>side or p['y']>side for p in metadata['pivots']): raise ValueError('pivot outside frame')
        sheet=Image.new('RGBA',(side*2,side*2))
        for im,xy in zip(images,((0,0),(side,0),(0,side),(side,side))): sheet.paste(im,xy)
        out=io.BytesIO(); sheet.save(out,format='PNG'); data=out.getvalue()
        if not 0<len(data)<=MAX_SHEET: raise ValueError('sheet exceeds bounds')
        return data,side
    finally:
        for image in images: image.close()
        if sheet is not None: sheet.close()


def layout_for(sha256, inputs, side, metadata):
    metadata=annotations(metadata)
    if not isinstance(inputs,list) or len(inputs)!=4 or type(side) is not int or not 1<=side<=512: raise ValueError('invalid sheet layout')
    return {'version':1,'cellWidth':side,'cellHeight':side,'sheetWidth':side*2,'sheetHeight':side*2,
        'frames':[{'index':i,'x':x,'y':y,'width':side,'height':side,'source':dict(inputs[i]),'pivot':metadata['pivots'][i]}
                  for i,(x,y) in enumerate(((0,0),(side,0),(0,side),(side,side)))],
        'fps':metadata['fps'],'facing':metadata['facing'],'palette':metadata['palette'],'style':metadata['style'],'sha256':sha256}


def sheet_rectangles(rectangles, width, height):
    if (type(width) is not int or type(height) is not int or not 1<=width<=4096 or not 1<=height<=4096
        or width*height>16777216 or not isinstance(rectangles,list) or len(rectangles)!=4):raise ValueError('invalid sheet geometry')
    clean=[];side=None
    for item in rectangles:
        if (type(item) is not dict or set(item)!={'x','y','width','height'}
            or any(type(item[k]) is not int for k in item)):raise ValueError('invalid frame rectangle')
        x,y,w,h=(item[k] for k in ('x','y','width','height'))
        if x<0 or y<0 or not 1<=w<=512 or h!=w or x+w>width or y+h>height or (side is not None and side!=w):raise ValueError('invalid frame rectangle')
        if any(x<other['x']+side and other['x']<x+w and y<other['y']+side and other['y']<y+h for other in clean):raise ValueError('overlapping frame rectangles')
        side=w;clean.append(dict(item))
    return clean,side


def inspect_sheet(data, rectangles, metadata):
    metadata=annotations(metadata);_check_png(data)
    width,height=struct.unpack_from('>II',data,16)
    rects,side=sheet_rectangles(rectangles,width,height)
    if any(p['x']>side or p['y']>side for p in metadata['pivots']):raise ValueError('pivot outside frame')
    with Image.open(io.BytesIO(data)) as image:
        if image.format!='PNG' or image.mode not in ('RGB','RGBA') or image.size!=(width,height):raise ValueError('unsupported sheet')
        image.load()
    return width,height,side


def pack_sheet(data, rectangles, metadata):
    width,height,side=inspect_sheet(data,rectangles,metadata);rects,_=sheet_rectangles(rectangles,width,height)
    with Image.open(io.BytesIO(data)) as source:
        source.load();sheet=Image.new('RGBA',(side*2,side*2))
        try:
            for rect,xy in zip(rects,((0,0),(side,0),(0,side),(side,side))):
                crop=source.crop((rect['x'],rect['y'],rect['x']+side,rect['y']+side))
                try:sheet.paste(crop,xy)
                finally:crop.close()
            out=io.BytesIO();sheet.save(out,format='PNG');result=out.getvalue()
            if not 0<len(result)<=MAX_SHEET:raise ValueError('sheet exceeds bounds')
            return result,side
        finally:sheet.close()


def selected_layout_for(sha256, source, width, height, rectangles, metadata):
    metadata=annotations(metadata);rects,side=sheet_rectangles(rectangles,width,height)
    if any(p['x']>side or p['y']>side for p in metadata['pivots']):raise ValueError('pivot outside frame')
    return {'version':2,'source':dict(source),'sourceWidth':width,'sourceHeight':height,
        'cellWidth':side,'cellHeight':side,'sheetWidth':side*2,'sheetHeight':side*2,
        'frames':[{'index':i,'sourceRect':rect,'x':x,'y':y,'width':side,'height':side,'pivot':metadata['pivots'][i]}
            for i,(rect,(x,y)) in enumerate(zip(rects,((0,0),(side,0),(0,side),(side,side))))],
        'fps':metadata['fps'],'facing':metadata['facing'],'palette':metadata['palette'],'style':metadata['style'],'sha256':sha256}
