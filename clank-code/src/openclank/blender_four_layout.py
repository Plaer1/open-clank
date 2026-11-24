"""Closed four-heading sheet geometry and retained digest."""
def layout_for(sha256):
    return {'version':1,'coordinateFrame':'imported-world-z','cellWidth':512,'cellHeight':512,
        'sheetWidth':1024,'sheetHeight':1024,'views':[
            {'degrees':0,'x':0,'y':0,'width':512,'height':512},
            {'degrees':90,'x':512,'y':0,'width':512,'height':512},
            {'degrees':180,'x':0,'y':512,'width':512,'height':512},
            {'degrees':270,'x':512,'y':512,'width':512,'height':512}], 'sha256':sha256}
