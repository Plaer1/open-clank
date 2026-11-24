"""Fixed TRELLIS image-to-GLB child entry; component paths come from its claimed job."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from contextlib import contextmanager

from src.openclank.trellis_recipe import FIXED_SHA256, MODEL_COMPONENTS, REQUIRED_COMPONENTS


def _json(raw):
    def unique(items):
        result = {}
        for key, value in items:
            if key in result: raise ValueError('duplicate configuration key')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid configuration')))


def _regular(path, limit):
    if type(path) is not str or not os.path.isabs(path) or os.path.realpath(path) != path:
        raise ValueError('component unavailable')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit: raise ValueError('component unavailable')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(limit + 1)
        if len(raw) != info.st_size: raise ValueError('component changed')
        return raw
    finally:
        os.close(fd)


def _hash_file(path, limit):
    if type(path) is not str or not os.path.isabs(path) or os.path.realpath(path) != path: raise ValueError('component unavailable')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= limit: raise ValueError('component unavailable')
        with os.fdopen(fd, 'rb', closefd=False) as stream: digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        after = os.fstat(fd)
        if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns) != (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns): raise ValueError('component changed')
        return digest
    finally: os.close(fd)


@contextmanager
def _weight(item):
    path, size, expected = item['path'], item['size'], item['sha256']
    if type(path) is not str or not os.path.isabs(path) or os.path.realpath(path) != path:
        raise ValueError('weight unavailable')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        identity = lambda x: (x.st_dev, x.st_ino, x.st_size, x.st_mtime_ns, x.st_ctime_ns)
        if not stat.S_ISREG(before.st_mode) or before.st_size != size: raise ValueError('weight unavailable')
        with os.fdopen(os.dup(fd), 'rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != expected: raise ValueError('weight changed')
        if identity(before) != identity(os.fstat(fd)) or identity(before) != identity(os.lstat(path)):
            raise ValueError('weight changed')
        os.lseek(fd, 0, os.SEEK_SET)
        yield fd
        if identity(before) != identity(os.fstat(fd)) or identity(before) != identity(os.lstat(path)):
            raise ValueError('weight changed')
    finally:
        os.close(fd)


def main(descriptor_path):
    descriptor = _json(_regular(descriptor_path, 65536))
    if (type(descriptor) is not dict or set(descriptor) != {'version','seed','input','output','components'}
            or type(descriptor['version']) is not int or descriptor['version'] != 1 or type(descriptor['seed']) is not int
            or not 0 <= descriptor['seed'] <= 2147483647 or type(descriptor['components']) is not dict
            or set(descriptor['components']) != set(REQUIRED_COMPONENTS)):
        raise ValueError('recipe unavailable')
    items = descriptor['components']
    for role, item in items.items():
        if (type(item) is not dict or set(item) != {'path','size','sha256'} or type(item['path']) is not str
                or type(item['size']) is not int or not 0 < item['size'] <= 2 * 1024 * 1024 * 1024
                or type(item['sha256']) is not str or len(item['sha256']) != 64
                or any(c not in '0123456789abcdef' for c in item['sha256'])
                or (role in FIXED_SHA256 and item['sha256'] != FIXED_SHA256[role])):
            raise ValueError('component unavailable')
    components = {role:item['path'] for role,item in items.items()}
    for role, digest in FIXED_SHA256.items():
        if not role.endswith('_weights') and _hash_file(components[role], 2 * 1024 * 1024) != digest: raise ValueError('component changed')
    source = _regular(descriptor['input'], 4 * 1024 * 1024)
    import io
    import numpy as np
    from PIL import Image
    with Image.open(io.BytesIO(source)) as image:
        if image.format != 'PNG' or image.mode != 'RGBA' or image.width < 1 or image.height < 1 or image.width * image.height > 16777216:
            raise ValueError('RGBA image required')
        image.load()
        alpha = np.asarray(image)[:, :, 3]
        if not np.any(alpha != 255) or not np.any(alpha > 204): raise ValueError('foreground unavailable')
        image = image.copy()
    output = descriptor['output']
    if type(output) is not str or not os.path.isabs(output) or os.path.lexists(output): raise ValueError('output unavailable')

    # The installed code roots are selected by the private reviewed manifest.
    trellis_root = str(Path(components['trellis_models_init']).parents[2])
    dino_root = str(Path(components['dino_backbones']).parents[2])
    sys.path[:0] = [trellis_root, dino_root]
    import torch
    from torchvision import transforms
    from safetensors.torch import load_file
    from trellis import models
    from trellis.pipelines import TrellisImageTo3DPipeline, samplers
    from trellis.utils import postprocessing_utils
    from dinov2.hub.backbones import dinov2_vitl14_reg
    import trellis.models as model_module
    import trellis.pipelines.base as base_module
    import trellis.pipelines.trellis_image_to_3d as image_module
    import dinov2.hub.backbones as dino_module
    import dinov2.models.vision_transformer as vision_module
    for module, role in ((model_module,'trellis_models_init'),(base_module,'trellis_base'),
                         (image_module,'trellis_image_pipeline'),(dino_module,'dino_backbones'),(vision_module,'dino_vision')):
        if os.path.realpath(module.__file__) != components[role]: raise ValueError('installed code changed')
    pipeline_raw = _regular(components['pipeline'], 65536)
    if hashlib.sha256(pipeline_raw).hexdigest() != FIXED_SHA256['pipeline']: raise ValueError('pipeline changed')
    cfg = _json(pipeline_raw)
    args = cfg['args']
    expected_names = {'ss_decoder':'sparse_structure_decoder','ss_flow':'sparse_structure_flow_model',
                      'slat_gaussian':'slat_decoder_gs','slat_radiance':'slat_decoder_rf',
                      'slat_mesh':'slat_decoder_mesh','slat_flow':'slat_flow_model'}
    if cfg['name'] != 'TrellisImageTo3DPipeline' or args['image_cond_model'] != 'dinov2_vitl14_reg': raise ValueError('pipeline changed')
    loaded = {}
    for role, (base, class_name) in MODEL_COMPONENTS.items():
        if args['models'].get(expected_names[role]) != 'ckpts/' + base: raise ValueError('pipeline changed')
        config_raw = _regular(components[role + '_config'], 65536)
        if hashlib.sha256(config_raw).hexdigest() != FIXED_SHA256[role + '_config']: raise ValueError('model changed')
        model_config = _json(config_raw)
        if model_config['name'] != class_name: raise ValueError('model changed')
        model = getattr(models, class_name)(**model_config['args'])
        with _weight(items[role + '_weights']) as fd:
            model.load_state_dict(load_file(f'/proc/self/fd/{fd}'), strict=True)
        model.eval()
        loaded[expected_names[role]] = model
    dino = dinov2_vitl14_reg(pretrained=False)
    with _weight(items['dino_weights']) as fd:
        with os.fdopen(os.dup(fd), 'rb') as stream:
            dino.load_state_dict(torch.load(stream, map_location='cpu', weights_only=True), strict=True)
    dino.eval(); loaded['image_cond_model'] = dino
    pipeline = TrellisImageTo3DPipeline()
    pipeline.models = loaded
    for key in ('sparse_structure_sampler','slat_sampler'):
        spec = args[key]
        if spec['name'] != 'FlowEulerGuidanceIntervalSampler': raise ValueError('sampler changed')
        setattr(pipeline, key, samplers.FlowEulerGuidanceIntervalSampler(**spec['args']))
        setattr(pipeline, key + '_params', spec['params'])
    pipeline.slat_normalization = args['slat_normalization']
    pipeline.rembg_session = None
    pipeline.image_cond_model_transform = transforms.Compose([transforms.Normalize(mean=[0.485,0.456,0.406], std=[0.229,0.224,0.225])])
    pipeline.cuda()
    result = pipeline.run(image, num_samples=1, seed=descriptor['seed'], formats=['mesh','gaussian'], preprocess_image=True)
    glb = postprocessing_utils.to_glb(result['gaussian'][0], result['mesh'][0], simplify=0, fill_holes=False, texture_size=1024)
    glb.export(output)


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--': raise SystemExit(2)
    main(sys.argv[2])
