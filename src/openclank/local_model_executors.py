"""Registered host recipes for keyless local model routes.

The managed engine chooses a durable local connection/model route and invokes
one of these fixed recipes through its capability-bound host callback.  Recipe
inputs are artifact IDs and normalized options only; they never accept a
command, path, URL, environment, or credential.
"""

from __future__ import annotations

import io
import inspect
import json
from pathlib import Path
import threading
from typing import Any, Mapping

from src.openclank.artifacts import ArtifactStore
from src.openclank.local_executor import (
    ExecutorInput,
    ExecutorOutput,
    LocalExecutorBroker,
    LocalExecutorError,
)


_instances: dict[str, Any] = {}
_instance_lock = threading.Lock()


def _instance(name: str, factory):
    value = _instances.get(name)
    if value is not None:
        return value
    with _instance_lock:
        value = _instances.get(name)
        if value is None:
            value = factory()
            _instances[name] = value
    return value


def _text(value: Any, label: str, *, maximum: int) -> str:
    result = str(value or "")
    if not result.strip():
        raise LocalExecutorError(f"{label} is required")
    if len(result) > maximum:
        raise LocalExecutorError(f"{label} exceeds its size limit")
    return result


def _number(
    value: Any,
    label: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise LocalExecutorError(f"{label} must be numeric") from None
    if result < minimum or result > maximum:
        raise LocalExecutorError(f"{label} is outside its allowed range")
    return result


def _one_image(inputs: list[ExecutorInput]):
    if len(inputs) != 1 or not inputs[0].media_type.startswith("image/"):
        raise LocalExecutorError("local image executor requires one image artifact")
    from PIL import Image

    try:
        return Image.open(io.BytesIO(inputs[0].read())).convert("RGB")
    except Exception:
        raise LocalExecutorError("local image executor received an invalid image") from None


def _png(image) -> ExecutorOutput:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return ExecutorOutput(data=buffer.getvalue(), media_type="image/png")


def _fastembed(
    operation: str,
    _inputs: list[ExecutorInput],
    options: Mapping[str, Any],
) -> ExecutorOutput:
    if operation != "embeddings.create":
        raise LocalExecutorError("FastEmbed recipe received an unsupported operation")
    texts = options.get("texts")
    if not isinstance(texts, list) or not texts or len(texts) > 256:
        raise LocalExecutorError("FastEmbed requires between 1 and 256 texts")
    normalized = [_text(value, "embedding text", maximum=100_000) for value in texts]
    # This fixed recipe is the implementation of the normalized
    # ``fastembed/default`` route.  A caller option or host environment value
    # must not select a different model behind that stable route identity.
    model_name = "sentence-transformers/all-MiniLM-L6-v2"

    def create():
        from src.embeddings import FastEmbedClient

        return FastEmbedClient(model=model_name)

    client = _instance(f"fastembed:{model_name}", create)
    vectors = client.encode(normalized, normalize_embeddings=True)
    values = vectors.tolist() if hasattr(vectors, "tolist") else list(vectors)
    dimension = len(values[0]) if values else 0
    if dimension < 1 or any(len(value) != dimension for value in values):
        raise LocalExecutorError("FastEmbed returned inconsistent vector dimensions")
    payload = {
        "embeddings": values,
        "dimension": dimension,
        "modelFingerprint": f"fastembed:{model_name}:{dimension}",
    }
    return ExecutorOutput(
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        media_type="application/vnd.openclank.embeddings+json",
    )


def _kokoro(
    operation: str,
    _inputs: list[ExecutorInput],
    options: Mapping[str, Any],
) -> ExecutorOutput:
    if operation != "audio.synthesize":
        raise LocalExecutorError("Kokoro recipe received an unsupported operation")
    text = _text(options.get("text"), "speech text", maximum=5_000)
    voice = _text(options.get("voice") or "af_heart", "voice", maximum=64)
    _number(options.get("speed", 1), "speech speed", minimum=0.25, maximum=4)

    def create():
        from services.tts.tts_service import _KokoroPipeline

        return _KokoroPipeline()

    pipeline = _instance("kokoro-82m", create)
    if not getattr(pipeline, "available", False):
        raise LocalExecutorError("Kokoro local executor is unavailable")
    data = pipeline.synthesize_raw(text, voice)
    if not isinstance(data, bytes) or not data:
        raise LocalExecutorError("Kokoro local executor returned no audio")
    return ExecutorOutput(data=data, media_type="audio/wav")


def _whisper(
    operation: str,
    inputs: list[ExecutorInput],
    options: Mapping[str, Any],
) -> ExecutorOutput:
    if operation != "audio.transcribe":
        raise LocalExecutorError("Whisper recipe received an unsupported operation")
    if len(inputs) != 1 or not inputs[0].media_type.startswith("audio/"):
        raise LocalExecutorError("Whisper requires one audio artifact")
    language = str(options.get("language") or "")[:32]

    def create():
        from services.stt.stt_service import STTService

        return STTService()

    service = _instance("faster-whisper", create)
    result = service._transcribe_local(inputs[0].read(), language)
    if result is None:
        raise LocalExecutorError("Whisper local executor failed")
    return ExecutorOutput(
        data=json.dumps({"text": result}, separators=(",", ":")).encode("utf-8"),
        media_type="application/vnd.openclank.transcript+json",
    )


def _diffusion_device():
    import torch

    if torch.cuda.is_available():
        return "cuda", torch.float16
    if (
        getattr(torch.backends, "mps", None)
        and torch.backends.mps.is_available()
    ):
        return "mps", torch.float16
    return "cpu", torch.float32


def _load_diffusion_pipeline(kind: str):
    """Load one installation-owned diffusion pipeline without a network path."""

    from diffusers import (
        AutoPipelineForImage2Image,
        AutoPipelineForInpainting,
        AutoPipelineForText2Image,
    )
    from src.constants import DATA_DIR

    model_root = Path(DATA_DIR) / "model-assets" / "diffusion" / "default"
    if not model_root.is_dir() or not (model_root / "model_index.json").is_file():
        raise LocalExecutorError(
            "the managed diffusion/default installation model is unavailable"
        )
    device, dtype = _diffusion_device()
    classes = {
        "generate": AutoPipelineForText2Image,
        "img2img": AutoPipelineForImage2Image,
        "inpaint": AutoPipelineForInpainting,
    }
    try:
        pipeline = classes[kind].from_pretrained(
            str(model_root),
            torch_dtype=dtype,
            local_files_only=True,
        )
        pipeline = pipeline.to(device)
        if hasattr(pipeline, "set_progress_bar_config"):
            pipeline.set_progress_bar_config(disable=True)
        return pipeline
    except LocalExecutorError:
        raise
    except Exception:
        raise LocalExecutorError(
            f"the managed diffusion/default {kind} pipeline is unavailable"
        ) from None


def _diffusion_pipeline(kind: str):
    return _instance(
        f"diffusion/default:{kind}",
        lambda: _load_diffusion_pipeline(kind),
    )


def _diffusion_size(value: Any) -> tuple[int, int]:
    raw = str(value or "1024x1024").lower()
    try:
        width_raw, height_raw = raw.split("x", 1)
        width, height = int(width_raw), int(height_raw)
    except (TypeError, ValueError):
        raise LocalExecutorError("diffusion image size must be WIDTHxHEIGHT") from None
    if not (64 <= width <= 4096 and 64 <= height <= 4096):
        raise LocalExecutorError("diffusion image dimensions are outside their limits")
    # Diffusion latent grids require dimensions divisible by eight.
    return max(64, width - (width % 8)), max(64, height - (height % 8))


def _open_executor_image(value: ExecutorInput, *, mode: str):
    if not value.media_type.startswith("image/"):
        raise LocalExecutorError("diffusion inputs must be image artifacts")
    from PIL import Image

    try:
        return Image.open(io.BytesIO(value.read())).convert(mode)
    except Exception:
        raise LocalExecutorError("diffusion received an invalid image artifact") from None


def _pipeline_kwargs(pipeline: Any, values: Mapping[str, Any]) -> dict[str, Any]:
    """Filter fixed normalized fields to the installed pipeline signature."""

    try:
        parameters = inspect.signature(pipeline.__call__).parameters
    except (TypeError, ValueError):
        parameters = {}
    if any(
        value.kind == inspect.Parameter.VAR_KEYWORD
        for value in parameters.values()
    ):
        return {key: value for key, value in values.items() if value is not None}
    return {
        key: value
        for key, value in values.items()
        if value is not None and key in parameters
    }


def _diffusion(
    operation: str,
    inputs: list[ExecutorInput],
    options: Mapping[str, Any],
) -> ExecutorOutput:
    """Fixed ``diffusion/default`` recipe for managed image operations.

    The recipe never accepts a model identifier, repository, path, URL, or
    credential. Installation tooling owns the exact model payload under the
    fixed asset directory; MiMo owns route selection before this handler runs.
    """

    if operation not in {
        "image.generate",
        "image.edit",
        "image.inpaint",
        "image.img2img",
    }:
        raise LocalExecutorError("diffusion recipe received an unsupported operation")
    prompt = _text(options.get("prompt"), "image prompt", maximum=10_000)
    width, height = _diffusion_size(options.get("size"))
    quality = str(options.get("quality") or "medium").lower()
    default_steps = {"low": 4, "medium": 12, "high": 24, "auto": 16}.get(
        quality,
        12,
    )
    steps = int(
        _number(
            options.get("steps", default_steps),
            "diffusion steps",
            minimum=1,
            maximum=100,
        )
    )
    strength = _number(
        options.get("strength", options.get("colorMatch", 0.55)),
        "diffusion strength",
        minimum=0.0,
        maximum=1.0,
    )

    if operation == "image.generate":
        if inputs:
            raise LocalExecutorError("image.generate does not accept source artifacts")
        pipeline = _diffusion_pipeline("generate")
        values: dict[str, Any] = {
            "prompt": prompt,
            "width": width,
            "height": height,
            "num_inference_steps": steps,
        }
    elif operation == "image.inpaint":
        if len(inputs) != 2:
            raise LocalExecutorError("image.inpaint requires source and mask artifacts")
        source = _open_executor_image(inputs[0], mode="RGB")
        mask = _open_executor_image(inputs[1], mode="L")
        if mask.size != source.size:
            from PIL import Image

            mask = mask.resize(source.size, Image.Resampling.BILINEAR)
        pipeline = _diffusion_pipeline("inpaint")
        values = {
            "prompt": prompt,
            "image": source,
            "mask_image": mask,
            "width": width,
            "height": height,
            "num_inference_steps": steps,
            "strength": max(0.05, strength),
        }
    else:
        if len(inputs) != 1:
            raise LocalExecutorError(f"{operation} requires one source image artifact")
        source = _open_executor_image(inputs[0], mode="RGB")
        pipeline = _diffusion_pipeline("img2img")
        values = {
            "prompt": prompt,
            "image": source,
            "width": width,
            "height": height,
            "num_inference_steps": steps,
            "strength": max(0.05, strength),
        }

    try:
        result = pipeline(**_pipeline_kwargs(pipeline, values))
        images = getattr(result, "images", None)
        if not images:
            raise LocalExecutorError("managed diffusion returned no image")
        return _png(images[0].convert("RGBA"))
    except LocalExecutorError:
        raise
    except Exception:
        raise LocalExecutorError("managed diffusion execution failed") from None


def _realesrgan(
    operation: str,
    inputs: list[ExecutorInput],
    options: Mapping[str, Any],
) -> ExecutorOutput:
    if operation not in {"image.upscale", "image.denoise"}:
        raise LocalExecutorError("Real-ESRGAN recipe received an unsupported operation")
    image = _one_image(inputs)
    try:
        import numpy as np
        from realesrgan import RealESRGANer
    except Exception:
        raise LocalExecutorError("Real-ESRGAN local executor is unavailable") from None

    scale = int(_number(options.get("scale", 2), "upscale factor", minimum=1, maximum=4))
    # Fixed installation-owned assets only.  Model installers populate these
    # paths; the recipe never downloads a release or accepts a caller path.
    from src.constants import DATA_DIR

    model_root = Path(DATA_DIR) / "model-assets" / "realesrgan"
    if operation == "image.denoise":
        from realesrgan.archs.srvgg_arch import SRVGGNetCompact

        model = SRVGGNetCompact(
            num_in_ch=3,
            num_out_ch=3,
            num_feat=64,
            num_conv=32,
            upscale=4,
            act_type="prelu",
        )
        model_path = model_root / "realesr-general-x4v3.pth"
    else:
        from basicsr.archs.rrdbnet_arch import RRDBNet

        model = RRDBNet(
            num_in_ch=3,
            num_out_ch=3,
            num_feat=64,
            num_block=23,
            num_grow_ch=32,
            scale=4,
        )
        model_path = model_root / "RealESRGAN_x4plus.pth"
    if not model_path.is_file():
        raise LocalExecutorError("Real-ESRGAN installation model is unavailable")
    upsampler = RealESRGANer(
        scale=4,
        model_path=str(model_path),
        model=model,
        tile=0,
        tile_pad=10,
        pre_pad=0,
        half=False,
    )
    output, _ = upsampler.enhance(np.array(image), outscale=(1 if operation == "image.denoise" else scale))
    from PIL import Image

    return _png(Image.fromarray(output))


def _background_or_segment(
    operation: str,
    inputs: list[ExecutorInput],
    _options: Mapping[str, Any],
) -> ExecutorOutput:
    if operation not in {"image.segment", "image.remove_background"}:
        raise LocalExecutorError("background recipe received an unsupported operation")
    image = _one_image(inputs)
    try:
        from transformers import pipeline
    except Exception:
        raise LocalExecutorError("background-removal local executor is unavailable") from None

    from src.constants import DATA_DIR

    model_path = (
        Path(DATA_DIR)
        / "model-assets"
        / "rmbg"
        / "bria-rmbg-1.4"
    )
    if not model_path.is_dir():
        raise LocalExecutorError(
            "background-removal installation model is unavailable"
        )

    segmenter = _instance(
        "bria-rmbg-1.4",
        lambda: pipeline(
            "image-segmentation",
            model=str(model_path),
            trust_remote_code=True,
        ),
    )
    result = segmenter(image)
    masks = result if isinstance(result, list) else [result]
    masks = [item.get("mask") for item in masks if isinstance(item, Mapping) and item.get("mask") is not None]
    if not masks:
        raise LocalExecutorError("background-removal executor returned no mask")
    mask = masks[0].convert("L").resize(image.size)
    if operation == "image.segment":
        return _png(mask)
    rgba = image.convert("RGBA")
    rgba.putalpha(mask)
    return _png(rgba)


def _restore_face(
    operation: str,
    inputs: list[ExecutorInput],
    _options: Mapping[str, Any],
) -> ExecutorOutput:
    if operation != "image.restore_face":
        raise LocalExecutorError("GFPGAN recipe received an unsupported operation")
    image = _one_image(inputs)
    try:
        import numpy as np
        from gfpgan import GFPGANer
    except Exception:
        raise LocalExecutorError("GFPGAN local executor is unavailable") from None
    from src.constants import DATA_DIR

    model_path = Path(DATA_DIR) / "model-assets" / "gfpgan" / "GFPGANv1.4.pth"
    if not model_path.is_file():
        raise LocalExecutorError("GFPGAN installation model is unavailable")
    restorer = GFPGANer(
        model_path=str(model_path),
        upscale=1,
        arch="clean",
        channel_multiplier=2,
        bg_upsampler=None,
    )
    _, _, restored = restorer.enhance(
        np.array(image)[:, :, ::-1],
        has_aligned=False,
        only_center_face=False,
        paste_back=True,
    )
    if restored is None:
        raise LocalExecutorError("GFPGAN local executor returned no image")
    from PIL import Image

    return _png(Image.fromarray(restored[:, :, ::-1]))


def build_local_executor_broker(
    artifact_store: ArtifactStore,
    *,
    global_concurrency: int = 2,
) -> LocalExecutorBroker:
    broker = LocalExecutorBroker(
        artifact_store,
        global_concurrency=global_concurrency,
    )
    broker.register(
        executor_id="openclank.fastembed.v1",
        model_id="fastembed/default",
        operations={"embeddings.create"},
        handler=_fastembed,
        max_concurrency=1,
    )
    broker.register(
        executor_id="openclank.kokoro.v1",
        model_id="kokoro/Kokoro-82M",
        operations={"audio.synthesize"},
        handler=_kokoro,
        max_concurrency=1,
    )
    broker.register(
        executor_id="openclank.whisper.v1",
        model_id="faster-whisper/default",
        operations={"audio.transcribe"},
        handler=_whisper,
        max_concurrency=1,
    )
    broker.register(
        executor_id="openclank.diffusion.v1",
        model_id="diffusion/default",
        operations={
            "image.generate",
            "image.edit",
            "image.inpaint",
            "image.img2img",
        },
        handler=_diffusion,
        max_concurrency=1,
    )
    broker.register(
        executor_id="openclank.realesrgan.v1",
        model_id="realesrgan/x4plus",
        operations={"image.upscale", "image.denoise"},
        handler=_realesrgan,
        max_concurrency=1,
    )
    broker.register(
        executor_id="openclank.rmbg.v1",
        model_id="briaai/RMBG-1.4",
        operations={"image.segment", "image.remove_background"},
        handler=_background_or_segment,
        max_concurrency=1,
    )
    broker.register(
        executor_id="openclank.gfpgan.v1",
        model_id="gfpgan/clean-v1",
        operations={"image.restore_face"},
        handler=_restore_face,
        max_concurrency=1,
    )
    return broker


__all__ = ["build_local_executor_broker"]
