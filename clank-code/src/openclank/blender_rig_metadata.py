"""Closed metadata for a single explicit imported-bone pose clip."""
def layout_for(sha256, rig, importer):
    return {'version':1,'coordinateFrame':'imported-blender-bone-local-x','cellWidth':512,'cellHeight':512,
        'sheetWidth':1024,'sheetHeight':1024,'fps':6,'rig':rig,'importer':importer,
        'frames':[{'frameIndex':i,'degrees':d,'x':x,'y':y,'width':512,'height':512}
            for i,(d,x,y) in enumerate(((0,0,0),(15,512,0),(0,0,512),(-15,512,512)))], 'sha256':sha256}
