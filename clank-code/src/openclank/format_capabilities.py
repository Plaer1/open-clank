"""Project existing preview hints as one versioned, authority-neutral descriptor."""
from __future__ import annotations

import fnmatch
import json
import re
from collections.abc import Mapping
from pathlib import Path, PurePath
from typing import Any

from src.openclank.format_adapters.base import FormatCapabilityV1, FormatOperation

# Generated from the editor's existing registry; these are hints only.
try:
    _EDITOR_ASSOCIATIONS = json.loads(Path(__file__).with_name("editor_language_associations.json").read_text(encoding="utf-8"))
except (OSError, ValueError):
    _EDITOR_ASSOCIATIONS = {}
_EDITOR_BINARY_SUFFIX = re.compile(_EDITOR_ASSOCIATIONS.get("binarySuffixPattern", r"(?!)"), re.IGNORECASE)
EDITOR_MIME_TYPES = frozenset({
    "application/javascript", "application/json", "application/ld+json", "application/xml",
    "text/css", "text/html", "text/markdown", "text/plain", "text/x-c", "text/x-python",
    "text/x-rust", "text/x-shellscript", "text/xml",
})
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp", ".tif", ".tiff"})
_AUDIO_SUFFIXES = frozenset({".mp3", ".wav", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wma"})


def editor_text_hint(name: str, mime_type: str | None = None, *, include_conditional: bool = True) -> bool:
    """Share Host's existing editor association hint without granting access."""
    mime = str(mime_type or "").strip().lower()
    path = str(name).replace("\\", "/").lower()
    leaf = path.rsplit("/", 1)[-1]
    if _EDITOR_BINARY_SUFFIX.search(leaf):
        return False
    if leaf.endswith((".clankbook", ".clankslides")):
        return True
    if mime in EDITOR_MIME_TYPES or mime.startswith("text/"):
        return True
    if not include_conditional and any(leaf.endswith(suffix) for suffix in _EDITOR_ASSOCIATIONS.get("conditionalSuffixes", ())):
        return False
    if leaf in _EDITOR_ASSOCIATIONS.get("filenames", ()):
        return True
    if any(leaf.endswith(suffix) for suffix in _EDITOR_ASSOCIATIONS.get("suffixes", ())):
        return True
    if include_conditional and any(leaf.endswith(suffix) for suffix in _EDITOR_ASSOCIATIONS.get("conditionalSuffixes", ())):
        return True
    return any(fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(leaf, pattern)
               for pattern in _EDITOR_ASSOCIATIONS.get("patterns", ()))


def _operation(available: bool, unavailable_reason: str, *, hint: bool = False) -> FormatOperation:
    return {"available": available, "reason": "content_not_sniffed" if available and hint else (None if available else unavailable_reason)}


# Presentation and parser selection only. The caller must independently authorize
# the resource and verify its bytes; this table never grants a capability.
_INSPECTION_HINTS = {
    '.sqlite': ('sqlite', 'sqlite-main-schema'), '.sqlite3': ('sqlite', 'sqlite-main-schema'), '.db': ('sqlite', 'sqlite-main-schema'),
    '.dylib': ('macho', 'macho-framing'), '.bundle': ('macho', 'macho-framing'), '.macho': ('macho', 'macho-framing'),
    '.exe': ('pe', 'pe-metadata'), '.dll': ('pe', 'pe-metadata'), '.sys': ('pe', 'pe-metadata'), '.efi': ('pe', 'pe-metadata'),
    '.elf': ('elf', 'elf-metadata'), '.so': ('elf', 'elf-metadata'), '.o': ('elf', 'elf-metadata'), '.axf': ('elf', 'elf-metadata'),
    '.wasm': ('wasm', 'wasm-framing'), '.ff': ('fastfile', 'fastfile-scan'),
    '.asar': ('asar', 'asar-header'), '.gguf': ('gguf', 'gguf-header'), '.safetensors': ('safetensors', 'safetensors-header'),
}


def inspection_hint(name: str):
    from importlib import import_module
    item = _INSPECTION_HINTS.get(PurePath(str(name or '').lower()).suffix)
    if item is None:
        return None
    format_id, adapter = item
    module = import_module('src.openclank.format_adapters.' + ('sqlite_metadata' if format_id == 'sqlite' else format_id))
    if format_id == 'sqlite':
        limits = {'inputBytes':module.MAX_FILE,'expandedBytes':module.MAX_OUTPUT,'entries':module.MAX_ROWS,'depth':1,'timeMs':15000}
    elif format_id in {'pe', 'elf', 'wasm', 'macho'}:
        entries = module.MAX_COMMANDS if format_id == 'macho' else module.MAX_SECTIONS if format_id != 'elf' else module.MAX_ENTRIES
        limits = {'inputBytes':module.MAX_FILE,'expandedBytes':module.MAX_OBSERVED,'entries':entries,'depth':1,'timeMs':15000}
    elif format_id == 'fastfile':
        limits = {'inputBytes':module.MAX_INPUT,'expandedBytes':module.MAX_EXPANDED,'entries':module.MAX_CANDIDATES,'depth':1,'timeMs':15000}
    elif format_id == 'asar':
        limits = {'inputBytes':module.MAX_HEADER+8,'expandedBytes':module.MAX_HEADER,'entries':module.MAX_ENTRIES,'depth':module.MAX_DEPTH,'timeMs':15000}
    elif format_id == 'gguf':
        limits = {'inputBytes':module.MAX_HEADER,'expandedBytes':module.MAX_HEADER,'entries':module.MAX_RECORDS,'depth':3,'timeMs':15000}
    else:
        limits = {'inputBytes':module.MAX_HEADER+8,'expandedBytes':module.MAX_HEADER,'entries':module.MAX_ENTRIES,'depth':module.MAX_DIMS,'timeMs':15000}
    return format_id, module, adapter, limits


def project_format_capability(*, resource_ref: str, policy_generation: int,
                              provider_revision: Mapping[str, Any] | None,
                              kind: str, name: str, mime_type: str | None,
                              preview_kind: str | None, capabilities: tuple[str, ...],
                              native_thumbnail_available: bool | None, provider: str | None = None) -> FormatCapabilityV1:
    """Describe existing routes without sniffing content or granting permission."""
    caps = set(capabilities)
    spec = inspection_hint(name) if kind == 'file' else None
    if spec is not None:
        format_id, module, adapter, limits = spec
        available = 'preview' in caps and provider == 'host'
        reason = 'content_not_sniffed' if available else 'range_provider_unavailable'
        profile = getattr(module, 'REGISTRY_PROFILE', None) or module.PROFILE
        return {'version':1,'formatId':format_id,'variant':None,'mimeType':mime_type,
            'sniff':{'confidence':'hint','source':'extension'},'adapter':{'id':adapter,'version':profile},
            'operations':{'read':_operation(available,reason),'inspect':{'available':available,'reason':reason},
                'thumbnail':_operation(False,'unsupported'),'preview':_operation(False,'inspect_only'),
                'convert':_operation(False,'unsupported'),'edit':_operation(False,'read_only')},
            'limits':limits,'readiness':{'state':'available' if available else 'unavailable','reason':reason},
            'outputKinds':['metadata'] if available else [],'resourceRef':resource_ref,
            'providerRevision':dict(provider_revision) if provider_revision is not None else None,
            'contentSHA256':None,'decoderGeneration':profile,'policyGeneration':int(policy_generation)}
    if kind == "file" and PurePath(str(name or "").lower()).suffix == ".iwi":
        from src.openclank.format_adapters import iwi
        available = "preview" in caps and iwi.decoder_available()
        reason = "content_not_sniffed" if available else ("decoder_unavailable" if "preview" in caps else "preview_unavailable")
        operations = {
            "read": _operation(bool(caps & {"read", "preview", "download"}), "read_route_unavailable"),
            "inspect": {"available": available, "reason": reason},
            "thumbnail": {"available": available, "reason": reason},
            "preview": {"available": available, "reason": reason},
            "convert": _operation(False, "convert_adapter_unavailable"),
            "edit": _operation(False, "format_edit_not_qualified"),
        }
        return {
            "version": 1, "formatId": "iwi", "variant": None, "mimeType": str(mime_type or "").strip().lower() or None,
            "sniff": {"confidence": "hint", "source": "extension"},
            "adapter": {"id": "iwi6-bcn", "version": iwi.ADAPTER_VERSION},
            "operations": operations,
            "limits": {"inputBytes": iwi.MAX_INPUT_BYTES, "expandedBytes": iwi.MAX_PIXELS * 4, "entries": 1, "depth": 1, "timeMs": 15000},
            "readiness": {"state": "available" if available else "unavailable", "reason": reason},
            "outputKinds": ["image"] if available else [], "resourceRef": resource_ref,
            "providerRevision": dict(provider_revision) if provider_revision is not None else None,
            "contentSHA256": None, "decoderGeneration": (f"{iwi.ADAPTER_VERSION}:pillow-{__import__('PIL').__version__}" if available else None), "policyGeneration": int(policy_generation),
        }
    mime = str(mime_type or "").split(";", 1)[0].strip().lower()
    supplied = str(preview_kind or "").lower()
    family = supplied if supplied in {"image", "audio", "text"} else "unknown"
    source = "provider" if family != "unknown" else "none"
    if family == "unknown":
        suffix = PurePath(str(name or "").lower()).suffix
        if mime.startswith("image/") or suffix in _IMAGE_SUFFIXES:
            family, source = "image", "mime" if mime.startswith("image/") else "extension"
        elif mime.startswith("audio/") or suffix in _AUDIO_SUFFIXES:
            family, source = "audio", "mime" if mime.startswith("audio/") else "extension"
        elif editor_text_hint(name, mime, include_conditional=False):
            family, source = "text", "mime" if mime.startswith("text/") or mime in EDITOR_MIME_TYPES else "editor-association"
    if kind == "file" and provider == "host" and PurePath(str(name or "").lower()).suffix == ".webm" and mime == "video/webm":
        family, source = "video", "mime"
    workbook = kind == "file" and PurePath(str(name or "").lower()).suffix == ".clankbook"
    slides = kind == "file" and PurePath(str(name or "").lower()).suffix == ".clankslides"
    if workbook or slides:
        family, source = "text", "extension"
    readable = bool(caps & {"read", "preview", "download"})
    preview = "preview" in caps and family != "unknown" and kind not in {"folder", "directory"}
    thumbnail = bool("preview" in caps and (preview and family == "image" or native_thumbnail_available is True))
    operations = {
        "read": _operation(readable, "read_route_unavailable"),
        "inspect": _operation(False, "inspect_adapter_unavailable"),
        "thumbnail": _operation(thumbnail, "thumbnail_unavailable", hint=True),
        "preview": _operation(preview, "preview_unavailable" if family != "unknown" else "format_unknown", hint=True),
        "convert": _operation(False, "convert_adapter_unavailable"),
        "edit": _operation(False, "format_edit_not_qualified"),
    }
    # Browser execution is a separate view, and still requires WB06 authorization.
    html_view = mime in {"text/html", "application/xhtml+xml"} and {"preview", "stat"}.issubset(caps) and kind in {"file", "asset"}
    operations["browserPreview"] = _operation(html_view, "browser_preview_unavailable", hint=True)
    operations["source"] = _operation(readable and (family == "text" or (kind == "file" and PurePath(str(name or "").lower()).suffix == ".svg" and editor_text_hint(name, mime))), "source_unavailable", hint=True)
    if workbook:
        operations["workbookView"] = {**_operation(preview, "preview_unavailable", hint=True), "requires": "workbook-v1"}
    if slides:
        operations["slideView"] = {**_operation(preview, "preview_unavailable", hint=True), "requires": "slides-v1"}
    operations["export"] = _operation("download" in caps, "download_unavailable")
    return {
        "version": 1, "formatId": family, "variant": "openclank-workbook-v1" if workbook else "openclank-slides-v1" if slides else None, "mimeType": mime or None,
        "sniff": {"confidence": "hint" if family != "unknown" else "unknown", "source": source},
        "adapter": {"id": "existing-preview", "version": "1"},
        "operations": operations,
        "limits": {"inputBytes": None, "expandedBytes": None, "entries": None, "depth": None, "timeMs": None},
        "readiness": {"state": "available" if preview else "unavailable", "reason": "content_not_sniffed" if preview else operations["preview"]["reason"]},
        "outputKinds": [family] if preview else [],
        "resourceRef": resource_ref,
        "providerRevision": dict(provider_revision) if provider_revision is not None else None,
        "contentSHA256": None, "decoderGeneration": None, "policyGeneration": int(policy_generation),
    }
