"""Closed provenance and component identities for local Hunyuan shape."""
RECIPE_ID = 'hunyuan3d-v2-image-glb-v1'
SOURCE_REVISION = 'f8db63096c8282cb27354314d896feba5ba6ff8a'
MODEL_REVISION = '9cd649ba6913f7a852e3286bad86bfa9a2d83dcf'
REQUIRED_COMPONENTS = {
    'code_pipeline': 'hunyuan/hy3dgen/shapegen/pipelines.py',
    'code_utils': 'hunyuan/hy3dgen/shapegen/utils.py',
    'code_conditioner': 'hunyuan/hy3dgen/shapegen/models/conditioner.py',
    'code_preprocessor': 'hunyuan/hy3dgen/shapegen/preprocessors.py',
    'config': 'model/hunyuan3d-dit-v2-0/config.yaml',
    'weights': 'model/hunyuan3d-dit-v2-0/model.fp16.safetensors',
}
FIXED_SHA256 = {
    'code_pipeline': '3eb438997fca59f682866f529f1984d4852d717577c24743e5d9bfd5566fa467',
    'code_utils': '434987747ec07c734b64cc933f7829bf6d10800a7c5e97f649547aa5979c1fbd',
    'code_conditioner': 'c735ef7abbdd0fad089e684a237ef30a6cb7616c2faa49a30571d3640c2865c9',
    'code_preprocessor': '284dc24fabbf974bd6793ebd58e95b7700550d90371cacb96ad0889ec558de1e',
    'config': '70bbfcdd18b9686ee56fe011c3a059b222eae1f5450f904577c50f0e107a8a53',
    'weights': '360bc281fc956d4acac0c3d36d5ec0ebf8cdddbf4b8892e894d12419388d479b',
}
FIXED_SIZES = {'config': 1604, 'weights': 4928151562}
DEVICES = ('cpu-fp32', 'cuda-fp16')
