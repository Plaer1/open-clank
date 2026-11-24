"""Existing Files image/destination scope for local Hunyuan shape."""
import io
import warnings
from PIL import Image
from src.openclank.trellis_files import TrellisFilesScope


def _validate_image(data):
    if type(data) is not bytes or not 0 < len(data) <= 4 * 1024 * 1024:
        raise ValueError('Hunyuan image unavailable')
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data)) as image:
            if (image.format != 'PNG' or image.mode != 'RGBA' or getattr(image,'n_frames',1) != 1
                    or getattr(image,'is_animated',False) or not 1 <= image.width <= 4096
                    or not 1 <= image.height <= 4096 or image.width * image.height > 16777216):
                raise ValueError('Hunyuan image unavailable')
            image.load()
            bounds=image.getchannel('A').getbbox()
            if bounds is None or bounds[2]-bounds[0]<2 or bounds[3]-bounds[1]<2:
                raise ValueError('Hunyuan foreground unavailable')


class HunyuanFilesScope(TrellisFilesScope):
    _validate_image = staticmethod(_validate_image)

    async def control(self, identity):
        binding=identity.get('resource_binding') or {}
        return binding.get('interface')=='hunyuan3d-v2-shape-v1' and binding.get('ownerAccountID')==identity.get('account_id')
