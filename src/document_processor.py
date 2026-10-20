# src/document_processor.py
"""Document processing: PDF/OCR extraction, text file handling, image VL analysis, user content building."""

import asyncio
import os
import logging
import mimetypes
import base64
import tempfile
import urllib.parse
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

MAX_INLINE_ATTACHMENT_CHARS = 24000
MIN_INLINE_ATTACHMENT_SLICE = 500
NATIVE_MEDIA_SOURCE_BYTES = 150 * 1024 * 1024
NATIVE_MEDIA_PAYLOAD_BYTES = 50 * 1024 * 1024


def _native_media_limit_message(display_name: str, mime: str, size: int) -> str | None:
    """Return a typed result before reading/encoding an oversized media source."""
    if size > NATIVE_MEDIA_SOURCE_BYTES:
        return (f"\n\n[Attachment unavailable: {display_name} is {size:,} bytes, "
                f"over the {NATIVE_MEDIA_SOURCE_BYTES:,}-byte source limit.]" )
    encoded = ((size + 2) // 3) * 4
    if encoded > NATIVE_MEDIA_PAYLOAD_BYTES:
        return (f"\n\n[Attachment unavailable: {display_name} encodes to {encoded:,} bytes, "
                f"over the {NATIVE_MEDIA_PAYLOAD_BYTES:,}-byte native payload limit.]" )
    return None


def _is_text_file(path: str) -> bool:
    """Check if file has text extension."""
    return any(
        path.lower().endswith(ext)
        for ext in (".txt", ".py", ".html", ".htm", ".md", ".json", ".csv", ".log", ".js", ".nix")
    )


def _process_text_file(path: str) -> str:
    """Process text file with enhanced formatting and metadata."""
    language_map = {
        ".py": "python", ".js": "javascript", ".html": "html", ".css": "css",
        ".json": "json", ".md": "markdown", ".txt": "text", ".csv": "csv",
        ".log": "log", ".sh": "bash", ".bash": "bash", ".nix": "nix",
        ".yml": "yaml", ".yaml": "yaml",
        ".xml": "xml", ".sql": "sql", ".cpp": "cpp", ".c": "c",
        ".java": "java", ".go": "go", ".rs": "rust", ".php": "php",
        ".rb": "ruby", ".ts": "typescript", ".jsx": "javascript", ".tsx": "typescript",
    }

    filename = os.path.basename(path)
    _, ext = os.path.splitext(path.lower())
    language = language_map.get(ext, "text")
    max_len = 30000 if ext != ".log" else 10000

    try:
        from src.personal_docs import read_text_file
        content = read_text_file(path)
    except Exception:
        try:
            with open(path, "rb") as f:
                raw_data = f.read()
            try:
                content = raw_data.decode("utf-8")
            except UnicodeDecodeError:
                from charset_normalizer import detect
                encoding = (detect(raw_data) or {}).get("encoding") or "utf-8"
                content = raw_data.decode(encoding, errors="replace")
        except Exception as e:
            logger.error(f"Failed to read file {path}: {e}")
            return "\n\n[Failed to read attached file]"

    try:
        file_size = os.path.getsize(path)
        size_str = f"{file_size:,}"
    except OSError:
        size_str = "unknown"

    lines = content.split("\n")
    line_count = len(lines)
    content_length = len(content)
    truncated = False

    if content_length > max_len:
        truncation_point = max_len
        search_range = min(100, content_length - max_len)
        for i in range(search_range):
            if truncation_point + i >= content_length:
                break
            if content[truncation_point + i] == "\n":
                truncation_point += i
                truncated = True
                break
        else:
            for i in range(min(100, truncation_point)):
                if content[truncation_point - i] == "\n":
                    truncation_point -= i
                    truncated = True
                    break
        content = content[:truncation_point]
        truncated = True

    header = f"\n=== File: {filename} ===\n"
    header += f"[Type: {language}, Lines: {line_count}, Size: {size_str} bytes]"

    code_extensions = {
        ".py", ".js", ".html", ".css", ".json", ".md", ".sh", ".bash", ".nix",
        ".yml", ".yaml", ".xml", ".sql", ".cpp", ".c", ".java", ".go", ".rs", ".php", ".rb",
        ".ts", ".jsx", ".tsx",
    }
    if ext in code_extensions:
        code_block = f"```{language}\n{content}"
        if truncated:
            code_block += "\n[Truncated]"
        code_block += "\n```"
        return header + "\n\n" + code_block
    else:
        result = header + "\n\n" + content
        if truncated:
            result += "\n[Truncated]"
        return result


def _process_pdf(path: str, owner: str | None = None) -> str:
    """Process PDF file with text extraction (pypdf). Uses VL model for image-heavy pages."""
    try:
        from pypdf import PdfReader
        pdf_text = ""
        reader = PdfReader(path)

        for page_num, page in enumerate(reader.pages):
            page_text = (page.extract_text() or "").strip()
            if page_text:
                pdf_text += f"\n\n[Page {page_num + 1} text]:\n{page_text}"

            # For pages with images but little text, try VL model
            try:
                images = list(page.images)
            except Exception:
                images = []
            if images and len(page_text) < 50:
                for img_index, img in enumerate(images[:3]):  # cap at 3 images per page
                    try:
                        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                            temp_img_path = tmp.name
                        try:
                            img.image.save(temp_img_path, "PNG")  # pypdf -> PIL image
                            ocr_text = analyze_image_with_vl(temp_img_path, owner=owner)
                            if ocr_text and "unavailable" not in ocr_text.lower():
                                pdf_text += f"\n\n[Page {page_num + 1} image {img_index + 1} text]: {ocr_text}"
                        finally:
                            try:
                                os.unlink(temp_img_path)
                            except OSError:
                                pass
                    except Exception as e:
                        logger.warning(f"Failed to analyze image in PDF: {e}")
                        continue

        if pdf_text:
            if len(pdf_text) > 15000:
                pdf_text = pdf_text[:15000] + "\n[PDF content truncated]"
            return f"\n\n[PDF content]:{pdf_text}"
        else:
            return "\n\n[PDF processed but no readable content found]"

    except Exception as e:
        return f"\n\n[PDF processing failed: {str(e)}]"


def _truncate_inline(text: str, limit: int = 15000) -> tuple[str, str]:
    """Cap inline document text so a huge file can't blow the model's context."""
    text = (text or "").strip()
    if len(text) > limit:
        return text[:limit], "\n[…truncated for inline context.]"
    return text, ""


def _fit_inline_attachment_text(
    text: str,
    remaining: int,
    display_name: str,
) -> tuple[str, int]:
    """Fit extracted attachment text into the shared inline attachment budget.

    Individual processors already cap single files, but multi-file batches can
    still add N capped bodies to one user turn. Keep the first files readable,
    keep later files visible by name, and mark exactly where inline content was
    reduced so the model does not silently miss attachments.
    """
    text = text or ""
    if len(text) <= remaining:
        return text, remaining - len(text)

    name = os.path.basename(display_name or "attachment")
    if remaining < MIN_INLINE_ATTACHMENT_SLICE:
        return (
            f"\n\n[Attachment omitted from inline context: {name}. "
            f"The {MAX_INLINE_ATTACHMENT_CHARS:,}-character shared inline "
            "attachment budget was already used by earlier attachments. Ask "
            "to inspect this file specifically if more detail is needed.]",
            0,
        )
    marker = (
        f"\n\n[Attachment content truncated: {name}. "
        f"Only {remaining:,} characters of this attachment fit within "
        f"the {MAX_INLINE_ATTACHMENT_CHARS:,}-character shared inline "
        "attachment budget. Ask to inspect this file specifically if more "
        "detail is needed.]"
    )
    return text[:remaining] + marker, 0


def _process_office_document(
    path: str,
    display_name: str,
    session_id: str | None = None,
    auto_opened_docs: list[Dict[str, Any]] | None = None,
    owner: str | None = None,
) -> str:
    """Extract an Office/EPUB document to Markdown via the optional markitdown dep.

    Falls back to a friendly banner when markitdown is unavailable or finds no
    text, so a missing optional dependency never breaks the chat path. When a
    session_id is provided AND the extraction succeeded, the FULL text is also
    saved as a Document so the agent can page through it via
    `manage_documents action=read offset=…` after the inline copy is capped.
    """
    from src.markitdown_runtime import (
        is_markitdown_format,
        convert_to_markdown,
        load_markitdown,
    )

    if not is_markitdown_format(path):
        return "\n\n[Attached document file]"

    markdown = convert_to_markdown(path)
    if markdown and markdown.strip():
        title = os.path.splitext(os.path.basename(path))[0]
        body, marker = _truncate_inline(markdown)

        # Persist the full extracted text as a Document. The agent's existing
        # manage_documents tool can then read past the inline cap with offset.
        doc_id = None
        if session_id:
            try:
                from src.office_doc import create_office_document
                doc_id = create_office_document(
                    session_id=session_id,
                    upload_id=os.path.basename(path),
                    title=title,
                    body_text=markdown,
                )
                if doc_id and auto_opened_docs is not None:
                    from src.database import SessionLocal, Document
                    _db = SessionLocal()
                    try:
                        _d = _db.query(Document).filter(Document.id == doc_id).first()
                        if _d:
                            auto_opened_docs.append({
                                "doc_id": _d.id,
                                "title": _d.title,
                                "language": _d.language,
                                "content": _d.current_content,
                                "version": _d.version_count,
                            })
                    finally:
                        _db.close()
            except Exception as e:
                logger.warning("Office auto-doc creation failed for %s: %s", path, e)

        # Upgrade the truncation marker with a hint pointing at the full doc so
        # the agent knows it can read the rest.
        if doc_id and marker:
            marker = (
                f"\n[…truncated for inline context — full {len(markdown):,} chars "
                f"saved as document `{doc_id}`. Use `manage_documents` with "
                f"action=read, document_id={doc_id}, offset=<N> to page through.]"
            )

        return f"\n\n[Document content — {title}]:\n{body}{marker}"

    # No content: tell the user whether to install the optional dep or whether
    # the document simply had no extractable text.
    try:
        load_markitdown()
        return f"\n\n[Attached document: {display_name} — no extractable text found.]"
    except RuntimeError as exc:
        return f"\n\n[Attached document: {display_name} — {exc}]"


# Marker that _process_pdf prepends to extracted text.
_PDF_CONTENT_MARKER = "\n\n[PDF content]:"


def strip_pdf_content_marker(text: str) -> str:
    """Remove the leading ``[PDF content]:`` wrapper that ``_process_pdf`` adds.

    Uses ``str.removeprefix`` rather than ``str.lstrip(chars)``: ``lstrip``
    treats its argument as a *set of characters*, so ``lstrip("\\n[PDF content]:")``
    keeps chewing into the page text that follows the marker. For example
    ``"\\n\\n[PDF content]:\\n\\n[Page 1 text]:\\nto the board"`` would lose the
    leading "to" because 't' and 'o' are in the marker's character set.
    """
    return (text or "").removeprefix(_PDF_CONTENT_MARKER).strip()


def _load_vl_settings() -> dict:
    """Load admin settings from disk."""
    try:
        from src.settings import load_settings
        return load_settings()
    except Exception:
        return {}


async def analyze_image_with_vl_result_async(
    image_path: str,
    owner: str | None = None,
    *,
    root_operation_id: str | None = None,
    grant_id: str | None = None,
    model_route_id: str | None = None,
    prompt: str = "Describe this image in detail",
) -> dict:
    """Analyze an image through the owner-bound vision.describe operation."""

    logger.info("Analyzing image through the managed vision route")
    settings = _load_vl_settings()
    if not settings.get("vision_enabled", True):
        return {
            "text": "[Vision is disabled — enable it in Settings → Vision]",
            "model": "",
        }
    selected_route = (
        str(model_route_id or settings.get("vision_model") or "").strip()
        or None
    )
    try:
        from src.openclank.modality_facade import describe_image_path

        result = await describe_image_path(
            owner=owner or "",
            image_path=image_path,
            prompt=prompt,
            model_route_id=selected_route,
            root_operation_id=root_operation_id,
            grant_id=grant_id,
        )
        return {
            "text": str(result.output.get("text") or ""),
            "model": result.model_route_id,
        }
    except Exception as exc:
        logger.warning(
            "Managed vision analysis unavailable (%s)",
            type(exc).__name__,
        )
        return {
            "text": "[VL model unavailable - image not analyzed]",
            "model": "",
        }


def analyze_image_with_vl_result(
    image_path: str,
    owner: str | None = None,
    *,
    root_operation_id: str | None = None,
    grant_id: str | None = None,
    model_route_id: str | None = None,
) -> dict:
    """Synchronous compatibility wrapper for non-async import/OCR workers.

    Async request and chat paths must await analyze_image_with_vl_result_async
    so execution stays on the application supervisor's event loop.
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            analyze_image_with_vl_result_async(
                image_path,
                owner=owner,
                root_operation_id=root_operation_id,
                grant_id=grant_id,
                model_route_id=model_route_id,
            )
        )
    logger.warning(
        "Synchronous vision compatibility call refused on an active event loop"
    )
    return {
        "text": "[VL model unavailable - image not analyzed]",
        "model": "",
    }


def analyze_image_with_vl(image_path: str, owner: str | None = None) -> str:
    """Compatibility wrapper for synchronous document import workers."""

    return analyze_image_with_vl_result(image_path, owner=owner).get("text", "")


def build_user_content(
    text: str,
    attachment_ids: list[str] | None,
    upload_dir: str,
    upload_handler,
    session_id: str | None = None,
    auto_opened_docs: list[Dict[str, Any]] | None = None,
    owner: str | None = None,
    resolved_uploads: dict[str, Dict[str, Any]] | None = None,
    structured_resources: bool = False,
    extract_attachments: bool = True,
) -> str | List[Dict[str, Any]]:
    """Build user content with attachments (text, images, audio, documents).

    If session_id is provided and an attached PDF contains AcroForm fields,
    a markdown Document is auto-created so the user can edit the form in the
    editor. When `auto_opened_docs` is supplied, an entry is appended for each
    such doc so the chat route can emit a `doc_update` SSE event and the
    frontend can switch to the new doc immediately.
    """
    content = [{"type": "text", "text": text}]
    inline_attachment_remaining = MAX_INLINE_ATTACHMENT_CHARS

    for fid in attachment_ids or []:
        upload_info = (resolved_uploads or {}).get(fid)
        if upload_info is None and hasattr(upload_handler, "resolve_upload"):
            upload_info = upload_handler.resolve_upload(fid, owner=owner)
        if upload_info is None:
            logger.warning(f"Attachment {fid} not found or not authorized")
            continue

        path = upload_info.get("path")
        if not path or not os.path.exists(path):
            logger.warning(f"Attachment {fid} path is missing")
            continue
        if hasattr(upload_handler, "_inside_upload_dir") and not upload_handler._inside_upload_dir(path):
            logger.warning(f"Attachment {fid} path is outside upload directory: {path}")
            continue
        if not hasattr(upload_handler, "_inside_upload_dir") and not upload_handler.inside_base_dir(path):
            logger.warning(f"Attachment {fid} path is outside base directory: {path}")
            continue

        _, ext = os.path.splitext(path.lower())
        mime = upload_info.get("mime") or mimetypes.guess_type(path)[0] or "application/octet-stream"
        display_name = upload_info.get("name") or upload_info.get("original_name") or path
        try:
            source_size = os.path.getsize(path)
        except OSError:
            source_size = -1
        if source_size < 0:
            limit_message = f"\n\n[Attachment unavailable: {display_name} could not be statted safely.]"
        else:
            limit_message = _native_media_limit_message(display_name, mime, source_size)
        if limit_message:
            content[0]["text"] += limit_message
            continue

        if upload_handler.is_image_file(display_name, mime):
            try:
                with open(path, "rb") as image_file:
                    encoded_string = base64.b64encode(image_file.read()).decode("utf-8")
                # Extensionless uploads (e.g. a pasted screenshot) have no ext,
                # so fall back to the resolved MIME subtype rather than emitting
                # an invalid "data:image/;base64," with an empty subtype.
                image_format = (mime.split("/", 1)[1] if mime.startswith("image/") else "png")
                if image_format == "jpg":
                    image_format = "jpeg"
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/{image_format};base64,{encoded_string}"},
                })
            except Exception as e:
                logger.error(f"Failed to encode image {fid}: {e}")
                if content and content[0]["type"] == "text":
                    content[0]["text"] += "\n\n[Image attached but could not be processed]"
                else:
                    content.insert(0, {"type": "text", "text": "[Image attached but could not be processed]"})

        elif upload_handler.is_audio_file(display_name, mime):
            try:
                with open(path, "rb") as audio_file:
                    encoded_string = base64.b64encode(audio_file.read()).decode("utf-8")
                audio_format = ext[1:] or (mime.split("/", 1)[1] if mime.startswith("audio/") else "mpeg")
                content.append({
                    "type": "audio",
                    "audio": {"url": f"data:audio/{audio_format};base64,{encoded_string}"},
                })
            except Exception as e:
                logger.error(f"Failed to encode audio {fid}: {e}")
                if content and content[0]["type"] == "text":
                    content[0]["text"] += "\n\n[Audio attached but could not be processed]"
                else:
                    content.insert(0, {"type": "text", "text": "[Audio attached but could not be processed]"})

        elif upload_handler.is_document_file(display_name, mime):
            if not extract_attachments:
                extracted_text = f"\n\n[Attachment retained as an original resource: {display_name}]"
            elif mime == "application/pdf":
                extracted_text = None
                if session_id:
                    try:
                        from src.pdf_forms import has_form_fields, extract_fields
                        from src.pdf_form_doc import (
                            save_field_sidecar,
                            create_form_markdown_document,
                            create_plain_pdf_document,
                        )
                        title = os.path.splitext(os.path.basename(display_name))[0]
                        # Pull the PDF prose once — used as either intro_text
                        # (form path) or the doc body (plain path).
                        try:
                            pdf_body_text = strip_pdf_content_marker(_process_pdf(path, owner=owner))
                        except Exception:
                            pdf_body_text = None

                        is_form = False
                        try:
                            is_form = has_form_fields(path)
                        except Exception as e:
                            logger.warning(f"PDF form detection failed for {path}: {e}")

                        # Inline the PDF body in the chat content too. Without
                        # this, the assistant only saw the "PDF attached"
                        # banner and had no idea what was inside — even though
                        # the sidebar Document held the full extracted text.
                        # Cap the inline copy so a multi-hundred-page PDF
                        # doesn't blow the model's context; the sidebar still
                        # carries the full body for direct reference.
                        _MAX_INLINE_CHARS = 15000
                        body_for_chat = (pdf_body_text or "").strip()
                        truncated_marker = ""
                        if body_for_chat and len(body_for_chat) > _MAX_INLINE_CHARS:
                            body_for_chat = body_for_chat[:_MAX_INLINE_CHARS]
                            truncated_marker = (
                                "\n[…truncated for inline context — full text "
                                "available in the document viewer.]"
                            )

                        if is_form:
                            fields = extract_fields(path)
                            save_field_sidecar(path, fields)
                            doc_id = create_form_markdown_document(
                                session_id=session_id,
                                fields=fields,
                                upload_id=os.path.basename(path),
                                title=title,
                                intro_text=pdf_body_text,
                            )
                            if doc_id:
                                extracted_text = (
                                    f"\n\n[Form attached: {title} — {len(fields)} fields. "
                                    f"Opened in editor — edit the values there and use "
                                    f"the Export PDF button when done.]"
                                )
                                if body_for_chat:
                                    extracted_text += (
                                        f"\n\n[PDF content — {title}]:\n{body_for_chat}{truncated_marker}"
                                    )
                        else:
                            doc_id = create_plain_pdf_document(
                                session_id=session_id,
                                upload_id=os.path.basename(path),
                                title=title,
                                body_text=pdf_body_text,
                            )
                            if doc_id:
                                extracted_text = (
                                    f"\n\n[PDF attached: {title} — opened in document viewer.]"
                                )
                                if body_for_chat:
                                    extracted_text += (
                                        f"\n\n[PDF content — {title}]:\n{body_for_chat}{truncated_marker}"
                                    )

                        if doc_id and auto_opened_docs is not None:
                            from src.database import SessionLocal, Document
                            _db = SessionLocal()
                            try:
                                _d = _db.query(Document).filter(
                                    Document.id == doc_id
                                ).first()
                                if _d:
                                    auto_opened_docs.append({
                                        "doc_id": _d.id,
                                        "title": _d.title,
                                        "language": _d.language,
                                        "content": _d.current_content,
                                        "version": _d.version_count,
                                    })
                            finally:
                                _db.close()
                    except Exception as e:
                        logger.warning(f"PDF auto-doc creation failed for {path}: {e}")
                if extracted_text is None:
                    extracted_text = _process_pdf(path, owner=owner)
            elif mime.startswith("text/") or _is_text_file(path):
                extracted_text = _process_text_file(path)
            else:
                extracted_text = _process_office_document(
                    path,
                    display_name,
                    session_id=session_id,
                    auto_opened_docs=auto_opened_docs,
                    owner=owner,
                )

            if structured_resources:
                with open(path, "rb") as resource_file:
                    blob = base64.b64encode(resource_file.read()).decode("ascii")
                content.append({
                    "type": "resource",
                    "resource": {
                        "uri": "attachment://" + urllib.parse.quote(
                            f"{fid}/{os.path.basename(display_name)}"
                        ),
                        "mimeType": mime,
                        "blob": blob,
                    },
                })
            else:
                extracted_text, inline_attachment_remaining = _fit_inline_attachment_text(
                    extracted_text,
                    inline_attachment_remaining,
                    display_name,
                )
                if content and content[0]["type"] == "text":
                    content[0]["text"] += extracted_text
                else:
                    content.insert(0, {"type": "text", "text": extracted_text.lstrip()})
        else:
            if structured_resources:
                with open(path, "rb") as resource_file:
                    blob = base64.b64encode(resource_file.read()).decode("ascii")
                content.append({
                    "type": "resource",
                    "resource": {
                        "uri": "attachment://" + urllib.parse.quote(
                            f"{fid}/{os.path.basename(display_name)}"
                        ),
                        "mimeType": mime,
                        "blob": blob,
                    },
                })
            elif content and content[0]["type"] == "text":
                content[0]["text"] += "\n\n[Attached non-text file]"
            else:
                content.insert(0, {"type": "text", "text": "[Attached non-text file]"})

    has_media = any(
        item.get("type") in ["image_url", "audio", "resource", "resource_link"]
        for item in content if isinstance(item, dict)
    )
    if not has_media and content:
        combined_text = ""
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                combined_text += item.get("text", "")
        return combined_text.strip()

    return content
