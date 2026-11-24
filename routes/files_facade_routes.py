"""Versioned provider-neutral API for the unified Files namespace."""

from __future__ import annotations

import asyncio
import base64
import email.utils
import hashlib
import json
import os
import sys
import re
import stat
import time
import unicodedata
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Literal, Mapping
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError, model_validator

from core.database import SessionLocal
from src.openclank.file_policy import FilePolicyError, FilePolicyRepository
from src.openclank.chat_lifecycle import ChatLifecycleService
from src.openclank.files_facade import FilesFacade, FilesFacadeError, ProviderContext
from src.openclank.files_host_provider import HostFilesProvider
from src.openclank.files_principal import files_principal
from src.openclank.files_managed_providers import (
    CopalFilesProvider,
    FilesImagesProvider,
    LibraryFilesProvider,
)
from src.openclank.files_service_client import client_for_owner, close_all_clients
from src.openclank.filesystem_registry import FilesystemRootRegistry
from src.openclank.resource_refs import resolve_resource_ref
from src.openclank.copal_treehouse_repository import TreeHouseRepository
from src.openclank.media_attachment_targets import (
    HostDocumentAttachmentTarget,
    adopt_loose_media_for_workspace,
)
from src.openclank.history_capture import trusted_tool_context
from src.openclank.treehouse_files_adapter import TreeHouseLessonAttachmentTarget, TreeHouseSubmissionAttachmentTarget
from src.constants import DATA_DIR
from src.openclank.workspace_policy_service import (
    WorkspacePolicyServiceError,
    bind_workspace_path,
    resolve_workspace_binding,
    workspace_view,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChildrenRequest(_StrictModel):
    parent_ref: str = Field(min_length=8, max_length=16_384)
    cursor: str | None = Field(default=None, max_length=16_384)
    limit: StrictInt = Field(default=100, ge=1, le=200)
    sort: dict[str, Any] = Field(default_factory=dict)
    query: str = Field(default="", max_length=512)


class StatRequest(_StrictModel):
    resource_ref: str = Field(min_length=8, max_length=16_384)
    purpose: Literal['inspect', 'inspect_elf', 'inspect_pe', 'inspect_macho', 'inspect_sqlite', 'snapshot', 'checkpoint'] | None = None


class SelectedOutputItem(_StrictModel):
    ref: str = Field(min_length=8,max_length=16384)
    id: str = Field(min_length=1,max_length=256)
    revision: dict[str,str]


class SelectedOutputPreview(_StrictModel):
    items: list[SelectedOutputItem] = Field(min_length=1,max_length=16)
    omissions: list[str] = Field(max_length=32)
    license: str = Field(max_length=2048)
    attribution: str = Field(max_length=2048)
    privacy: StrictBool


class SelectedOutputExport(_StrictModel):
    previewToken: str = Field(min_length=8,max_length=600000)
    firstRef: str = Field(min_length=8,max_length=16384)


class CheckpointCapabilityRequest(_StrictModel):
    root_ref: str = Field(min_length=8,max_length=16_384)
    root_revision: dict[str,str]


class CheckpointRegistrationItem(_StrictModel):
    resource_ref: str = Field(min_length=8,max_length=16_384)
    expected_revision: dict[str,str]


class CheckpointRegistrationRequest(_StrictModel):
    root_ref: str = Field(min_length=8,max_length=16_384)
    root_revision: dict[str,str]
    items: list[CheckpointRegistrationItem] = Field(min_length=1,max_length=16)
    request_id: str = Field(min_length=16,max_length=80,pattern=r"^[A-Za-z0-9_-]+$")
    status_only: StrictBool = False


class CheckpointCompareRequest(_StrictModel):
    root_ref: str = Field(min_length=8,max_length=16_384)
    original_root_revision: dict[str,str]
    items: list[CheckpointRegistrationItem] = Field(min_length=1,max_length=16)
    request_id: str = Field(min_length=16,max_length=80,pattern=r"^[A-Za-z0-9_-]+$")
    resource_id: str = Field(min_length=1,max_length=256)


class CheckpointRestorePreviewRequest(_StrictModel):
    version: StrictInt = Field(ge=1,le=1)
    checkpoint_id: str = Field(min_length=16,max_length=80,pattern=r"^[A-Za-z0-9_-]+$")
    resource_id: str = Field(pattern=r"^resource-[0-9a-f]{64}$")


class CheckpointRestoreConfirmRequest(CheckpointRestorePreviewRequest):
    request_id: str = Field(min_length=16,max_length=80,pattern=r"^[A-Za-z0-9_-]+$")
    expected_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class CheckpointRestoreStatusRequest(_StrictModel):
    version: StrictInt = Field(ge=1,le=1)
    request_id: str = Field(min_length=16,max_length=80,pattern=r"^[A-Za-z0-9_-]+$")
    expected_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class InspectFormatRequest(_StrictModel):
    resource_ref: str = Field(min_length=8, max_length=16_384)
    expected_revision: dict[str, str]


class IwiCopyRequest(_StrictModel):
    operation_id: str = Field(min_length=1, max_length=128)
    item_id: str = Field(min_length=1, max_length=128)
    generation: StrictInt = Field(ge=0)
    source_ref: str = Field(min_length=8, max_length=16_384)
    revision: dict[str, str]
    source_sha256: str = Field(min_length=64, max_length=64)
    decoder: str = Field(min_length=1, max_length=128)
    png_sha256: str = Field(min_length=64, max_length=64)
    destination_ref: str = Field(min_length=8, max_length=16_384)
    name: str = Field(min_length=1, max_length=240)


class AnalysisInitializeRequest(_StrictModel):
    workspaceID: str = Field(min_length=1, max_length=256)
    operationID: str = Field(min_length=16, max_length=128)


class AnalysisObservationRequest(_StrictModel):
    workspaceID: str = Field(min_length=1, max_length=256)
    sourceRef: str = Field(min_length=8, max_length=16384)
    sourceRevision: dict[str, str]
    annotationID: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(max_length=16384)
    address: dict[str, Any] | None = None
    expectedRevision: dict[str, str] | None = None


class AnalysisPreviewRequest(_StrictModel):
    workspaceID: str = Field(min_length=1, max_length=256)
    sourceRef: str = Field(min_length=8, max_length=16384)
    annotationID: str | None = Field(default=None, min_length=1, max_length=64)


class AnalysisCompletedResultRequest(_StrictModel):
    provisional: str = Field(min_length=8, max_length=16384)
    sourceRef: str = Field(min_length=8, max_length=16384)


class AnalysisDossierSaveRequest(_StrictModel):
    workspaceID: str = Field(min_length=1, max_length=256)
    sourceRef: str = Field(min_length=8, max_length=16384)
    completedReceipt: str = Field(min_length=8, max_length=16384)
    dossierID: str = Field(min_length=1, max_length=64)
    expectedRevision: dict[str, str] | None = None


class AnalysisElfSaveRequest(_StrictModel):
    workspaceID: str = Field(min_length=1, max_length=256)
    sourceRef: str = Field(min_length=8, max_length=16384)
    sourceRevision: dict[str, str]
    dossierID: str = Field(min_length=1, max_length=64)
    expectedRevision: dict[str, str] | None = None


class AnalysisDossierReadRequest(_StrictModel):
    workspaceID: str = Field(min_length=1, max_length=256)
    dossierID: str = Field(min_length=1, max_length=64)
    sourceRef: str | None = Field(default=None, min_length=8, max_length=16384)


class AnalysisCandidatesRequest(_StrictModel):
    workspaceID: str = Field(min_length=1, max_length=256)
    sourceRef: str = Field(min_length=8, max_length=16384)
    stableChatID: str = Field(min_length=1, max_length=512)
    cursor: str | None = Field(default=None, min_length=1, max_length=1024)


class AnalysisFunctionRequest(_StrictModel):
    workspaceID: str = Field(min_length=1, max_length=256)
    dossierID: str = Field(min_length=1, max_length=64)
    expectedDossierRevision: dict[str, str]
    selector: dict[str, Any]
    sourceRef: str | None = Field(default=None, min_length=8, max_length=16384)


class AnalysisStaticOverlayRequest(_StrictModel):
    workspaceID: str = Field(min_length=1, max_length=256)
    dossierID: str = Field(min_length=1, max_length=64)
    expectedDossierRevision: dict[str, str]
    sourceRef: str | None = Field(default=None, min_length=8, max_length=16384)


def _analysis_ingest_bytes(value: str) -> bytes:
    """Strict base64 for the ingest upload; the bytes go only to AnalysisProjects.save_ingest."""
    data = base64.b64decode(value, validate=True)
    if not 1 <= len(data) <= 1_048_576:
        raise ValueError('ingest size')
    return data


class AnalysisIngestSaveRequest(_StrictModel):
    workspaceID: str = Field(min_length=1, max_length=256)
    sourceRef: str = Field(min_length=8, max_length=16384)
    sourceRevision: dict[str, str]
    adapterID: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    data_b64: str = Field(min_length=4, max_length=1_398_104)
    expectedRevision: dict[str, str] | None = None

    @model_validator(mode='after')
    def _data(self):
        _analysis_ingest_bytes(self.data_b64)
        return self


class AnalysisIngestOverlayRequest(_StrictModel):
    workspaceID: str = Field(min_length=1, max_length=256)
    ingestID: str = Field(min_length=1, max_length=64)
    sourceRef: str | None = Field(default=None, min_length=8, max_length=16384)


class AnalysisFunctionExportRequest(_StrictModel):
    workspaceID: str = Field(min_length=1, max_length=256)
    dossierID: str = Field(min_length=1, max_length=64)
    expectedDossierRevision: dict[str, str]
    selectors: list[dict[str, Any]] = Field(min_length=1, max_length=16)
    exportID: str = Field(min_length=1, max_length=64)
    expectedExportRevision: dict[str, str] | None = None
    sourceRef: str | None = Field(default=None, min_length=8, max_length=16384)


class AnalysisFunctionExportReadRequest(_StrictModel):
    workspaceID: str = Field(min_length=1, max_length=256)
    exportID: str = Field(min_length=1, max_length=64)


class AnalysisSnapshotCaptureRequest(_StrictModel):
    workspaceID: str = Field(min_length=1,max_length=256)
    rootRef: str = Field(min_length=8,max_length=16384)
    rootRevision: dict[str,str]
    mode: Literal['folder','selected']
    selectedRefs: list[str] = Field(default_factory=list,max_length=1024)
    dossierID: str = Field(min_length=1,max_length=64)
    expectedDossierRevision: dict[str,str]
    sourceRef: str = Field(min_length=8,max_length=16384)
    limits: dict[str,StrictInt] | None = None


class AnalysisSnapshotCompareRequest(_StrictModel):
    workspaceID: str = Field(min_length=1,max_length=256)
    previewToken: str | None = Field(default=None,min_length=8,max_length=16384)
    memoryID: str | None = Field(default=None,min_length=1,max_length=512)
    rootRef: str = Field(min_length=8,max_length=16384)
    rootRevision: dict[str,str]


async def _analysis_request_body(request: Request, model, limit: int = 262144):
    """Bound and decode raw JSON before the existing closed DTO validation."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    try:
        if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json':
            raise ValueError('invalid content type')
        declared = request.headers.get('content-length')
        if declared is not None and (not declared.isdigit() or int(declared) > limit):
            raise ValueError('invalid body length')
        chunks = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > limit:
                raise ValueError('body too large')
            chunks.append(chunk)
        raw = b''.join(chunks).decode('utf-8', 'strict')
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid JSON')))
        return model.model_validate(value)
    except (ValueError, UnicodeError, ValidationError, RecursionError):
        raise HTTPException(422, 'Invalid analysis request')


async def _shared_text_request_body(request: Request, model, *, maximum: int):
    """Bound the entire wire body before parsing a shared-text request."""
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ValueError('duplicate key')
            result[key]=value
        return result
    try:
        if request.headers.get('content-type','').split(';',1)[0].strip().lower()!='application/json':raise ValueError('invalid content type')
        declared=request.headers.get('content-length')
        if declared is not None and (not declared.isdigit() or int(declared)>maximum):raise ValueError('invalid length')
        data=bytearray()
        async for chunk in request.stream():
            if len(data)+len(chunk)>maximum:raise ValueError('body too large')
            data.extend(chunk)
        value=json.loads(data.decode('utf-8','strict'),object_pairs_hook=pairs,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid number')))
        return model.model_validate(value)
    except (ValueError,UnicodeError,ValidationError,RecursionError):
        raise HTTPException(422,'Invalid shared text request')


class SharedTextReadRequest(_StrictModel):
    version: Literal[1]
    publication_id: str = Field(min_length=1, max_length=128)


class SharedFeedbackSaveRequest(SharedTextReadRequest):
    request_key: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=4096)
    expected_revision: dict
    annotation_revision: StrictInt = Field(ge=0)
    feedback_id: str | None = Field(default=None,pattern=r'^[a-f0-9]{32}$')
    comment_revision: StrictInt | None = Field(default=None,ge=1)
    anchor: dict


class SharedTextCommitRequest(SharedTextReadRequest):
    operation_id: str = Field(min_length=1, max_length=256)
    base_sha256: str = Field(min_length=64, max_length=64)
    base_text: str = Field(max_length=262144)
    edits: list[dict[str, Any]] = Field(min_length=1, max_length=64)
    client_digest: str = Field(min_length=64, max_length=64)


class SharedWorkbookCommitRequest(SharedTextReadRequest):
    operation_id: str = Field(min_length=1, max_length=256)
    base_sha256: str = Field(min_length=64, max_length=64)
    base_text: str = Field(max_length=262144)
    cells: list[dict[str, Any]] = Field(min_length=1, max_length=64)
    client_digest: str = Field(min_length=64, max_length=64)


class SharedSlidesCommitRequest(SharedTextReadRequest):
    operation_id: str = Field(min_length=1, max_length=256)
    base_sha256: str = Field(min_length=64, max_length=64)
    base_text: str = Field(max_length=262144)
    objects: list[dict[str, Any]] = Field(min_length=1, max_length=64)
    client_digest: str = Field(min_length=64, max_length=64)


class SharedTextStatusRequest(SharedTextReadRequest):
    operation_id: str = Field(min_length=1, max_length=256)
    client_digest: str = Field(min_length=64, max_length=64)


class HumanMediaPrepareRequest(_StrictModel):
    sessionID: str = Field(min_length=1, max_length=256)
    connectionID: str = Field(min_length=1, max_length=256)
    recipeRef: str = Field(min_length=8, max_length=16384)
    destinationRef: str = Field(min_length=8, max_length=16384)
    destinationName: str = Field(min_length=1, max_length=240)
    parameters: dict[str, Any]
    outputProfile: str = 'png'


class HumanMediaProfilesRequest(_StrictModel):
    sessionID: str = Field(min_length=1, max_length=256)
    operation: str = Field(min_length=1, max_length=128)


class HumanMediaOperationPrepareRequest(HumanMediaProfilesRequest):
    profile_id: str = Field(min_length=1, max_length=256)
    inputs: list[dict[str, Any]]
    parameters: dict[str, Any]
    destination: dict[str, Any]
    profile_revision: str | None = Field(default=None, max_length=128)


class HumanMediaInspectRequest(_StrictModel):
    sessionID: str = Field(min_length=1, max_length=256)
    recipeRef: str = Field(min_length=8, max_length=16384)


class SprixenMediaPrepareRequest(_StrictModel):
    sessionID: str = Field(min_length=1, max_length=256)
    connectionID: str = Field(min_length=1, max_length=256)
    providerAccountID: str = Field(min_length=1, max_length=256)
    destinationRef: str = Field(min_length=8, max_length=16384)
    destinationName: str = Field(min_length=1, max_length=255)
    parameters: dict[str, Any]


class MeshyMediaPrepareRequest(SprixenMediaPrepareRequest):
    pass


class TripoMediaPrepareRequest(SprixenMediaPrepareRequest):
    pass


class MeshyRefinePrepareRequest(_StrictModel):
    sessionID: str = Field(min_length=1, max_length=256)
    sourceJobID: str = Field(min_length=1, max_length=256)
    destinationRef: str = Field(min_length=8, max_length=16384)
    destinationName: str = Field(min_length=1, max_length=255)
    parameters: dict[str, Any]


class BlenderMediaPrepareRequest(_StrictModel):
    sessionID: str = Field(min_length=1, max_length=256)
    connectionID: str = Field(min_length=1, max_length=256)
    sourceRef: str = Field(min_length=8, max_length=16384)
    destinationRef: str = Field(min_length=8, max_length=16384)
    destinationName: str = Field(min_length=1, max_length=255)


class RecordingAudioIntent(_StrictModel):
    microphone: StrictBool = False
    systemOutput: StrictBool = False

class RecordingPrepareRequest(_StrictModel):
    sessionID: str = Field(min_length=1, max_length=256)
    destinationRef: str = Field(min_length=8, max_length=16384)
    destinationName: str = Field(min_length=1, max_length=240)
    audioIntent: RecordingAudioIntent | None = None
    cropIntent: StrictBool = False
    controlSession: StrictBool = False

class RecordingCropRect(_StrictModel):
    x: StrictInt = Field(ge=0)
    y: StrictInt = Field(ge=0)
    width: StrictInt = Field(gt=0)
    height: StrictInt = Field(gt=0)

class RecordingCropSelection(_StrictModel):
    mode: Literal['full','crop']
    basis_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    rect: RecordingCropRect | None

class RecordingAudioSelectRequest(_StrictModel):
    jobID: str = Field(min_length=1, max_length=128)
    revision: StrictInt = Field(ge=1)
    selectionID: str = Field(pattern=r'^[a-f0-9]{32}$')
    generation: str = Field(pattern=r'^[a-f0-9]{32}$')
    microphone: str | None = Field(..., pattern=r'^[a-f0-9]{32}$')
    systemOutput: str | None = Field(..., pattern=r'^[a-f0-9]{32}$')
    crop: RecordingCropSelection | None = None

class RecordingStopRequest(_StrictModel):
    jobID: str = Field(min_length=1, max_length=128)
    revision: StrictInt = Field(ge=1)
    stopID: str = Field(min_length=1, max_length=128)


class LatexInputRequest(_StrictModel):
    ref: str = Field(min_length=8, max_length=16384)
    revision: dict[str, str]

class LatexDependencyRequest(LatexInputRequest):
    relative_name: str = Field(min_length=1, max_length=240)

class LatexPrepareRequest(_StrictModel):
    sessionID: str = Field(min_length=1, max_length=256)
    source: LatexInputRequest
    dependencies: list[LatexDependencyRequest] = Field(default_factory=list, max_length=31)
    destinationRef: str = Field(min_length=8, max_length=16384)
    destinationName: str = Field(min_length=1, max_length=240)


class HumanMediaRunRequest(_StrictModel):
    jobID: str = Field(min_length=1, max_length=128)
    revision: StrictInt = Field(ge=1)
    parameters: dict[str, Any]


class HumanMediaJobRequest(_StrictModel):
    jobID: str = Field(min_length=1, max_length=128)


class HumanMediaRecoveryPreviewRequest(_StrictModel):
    jobID: str = Field(min_length=1, max_length=128)
    destinationRef: str = Field(min_length=8, max_length=16384)
    destinationName: str = Field(min_length=1, max_length=240)


class BlenderRigPrepareRequest(BlenderMediaPrepareRequest):
    selected: dict[str, Any]


class BlenderRigInspectRequest(_StrictModel):
    sessionID: str = Field(min_length=1, max_length=256)
    sourceRef: str = Field(min_length=8, max_length=16384)


class SpriteSheetPrepareRequest(_StrictModel):
    sessionID: str = Field(min_length=1, max_length=256)
    sourceRefs: list[str] = Field(min_length=4, max_length=4)
    destinationRef: str = Field(min_length=8, max_length=16384)
    destinationName: str = Field(min_length=1, max_length=240)
    parameters: dict[str, Any]


class SpriteSheetCropsPrepareRequest(_StrictModel):
    sessionID: str = Field(min_length=1,max_length=256)
    sourceRef: str = Field(min_length=8,max_length=16384)
    destinationRef: str = Field(min_length=8,max_length=16384)
    destinationName: str = Field(min_length=1,max_length=240)
    rectangles: list[dict[str,StrictInt]] = Field(min_length=4,max_length=4)
    parameters: dict[str, Any]


class HumanMediaRecoverySaveRequest(_StrictModel):
    jobID: str = Field(min_length=1, max_length=128)
    revision: StrictInt = Field(ge=1)
    recoveryID: str = Field(min_length=1, max_length=128)


class BinderResolveRequest(_StrictModel):
    open_home: StrictBool = False
    binder_id: str = Field(min_length=1, max_length=128)
    expected_catalog_revision: StrictInt = Field(ge=0)
    expected_descriptor_revision: StrictInt = Field(ge=0)
    expected_descriptor: dict[str, Any]


class BinderCreateRequest(_StrictModel):
    resource_ref: str = Field(min_length=8, max_length=16_384)
    name: str = Field(min_length=1, max_length=256)
    expected_account_id: str = Field(min_length=1, max_length=256)
    expected_catalog_revision: StrictInt = Field(ge=0)
    make_default: StrictBool = False


class BinderPreviewRequest(_StrictModel):
    source_ref: str = Field(min_length=8, max_length=16_384)
    untouched_ref: str = Field(min_length=8, max_length=16_384)
    expected_account_id: str = Field(min_length=1, max_length=256)
    expected_catalog_revision: StrictInt = Field(ge=0)
    binder_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=256)


class BinderApplyRequest(BinderPreviewRequest):
    operation_id: str = Field(min_length=36, max_length=36)
    source_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    untouched_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    root_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog: dict[str, Any]


class SearchRequest(_StrictModel):
    query: str = Field(min_length=1, max_length=512)
    limit: StrictInt = Field(default=100, ge=1, le=200)
    sort: dict[str, Any] = Field(default_factory=dict)


class WorkspaceContentSearchRequest(_StrictModel):
    workspace_id: str = Field(min_length=1, max_length=256)
    expected_revision: StrictInt = Field(ge=1, le=9_007_199_254_740_991)
    query: str = Field(min_length=1, max_length=256)


class RevisionRequest(_StrictModel):
    kind: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=512)


class ManagedTransferRoot(_StrictModel):
    resource_ref: str = Field(min_length=8, max_length=16_384)
    expected_revision: RevisionRequest | None = None


class TrashEntryRequest(_StrictModel):
    # Mirrors the Host provider's TrashEntry (files_host_provider._trash_action): exactly these five strings.
    id: str = Field(min_length=1, max_length=128)
    root_id: str = Field(min_length=1, max_length=128)
    original_path: str = Field(min_length=1, max_length=4096)
    trashed_path: str = Field(min_length=1, max_length=4096)
    fingerprint: str = Field(min_length=1, max_length=128)


class ActionRequest(_StrictModel):
    resource_ref: str = Field(min_length=8, max_length=16_384)
    action: Literal["open", "favorite.set", "archive.set", "rename", "move", "trash", "restore"]
    action_id: str | None = Field(default=None, min_length=1, max_length=128)
    args: dict[str, Any] = Field(default_factory=dict, max_length=2)
    expected_revision: RevisionRequest | None = None
    managed_root: ManagedTransferRoot | None = None


class OpenResourceRequest(_StrictModel):
    resource_ref: str = Field(min_length=8, max_length=16_384)
    expected_revision: dict[str, str] | None = None
    purpose: Literal['inspect'] | None = None


class HostOpenRequest(_StrictModel):
    resource_ref: str = Field(min_length=8, max_length=16_384)
    app_id: str = Field(min_length=1, max_length=512)


class HostRevisionRequest(_StrictModel):
    kind: Literal["hostFingerprint"]
    value: str = Field(min_length=1, max_length=512)


class RevealRequest(StatRequest):
    expected_revision: HostRevisionRequest | None = None


class SaveResourceRequest(_StrictModel):
    resource_ref: str = Field(min_length=8, max_length=16_384)
    expected_revision: HostRevisionRequest
    text: str = Field(max_length=8 * 1024 * 1024)


class CreateResourceRequest(_StrictModel):
    parent_ref: str = Field(min_length=8, max_length=16_384)
    name: str = Field(min_length=1, max_length=240)
    text: str = Field(max_length=8 * 1024 * 1024)
    action_id: str | None = Field(default=None, min_length=1, max_length=128)
    operation_id: str | None = Field(default=None, min_length=1, max_length=128)
    expected_parent_revision: dict[str, str] | None = None
    status_only: bool = False


class CreateDirectoryRequest(_StrictModel):
    parent_ref: str = Field(min_length=8, max_length=16_384)
    name: str = Field(min_length=1, max_length=240)
    operation_id: str = Field(min_length=1, max_length=128)
    generation: StrictInt = Field(ge=0)
    expected_revision: HostRevisionRequest | None = None
    collision: Literal["fail", "reuse"] = "fail"


class WorkspaceResourceRequest(_StrictModel):
    resource_ref: str = Field(min_length=8, max_length=16_384)
    purpose: Literal["app_folder", "agent_workspace"]


class CodeGraphTargetRequest(_StrictModel):
    resource_ref: str = Field(min_length=8, max_length=16_384)
    workspace_id: str | None = Field(default=None, min_length=1, max_length=256)
    expected_workspace_revision: StrictInt | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def complete_workspace_capture(self):
        if (self.workspace_id is None) != (self.expected_workspace_revision is None):
            raise ValueError("Workspace identity and revision must be supplied together")
        return self


class WorkspaceUpdateRequest(_StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    archived: bool | None = None
    expected_revision: StrictInt | None = Field(default=None, ge=1)


class WorkspacePathResourceRequest(_StrictModel):
    workspace_id: str = Field(min_length=1, max_length=256)
    relative_path: str = Field(default="", max_length=4096)
    capture_layout: Literal["movs", "pics"] | None = None

    @model_validator(mode="after")
    def exclusive_capture_layout(self):
        if self.capture_layout is not None and self.relative_path:
            raise ValueError("capture layout cannot include a relative path")
        return self


class PlaceRequest(_StrictModel):
    resource_ref: str = Field(min_length=8, max_length=16_384)


class SavedSearchRequest(_StrictModel):
    name: str = Field(min_length=1, max_length=200)
    provider: Literal["all", "host", "copal", "files", "library"] = "all"
    query: str = Field(min_length=1, max_length=512)
    sort: dict[str, Any] = Field(default_factory=dict)


class WatchResourceRequest(_StrictModel):
    resource_ref: str = Field(min_length=8, max_length=16_384)


class TransferSource(_StrictModel):
    item_id: str = Field(min_length=1, max_length=128)
    resource_ref: str = Field(min_length=8, max_length=16_384)
    expected_revision: RevisionRequest | None = None


class AttachmentRevision(_StrictModel):
    kind: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=512)


class AttachmentSourceRequest(_StrictModel):
    resource_ref: str | None = Field(default=None, min_length=8, max_length=16_384)
    expected_revision: AttachmentRevision | None = None
    import_receipt_id: str | None = Field(default=None, min_length=1, max_length=128)
    item_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_variant(self):
        has_ref = self.resource_ref is not None
        has_import = self.import_receipt_id is not None or self.item_id is not None
        if has_ref == has_import or (has_import and (self.import_receipt_id is None or self.item_id is None)):
            raise ValueError("attachment source must have exactly one complete variant")
        return self


class AttachmentTargetRequest(_StrictModel):
    kind: Literal["copal_document", "host_document", "treehouse_lesson", "treehouse_submission"]
    resource_ref: str | None = Field(default=None, min_length=8, max_length=16_384)
    course_id: str | None = Field(default=None, min_length=1, max_length=256)
    lesson_id: str | None = Field(default=None, min_length=1, max_length=256)
    assignment_id: str | None = Field(default=None, min_length=1, max_length=256)
    expected_revision: AttachmentRevision | None = None
    root_ref: str | None = Field(default=None, min_length=8, max_length=16_384)
    expected_root_revision: AttachmentRevision | None = None

    @model_validator(mode="after")
    def validate_target(self):
        if (self.root_ref is None) != (self.expected_root_revision is None) or (self.root_ref is not None and (self.kind != 'host_document' or self.expected_revision is None)):
            raise ValueError('selected Host root requires root and document revisions')
        if self.kind in {"copal_document", "host_document"} and (self.resource_ref is None or self.course_id is not None or self.lesson_id is not None):
            raise ValueError(f"{self.kind} attachment target requires a resource ref")
        if self.kind == "treehouse_lesson" and (self.course_id is None or self.lesson_id is None or self.resource_ref is not None):
            raise ValueError("TreeHouse attachment target requires course and lesson IDs")
        if self.kind == "treehouse_submission" and (self.course_id is None or self.assignment_id is None or self.resource_ref is not None or self.lesson_id is not None):
            raise ValueError("Submission attachment target requires course and assignment IDs")
        return self


class TransferRequest(_StrictModel):
    operation_id: str = Field(min_length=1, max_length=128)
    generation: StrictInt = Field(ge=0)
    kind: Literal["move", "copy"]
    sources: list[TransferSource] = Field(min_length=1, max_length=200)
    destination_ref: str = Field(min_length=8, max_length=16_384)
    collision: Literal["fail", "rename"] = "fail"
    managed_root: ManagedTransferRoot | None = None
    destination_root: ManagedTransferRoot | None = None


class TransferRootPreflightRequest(_StrictModel):
    generation: StrictInt = Field(ge=0)
    sources: list[TransferSource] = Field(min_length=1, max_length=200)
    destination_ref: str = Field(min_length=8, max_length=16_384)


class ActionRootPreflightRequest(_StrictModel):
    generation: StrictInt = Field(ge=0)
    resource_ref: str = Field(min_length=8, max_length=16_384)
    name: str = Field(min_length=1, max_length=240)
    expected_revision: RevisionRequest | None = None


class ResourceKeyRequest(_StrictModel):
    provider: Literal["copal", "files"]
    resource_id: str = Field(min_length=1, max_length=256)
    account_id: str | None = Field(default=None, min_length=1, max_length=256)
    workspace_id: str | None = Field(default=None, min_length=1, max_length=64)


class ResolveResourceRequest(_StrictModel):
    resource_key: ResourceKeyRequest


class AttachmentPrepareRequest(_StrictModel):
    operation_id: str = Field(min_length=1, max_length=128)
    generation: StrictInt = Field(ge=0)
    source: AttachmentSourceRequest
    target: AttachmentTargetRequest
    mode: Literal["link", "embed"]


class AttachmentUploadMetadata(_StrictModel):
    operation_id: str = Field(min_length=1, max_length=128)
    generation: StrictInt = Field(ge=0)
    target: AttachmentTargetRequest
    mode: Literal["link", "embed"]
    name: str = Field(min_length=1, max_length=240)
    mime: str = Field(min_length=1, max_length=128)


class AttachmentReceiptResponse(_StrictModel):
    operation_id: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=0)
    state: Literal["pending", "complete", "failed", "stale"]
    preparation: dict[str, Any] | None = None


class BaseQueryRequest(_StrictModel):
    base_ref: str = Field(min_length=8, max_length=16_384)
    expected_revision: RevisionRequest | None = None
    corpus_ref: str = Field(min_length=8, max_length=16_384)
    generation: StrictInt = Field(ge=0)
    view_id: str | None = Field(default=None, max_length=256)
    query: dict[str, Any] = Field(default_factory=dict)
    page: StrictInt = Field(default=0, ge=0)
    page_size: StrictInt = Field(default=100, ge=1, le=500)
    context_ref: str | None = Field(default=None, max_length=16_384)
    draft_definition: str | None = Field(default=None, max_length=256 * 1024)

    @model_validator(mode="after")
    def validate_query_bound(self):
        try:
            encoded = json.dumps(self.query, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError):
            raise ValueError("Base query is invalid") from None
        if len(encoded) > 256 * 1024:
            raise ValueError("Base query is too large")
        return self


_ACTIVE_DOWNLOAD_TYPES = {
    "text/html",
    "application/xhtml+xml",
    "image/svg+xml",
    "application/xml",
    "text/xml",
    "application/pdf",
}
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _download_filename(value: str) -> tuple[str, str]:
    raw = unicodedata.normalize("NFC", Path(str(value or "resource").replace("\\", "/")).name)
    raw = "".join(character for character in raw if ord(character) >= 32 and character not in {'\x7f', '"', ';', '/', '\\'})
    raw = (raw.strip() or "resource")[:240]
    ascii_value = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
    ascii_value = "".join(character for character in ascii_value if character.isalnum() or character in {".", "_", "-", " ", "(", ")"})
    ascii_value = (ascii_value.strip(" .") or "resource")[:180]
    return ascii_value, quote(raw, safe="")


def _range(value: str | None, size: int) -> tuple[int, int] | None:
    if not value:
        return None
    match = _RANGE_RE.fullmatch(value.strip())
    if not match or size <= 0:
        raise FilesFacadeError("requested range is unavailable", code="invalid_range")
    start_raw, end_raw = match.groups()
    if not start_raw and not end_raw:
        raise FilesFacadeError("requested range is unavailable", code="invalid_range")
    if not start_raw:
        suffix = int(end_raw)
        if suffix <= 0:
            raise FilesFacadeError("requested range is unavailable", code="invalid_range")
        start = max(0, size - suffix)
        end = size - 1
    else:
        start = int(start_raw)
        end = int(end_raw) if end_raw else size - 1
        if start >= size or end < start:
            raise FilesFacadeError("requested range is unavailable", code="invalid_range")
        end = min(end, size - 1)
    return start, end


def _open_path_content(content) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(content.path, flags)
        current = os.fstat(descriptor)
    except OSError as exc:
        try:
            os.close(descriptor)
        except (NameError, OSError):
            pass
        raise FilesFacadeError("resource content is unavailable", code="resource_unavailable") from exc
    identity = (int(current.st_dev), int(current.st_ino), int(current.st_size), int(current.st_mtime_ns))
    if not stat.S_ISREG(current.st_mode) or (content.expected_identity and identity != content.expected_identity):
        os.close(descriptor)
        raise FilesFacadeError("resource changed before it could be opened", code="resource_changed")
    return descriptor, current


def setup_files_facade_routes(
    *,
    application_state: Any = None,
    policy_repository: FilePolicyRepository | None = None,
    session_factory: Callable = SessionLocal,
    filesystem_registry: FilesystemRootRegistry | None = None,
    host_client_factory: Callable[..., Any] = client_for_owner,
) -> APIRouter:
    router = APIRouter(prefix="/api/files-v1", tags=["files-v1"])
    repository = policy_repository or FilePolicyRepository()
    host_registry = filesystem_registry or FilesystemRootRegistry(repository=repository)

    def context(request: Request, *, workspace: str = "default") -> ProviderContext:
        generation = repository.generation()
        account_id, username, is_admin = files_principal(request, repository=repository)
        if repository.generation() != generation:
            raise HTTPException(409, detail={"code": "policy_generation_changed", "message": "File access changed. Refresh and try again."})
        return ProviderContext(
            owner_subject_id=str(account_id),
            owner_username=username,
            policy_generation=generation,
            is_admin=is_admin,
            workspace_id=str(workspace or "default"),
        )

    def context_for_ref(request: Request, token: str) -> ProviderContext:
        """Carry a sealed Copal workspace into navigation requests.

        The roots endpoint accepts an explicit Copal workspace hint, while
        subsequent browser navigation submits only the opaque root ref. Use
        the already owner/generation-validated ref to select that same scope
        before the facade enforces its workspace binding.
        """
        current = context(request)
        try:
            ref = resolve_resource_ref(token, expected_owner_subject_id=current.owner_subject_id, current_policy_generation=current.policy_generation)
            workspace = ref.workspace_id or current.workspace_id
        except Exception:
            workspace = current.workspace_id
        return replace(current, workspace_id=str(workspace or "default"))

    def facade_for_state(app_state, *, copal_workspace: str = "default") -> FilesFacade:
        bridge = getattr(app_state, "copal_bridge", None)
        chat_lifecycle = ChatLifecycleService(
            session_factory=session_factory,
            session_manager=getattr(app_state, "session_manager", None),
            mimo_supervisor=getattr(app_state, "mimo_supervisor", None),
        )
        auth = getattr(app_state, 'auth_manager', None)
        barrier = getattr(app_state, 'account_request_barrier', None)

        @asynccontextmanager
        async def host_mutation_scope(original: ProviderContext):
            if not auth or not barrier or auth.account_id(original.owner_username) != original.owner_subject_id:
                raise FilesFacadeError('Account admission is unavailable', code='provider_unavailable')
            async with barrier.admitted(original.owner_username):
                if (auth.account_id(original.owner_username) != original.owner_subject_id
                    or repository.generation() != original.policy_generation):
                    raise FilesFacadeError('Account or Files policy changed', code='resource_ref_stale')
                yield

        host_provider = HostFilesProvider(registry=host_registry, client_factory=host_client_factory, operation_store=repository,
            mutation_scope=host_mutation_scope if auth and barrier else None)
        providers = [
            host_provider,
            FilesImagesProvider(session_factory),
            LibraryFilesProvider(session_factory, chat_lifecycle=chat_lifecycle),
        ]
        if bridge is not None:
            providers.insert(0, CopalFilesProvider(bridge, workspace_id=copal_workspace, operation_store=repository))
        treehouse_repository = getattr(app_state, "treehouse_repository", None)
        if treehouse_repository is None:
            configured = os.environ.get("TREEHOUSE_REPOSITORY_PATH")
            treehouse_repository = TreeHouseRepository(Path(configured) if configured else Path(DATA_DIR) / "treehouse.sqlite3")
            app_state.treehouse_repository = treehouse_repository
        app_state.treehouse_files_facade = facade
        app_state.treehouse_files_context = context
        app_state.treehouse_files_content_response = content_response
        return FilesFacade(
            providers,
            place_repository=repository,
            operation_store=repository,
            attachment_targets={
                "host_document": HostDocumentAttachmentTarget(provider=host_provider, operation_store=repository),
                "treehouse_lesson": TreeHouseLessonAttachmentTarget(treehouse_repository),
                "treehouse_submission": TreeHouseSubmissionAttachmentTarget(treehouse_repository),
            },
        )

    def facade(request: Request, *, copal_workspace: str = "default") -> FilesFacade:
        return facade_for_state(request.app.state, copal_workspace=copal_workspace)

    if application_state is not None:
        async def agent_open_on_host(owner: str, resource_ref: str, app_id: str) -> dict[str, Any]:
            """Desktop-control `open_resource`: the facade's open-on-host authority for a resource the person chose.

            Only an opaque Files resource token is accepted (a path or any other text fails the facade's token check),
            the token must belong to this owner and the current policy generation, and every other check stays the facade's.
            """
            if type(resource_ref) is not str or not resource_ref or type(app_id) is not str or not app_id:
                return {"ok": False, "code": "invalid_resource_request"}
            auth = getattr(application_state, "auth_manager", None)
            try:
                account = auth.account_id(owner) if auth is not None else None
                if not account or auth.username_for_account_id(account) != owner:
                    return {"ok": False, "code": "files_open_unavailable"}
                generation = repository.generation()
                try:
                    workspace = resolve_resource_ref(resource_ref, expected_owner_subject_id=str(account), current_policy_generation=generation).workspace_id
                except Exception:
                    workspace = None  # an invalid token is refused by the facade below, with its own code
                context = ProviderContext(str(account), owner, generation, False, str(workspace or "default"))
                await facade_for_state(application_state).open_on_host(context, resource_ref=resource_ref, app_id=app_id)
            except FilesFacadeError as error:
                return {"ok": False, "code": str(error.code)}
            except Exception:
                return {"ok": False, "code": "files_open_unavailable"}
            return {"ok": True}

        application_state.agent_open_on_host = agent_open_on_host

    def _media_human(request: Request):
        from src.auth_helpers import is_internal_tool_identity
        account, owner, _admin = files_principal(request, repository=repository)
        if (getattr(request.state, 'api_token', False) or getattr(request.state, 'internal_tool_authenticated', False)
            or is_internal_tool_identity(owner)):
            raise HTTPException(403, 'An interactive named user is required')
        return account, owner

    def _analysis_original(request, account, owner, project=None):
        auth=getattr(request.app.state,'auth_manager',None)
        if (not auth or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner
            or (project is not None and (project.identity['ownerSubjectID']!=account or project.context.owner_subject_id!=account))):
            raise ValueError('analysis account changed')

    def _analysis_projects(request: Request, workspace_id: str, *, read_only: bool = False):
        from src.openclank.analysis_projects import AnalysisProjects, resolve_project, read_only_project
        from src.openclank.files_service_client import history_binding_for
        account, owner = _media_human(request)
        state = request.app.state
        auth, barrier = getattr(state, 'auth_manager', None), getattr(state, 'account_request_barrier', None)
        if not auth or not barrier:
            raise HTTPException(503, 'Analysis project authority is unavailable')
        try:
            binding, identity = resolve_project(repository, workspace_id=workspace_id, owner_username=owner,
                account_id=account, auth_manager=auth)
            ctx = context(request, workspace=binding.workspace.id)
            if ctx.owner_subject_id != account:
                raise ValueError('account changed')
            files = facade(request, copal_workspace=binding.workspace.id)
            if read_only:
                return read_only_project(facade=files, context=ctx, repository=repository, auth_manager=auth,
                    owner=owner, account=account, workspace_id=binding.workspace.id)
            host = files._providers.get('host')
            if host is None or (not read_only and host.mutation_scope is None):
                raise ValueError('Host admission unavailable')
        except (ValueError, WorkspacePolicyServiceError):
            raise HTTPException(409, 'Analysis workspace is unavailable')

        async def current(original, expected, original_context):
            try:
                latest, latest_identity = resolve_project(repository, workspace_id=expected['workspaceID'],
                    owner_username=owner, account_id=account, auth_manager=auth)
                if (latest_identity != expected or latest.path != original.path or latest.workspace != original.workspace
                    or repository.generation() != original_context.policy_generation
                    or auth.account_id(owner) != original_context.owner_subject_id
                    or auth.username_for_account_id(original_context.owner_subject_id) != owner):
                    return False
                relative = '/'.join(filter(None, (latest.workspace.relative_folder, '.clanker', 'robonotes', expected['projectKey'], 'analysis')))
                decision = repository.resolve(subject_id=account, is_admin=original_context.is_admin, origin='app',
                    location_id=latest.workspace.location_id, capability='read' if read_only else 'write', workspace_id=latest.workspace.id,
                    relative_resource=relative, observation_only=True)
                if not decision.allowed or decision.policy_generation != original_context.policy_generation:
                    return False
                if any(part.lower() in {'.env', '.secrets', 'auth', 'credentials', 'tokens', 'settings'} for part in Path(latest.path).parts):
                    return False
                if read_only:
                    return True
                scope = host._scope(original_context)
                history = history_binding_for(owner, 'human', scope)
                if not isinstance(history, Mapping) or history.get('account_id') != account:
                    return False
                for root in history.get('root_bindings') or []:
                    if not isinstance(root, Mapping) or str(root.get('root_id')) != latest.workspace.location_id:
                        continue
                    root_path = str(root.get('canonical_path') or '')
                    if root_path and os.path.commonpath((root_path, latest.path)) == root_path:
                        return True
                return False
            except Exception:
                return False
        return AnalysisProjects(facade=files, host=host, context=ctx, binding=binding, identity=identity, current=current, read_only=read_only)

    def _media_service_for_state(state, workspace: str, session_current, before_dispatch=None):
        from src.openclank.media_adapters.comfyui import ComfyUIAdapter
        from src.openclank.media_adapters.comfy_flac import ComfyFlacAdapter
        from src.openclank.media_adapters.comfyui_material import ComfyUIMaterialAdapter
        from src.openclank.media_files import (MediaFilesPublisher, MediaFilesScope, bound_profile_resolver,
            human_session_authority, no_project_capture, no_project_current)
        from src.openclank.media_human import HumanMediaService
        from src.openclank.media_jobs import MediaCoordinator
        auth = getattr(state, 'auth_manager', None)
        barrier = getattr(state, 'account_request_barrier', None)
        store = getattr(state, 'provider_store', None)
        sessions = getattr(state, 'session_manager', None)
        if not all((auth, barrier, store, sessions)): raise HTTPException(503, 'Media jobs unavailable')
        files = facade_for_state(state, copal_workspace=workspace)
        async def context_for(identity):
            owner = auth.username_for_account_id(identity['account_id'])
            if not owner or auth.account_id(owner) != identity['account_id'] or repository.generation() != identity['policy_generation']:
                raise ValueError('media authority changed')
            return ProviderContext(identity['account_id'], owner, identity['policy_generation'], False, identity['workspace_id'])
        resolve = bound_profile_resolver(store=store, auth_manager=auth, account_barrier=barrier)
        scope = MediaFilesScope(facade=files, context_for=context_for, session_current=session_current,
            project_current=no_project_current, capture_project=no_project_capture, resolve_profile=resolve)
        adapter = ComfyUIAdapter(resolve_profile=resolve, capture_recipe=scope.recipe, artifact_store=state.media_artifact_store, before_dispatch=before_dispatch)
        audio = ComfyFlacAdapter(resolve_profile=resolve, capture_recipe=scope.recipe, artifact_store=state.media_artifact_store, before_dispatch=before_dispatch)
        generated = ComfyUIMaterialAdapter(resolve_profile=resolve, capture_recipe=scope.recipe, artifact_store=state.media_artifact_store, before_dispatch=before_dispatch)
        async def fetch_artifact(identity, job, current):
            selected = generated if identity.get('adapter_id') == 'comfyui-material-v1' else audio if identity.get('adapter_id') == 'comfyui-flac-v1' else adapter
            return await selected.fetch_artifact(identity, job, current)
        publisher = MediaFilesPublisher(facade=files, context_for=context_for, fetch_artifact=fetch_artifact)
        coordinator = MediaCoordinator(barrier=barrier, account_for_owner=auth.account_id, owner_for_account=auth.username_for_account_id,
            scope_current=scope.current, control_current=scope.control, adapters={'comfyui':adapter, 'comfyui-flac-v1':audio, 'comfyui-material-v1':generated}, publisher=publisher,
            foreground_runtime=lambda:getattr(getattr(state,'mimo_supervisor',None),'_s05_foreground_runtime',None), foreground_profile=lambda:getattr(state,'s05_profile',None),
            foreground_provider=lambda:getattr(state,'canonical_memory_provider',None) or getattr(state,'memory_provider',None))
        async def profile_selection(owner, account, connection_id):
            row = await asyncio.to_thread(store.get_connection, owner=owner, connection_id=connection_id)
            partial = {'account_id':account, 'resource_binding':{'ownerAccountID':account, 'connectionID':connection_id,
                'connectionRevision':row.revision, 'interface':'comfyui-workflow-v1'}}
            async with resolve(partial) as (profile, assert_current):
                await assert_current()
                return profile.job_binding()
        async def recovery_context_for(identity):
            owner = auth.username_for_account_id(identity['account_id'])
            if not owner or auth.account_id(owner) != identity['account_id']:
                raise ValueError('media account changed')
            return ProviderContext(identity['account_id'], owner, repository.generation(), False, identity['workspace_id'])
        return HumanMediaService(coordinator=coordinator, scope=scope, profile_selection=profile_selection, barrier=barrier,
            recovery_context_for=recovery_context_for, artifact_store=state.media_artifact_store)

    def _media_service(request: Request, workspace: str):
        from src.openclank.media_files import human_session_authority
        state=request.app.state
        current=human_session_authority(session_manager=getattr(state,'session_manager',None), auth_manager=getattr(state,'auth_manager',None))
        return _media_service_for_state(state,workspace,current)

    def _comfy_material_human_service(request, workspace, account, owner, identity, original):
        from src.openclank.media_files import human_session_authority
        state=request.app.state
        session=human_session_authority(session_manager=state.session_manager,auth_manager=state.auth_manager)
        async def current(candidate):
            return bool(_material_original_current(request,original) and candidate.get('account_id')==account
                and candidate.get('session_id')==identity['session_id'] and candidate.get('workspace_id')==workspace
                and repository.generation()==identity['policy_generation'] and await session(candidate)
                and _material_original_current(request,original))
        async def before_dispatch():
            if not await current(identity): raise ValueError('Material authority changed')
            return True
        return _media_service_for_state(state,workspace,current,before_dispatch),before_dispatch

    def _local_shape_service_for_state(state, workspace: str, session_current, before_dispatch, *, hunyuan=False):
        from src import bg_jobs
        if hunyuan:
            from src.openclank.hunyuan_runtime import admitted_hunyuan_profile as admitted_profile, INTERFACE
            from src.openclank.hunyuan_files import HunyuanFilesScope as ShapeFilesScope
            from src.openclank.hunyuan_human import HunyuanHumanService as ShapeHumanService
            from src.openclank.hunyuan_launcher import launch_fixed
            from src.openclank.media_adapters.hunyuan_local import HunyuanShapeAdapter as ShapeAdapter
        else:
            from src.openclank.trellis_runtime import admitted_trellis_profile as admitted_profile, INTERFACE
            from src.openclank.trellis_files import TrellisFilesScope as ShapeFilesScope
            from src.openclank.trellis_human import TrellisHumanService as ShapeHumanService
            from src.openclank.trellis_launcher import launch_fixed
            from src.openclank.media_adapters.trellis import TrellisAdapter as ShapeAdapter
        from src.openclank.media_files import MediaFilesPublisher
        from src.openclank.media_jobs import MediaCoordinator
        auth,barrier,store,sessions,artifacts=(getattr(state,k,None) for k in ('auth_manager','account_request_barrier','provider_store','session_manager','media_artifact_store'))
        if not all((auth,barrier,store,sessions,artifacts)) or not callable(session_current) or not callable(before_dispatch):
            raise HTTPException(503,'TRELLIS jobs unavailable')
        files=facade_for_state(state,copal_workspace=workspace)
        async def context_for(identity):
            owner=auth.username_for_account_id(identity['account_id'])
            if not owner or auth.account_id(owner)!=identity['account_id'] or repository.generation()!=identity['policy_generation']:
                raise ValueError('TRELLIS authority changed')
            return ProviderContext(identity['account_id'],owner,identity['policy_generation'],False,identity['workspace_id'])
        async def recovery_context_for(identity):
            owner=auth.username_for_account_id(identity['account_id'])
            if not owner or auth.account_id(owner)!=identity['account_id']: raise ValueError('TRELLIS account changed')
            return ProviderContext(identity['account_id'],owner,repository.generation(),False,identity['workspace_id'])
        def resolve(identity):
            binding=identity['resource_binding'];owner=auth.username_for_account_id(identity['account_id'])
            return admitted_profile(store=store,auth_manager=auth,account_barrier=barrier,artifact_store=artifacts,
                owner=owner,ownerAccountID=identity['account_id'],connectionID=binding['connectionID'],
                connectionRevision=binding['connectionRevision'],interface=binding['interface'])
        scope=ShapeFilesScope(facade=files,context_for=context_for,session_current=session_current,resolve_profile=resolve)
        async def profile_for(identity):
            async with resolve(identity) as (profile,check):
                await check();return profile.child_profile()
        async def launcher(child,source,dispatch_admission):
            job=await asyncio.to_thread(bg_jobs.get_media,child['job_id'],account_id=child['account_id'])
            if not job or job.get('local_child')!=child: raise ValueError('TRELLIS child changed')
            identity=job['identity'];owner=auth.username_for_account_id(child['account_id'])
            async with resolve(identity) as (profile,check):
                await check();binding=identity['resource_binding']
                expected={**profile.child_profile(),'input_sha256':hashlib.sha256(source).hexdigest(),'seed':binding['seed']}
                if expected!=child['profile']: raise ValueError('TRELLIS profile changed')
                def admission():
                    dispatch_admission()
                    if auth.account_id(owner)!=child['account_id'] or auth.username_for_account_id(child['account_id'])!=owner:
                        raise ValueError('TRELLIS account changed')
                async def final_dispatch():
                    await check()
                    if await before_dispatch() is False: raise ValueError('TRELLIS authority changed')
                    await check()
                await launch_fixed(child,source,profile=profile,artifact_store=artifacts,admission=admission,barrier=barrier,owner=owner,before_dispatch=final_dispatch)
        adapter=ShapeAdapter(profile_for=profile_for,capture_source=scope.source,launch_fixed=launcher,artifact_store=artifacts)
        publisher=MediaFilesPublisher(facade=files,context_for=context_for,fetch_artifact=adapter.fetch_artifact)
        coordinator=MediaCoordinator(barrier=barrier,account_for_owner=auth.account_id,owner_for_account=auth.username_for_account_id,
            scope_current=scope.current,control_current=scope.control,adapters={adapter.adapter_id:adapter},publisher=publisher,
            foreground_runtime=lambda:getattr(getattr(state,'mimo_supervisor',None),'_s05_foreground_runtime',None),
            foreground_profile=lambda:getattr(state,'s05_profile',None),
            foreground_provider=lambda:getattr(state,'canonical_memory_provider',None) or getattr(state,'memory_provider',None))
        async def selection(owner,account,connection_id):
            row=await asyncio.to_thread(store.get_connection,owner=owner,connection_id=connection_id)
            identity={'account_id':account,'resource_binding':{'connectionID':connection_id,'connectionRevision':row.revision,'interface':INTERFACE}}
            async with resolve(identity) as (profile,check):
                await check();return profile.job_binding()
        return ShapeHumanService(coordinator=coordinator,scope=scope,profile_selection=selection,barrier=barrier,
            recovery_context_for=recovery_context_for,artifact_store=artifacts)

    def _trellis_service_for_state(state, workspace: str, session_current, before_dispatch):
        return _local_shape_service_for_state(state,workspace,session_current,before_dispatch)

    def _hunyuan_service_for_state(state, workspace: str, session_current, before_dispatch):
        return _local_shape_service_for_state(state,workspace,session_current,before_dispatch,hunyuan=True)

    def _trellis_human_service(request,workspace,account,owner,identity,original):
        from src.openclank.media_files import human_session_authority
        state=request.app.state;session=human_session_authority(session_manager=state.session_manager,auth_manager=state.auth_manager)
        async def current(candidate):
            return bool(_material_original_current(request,original) and candidate.get('account_id')==account
                and candidate.get('session_id')==identity['session_id'] and candidate.get('workspace_id')==workspace
                and repository.generation()==identity['policy_generation'] and await session(candidate) and _material_original_current(request,original))
        async def before_dispatch():
            if not await current(identity): raise ValueError('TRELLIS authority changed')
        return _trellis_service_for_state(state,workspace,current,before_dispatch),before_dispatch

    def _hunyuan_human_service(request,workspace,account,owner,identity,original):
        from src.openclank.media_files import human_session_authority
        state=request.app.state;session=human_session_authority(session_manager=state.session_manager,auth_manager=state.auth_manager)
        async def current(candidate):
            return bool(_material_original_current(request,original) and candidate.get('account_id')==account
                and candidate.get('session_id')==identity['session_id'] and candidate.get('workspace_id')==workspace
                and repository.generation()==identity['policy_generation'] and await session(candidate) and _material_original_current(request,original))
        async def before_dispatch():
            if not await current(identity): raise ValueError('Hunyuan authority changed')
        return _hunyuan_service_for_state(state,workspace,current,before_dispatch),before_dispatch

    def _material_service_for_state(state, workspace: str, session_current, before_dispatch):
        from src.openclank.material_pack_human import MaterialPackHumanService
        from src.openclank.media_adapters.material_pack import MaterialPackAdapter
        from src.openclank.media_files import MaterialPackFilesScope, MediaFilesPublisher
        from src.openclank.media_jobs import MediaCoordinator
        auth,barrier,sessions,artifacts=(getattr(state,key,None) for key in ('auth_manager','account_request_barrier','session_manager','media_artifact_store'))
        if not all((auth,barrier,sessions,artifacts)) or not callable(session_current) or not callable(before_dispatch): raise HTTPException(503,'Material bundle unavailable')
        files=facade_for_state(state,copal_workspace=workspace)
        async def context_for(identity):
            owner=auth.username_for_account_id(identity['account_id'])
            if not owner or auth.account_id(owner)!=identity['account_id'] or repository.generation()!=identity['policy_generation']: raise ValueError('material authority changed')
            return ProviderContext(identity['account_id'],owner,identity['policy_generation'],False,identity['workspace_id'])
        async def recovery_context_for(identity):
            owner=auth.username_for_account_id(identity['account_id'])
            if not owner or auth.account_id(owner)!=identity['account_id']: raise ValueError('material account changed')
            return ProviderContext(identity['account_id'],owner,repository.generation(),False,identity['workspace_id'])
        scope=MaterialPackFilesScope(facade=files,context_for=context_for,session_current=session_current)
        adapter=MaterialPackAdapter(scope=scope,barrier=barrier,owner_for_account=auth.username_for_account_id,artifact_store=artifacts,before_dispatch=before_dispatch)
        publisher=MediaFilesPublisher(facade=files,context_for=context_for,fetch_artifact=adapter.fetch_artifact)
        coordinator=MediaCoordinator(barrier=barrier,account_for_owner=auth.account_id,owner_for_account=auth.username_for_account_id,
            scope_current=scope.current,control_current=scope.control,adapters={adapter.adapter_id:adapter},publisher=publisher,
            foreground_runtime=lambda:getattr(getattr(state,'mimo_supervisor',None),'_s05_foreground_runtime',None),
            foreground_profile=lambda:getattr(state,'s05_profile',None),
            foreground_provider=lambda:getattr(state,'canonical_memory_provider',None) or getattr(state,'memory_provider',None))
        return MaterialPackHumanService(coordinator=coordinator,scope=scope,profile_selection=None,barrier=barrier,recovery_context_for=recovery_context_for,artifact_store=artifacts)

    def _material_human_service(request, workspace, account, owner, identity, factory, credential):
        from src.openclank.media_files import human_session_authority
        state=request.app.state; auth=state.auth_manager; barrier=state.account_request_barrier
        session=human_session_authority(session_manager=state.session_manager,auth_manager=auth)
        def cheap():
            return (getattr(state,'capture_original_credential',None) is factory and credential() is True
                and _media_human(request)==(account,owner) and state.auth_manager is auth and state.account_request_barrier is barrier
                and repository.generation()==identity['policy_generation'])
        async def session_current(candidate):
            try:
                return bool(cheap() and candidate.get('account_id')==account and candidate.get('session_id')==identity['session_id']
                    and candidate.get('workspace_id')==workspace and await session(candidate) and cheap())
            except Exception:return False
        async def before_dispatch():
            if not await session_current(identity): raise ValueError('material authority changed')
        return _material_service_for_state(state,workspace,session_current,before_dispatch),before_dispatch

    def _material_original(request):
        account,owner=_media_human(request);factory=getattr(request.app.state,'capture_original_credential',None)
        if not callable(factory):raise HTTPException(503,'Material authority unavailable')
        credential=factory(request,owner,account)
        if not callable(credential) or credential() is not True:raise HTTPException(403,'Material authority unavailable')
        return account,owner,factory,credential

    def _material_original_current(request, original):
        try:
            account,owner,factory,credential=original
            return bool(getattr(request.app.state,'capture_original_credential',None) is factory
                and credential() is True and _media_human(request)==(account,owner))
        except Exception:
            return False

    async def _material_body(request):
        import json
        raw=bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw)>65536:raise HTTPException(413,'Material request too large')
        def unique(pairs):
            out={}
            for key,value in pairs:
                if key in out:raise ValueError('duplicate key')
                out[key]=value
            return out
        try:return json.loads(raw,object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('invalid value')))
        except Exception as exc:raise HTTPException(422,'Invalid material request') from exc

    def _sprite_sheet_service_for_state(state, workspace: str, session_current, before_dispatch=None, project_authority=None):
        from src.openclank.sprite_sheet_human import SpriteSheetHumanService, bind_files_project
        from src.openclank.media_adapters.sprite_sheet import SpriteSheetAdapter,SpriteSheetCropsAdapter
        from src.openclank.media_files import SpriteSheetFilesScope, MediaFilesPublisher, human_session_authority
        from src.openclank.media_jobs import MediaCoordinator
        from src.generated_images import resolve_gallery_image_path
        from core.database import SessionLocal
        auth,barrier,sessions,artifacts=(getattr(state,key,None) for key in ('auth_manager','account_request_barrier','session_manager','media_artifact_store'))
        if not all((auth,barrier,sessions,artifacts)): raise HTTPException(503,'Sprite sheets unavailable')
        files=facade_for_state(state,copal_workspace=workspace)
        async def context_for(identity):
            owner=auth.username_for_account_id(identity['account_id'])
            if not owner or auth.account_id(owner)!=identity['account_id'] or repository.generation()!=identity['policy_generation']: raise ValueError('sprite authority changed')
            return ProviderContext(identity['account_id'],owner,identity['policy_generation'],False,identity['workspace_id'])
        async def recovery_context_for(identity):
            owner=auth.username_for_account_id(identity['account_id'])
            if not owner or auth.account_id(owner)!=identity['account_id']: raise ValueError('sprite account changed')
            return ProviderContext(identity['account_id'],owner,repository.generation(),False,identity['workspace_id'])
        scope=SpriteSheetFilesScope(facade=files,context_for=context_for,session_current=session_current)
        adapter=SpriteSheetAdapter(scope=scope,barrier=barrier,owner_for_account=auth.username_for_account_id,artifact_store=artifacts,before_dispatch=before_dispatch)
        crops=SpriteSheetCropsAdapter(scope=scope,barrier=barrier,owner_for_account=auth.username_for_account_id,artifact_store=artifacts,before_dispatch=before_dispatch)
        publisher=MediaFilesPublisher(facade=files,context_for=context_for,fetch_artifact=adapter.fetch_artifact)
        coordinator=MediaCoordinator(barrier=barrier,account_for_owner=auth.account_id,owner_for_account=auth.username_for_account_id,
            scope_current=scope.current,control_current=scope.control,adapters={adapter.adapter_id:adapter,crops.adapter_id:crops},publisher=publisher)
        app_state=state
        def project_bind(*,owner,resource_id,sha256,state,width,height,operation_key):
            from routes.image_project_routes import image_project_repository
            if project_authority is None: raise ValueError('project authority unavailable')
            original_owner, account, current = project_authority
            if owner != original_owner or not current(): raise ValueError('project authority changed')
            return bind_files_project(session_factory=SessionLocal,resolve_path=lambda locator:resolve_gallery_image_path(locator,require_exists=True),
                repository_factory=lambda:image_project_repository(app_state,owner=owner,account=account,current=current),
                owner=owner,resource_id=resource_id,sha256=sha256,state=state,width=width,height=height,operation_key=operation_key)
        return SpriteSheetHumanService(coordinator=coordinator,scope=scope,profile_selection=None,barrier=barrier,recovery_context_for=recovery_context_for,artifact_store=artifacts,project_bind=project_bind)

    def _sprite_sheet_service(request: Request, workspace: str, project_authority=None):
        from src.openclank.media_files import human_session_authority
        state=request.app.state
        current=human_session_authority(session_manager=getattr(state,'session_manager',None),auth_manager=getattr(state,'auth_manager',None))
        return _sprite_sheet_service_for_state(state,workspace,current,project_authority=project_authority)

    def _latex_service(request: Request, workspace: str):
        from src.openclank.latex_files import LatexFilesScope
        from src.openclank.latex_human import LatexHumanService
        from src.openclank.media_adapters.latex import LatexAdapter
        from src.openclank.media_files import MediaFilesPublisher, human_session_authority
        from src.openclank.media_jobs import MediaCoordinator
        state=request.app.state
        auth,barrier,sessions,artifacts=(getattr(state,key,None) for key in ('auth_manager','account_request_barrier','session_manager','media_artifact_store'))
        if not all((auth,barrier,sessions,artifacts)): raise HTTPException(503,'LaTeX jobs unavailable')
        files=facade(request,copal_workspace=workspace)
        async def context_for(identity):
            owner=auth.username_for_account_id(identity['account_id'])
            if not owner or auth.account_id(owner)!=identity['account_id'] or repository.generation()!=identity['policy_generation']: raise ValueError('LaTeX authority changed')
            return ProviderContext(identity['account_id'],owner,identity['policy_generation'],False,identity['workspace_id'])
        async def recovery_context_for(identity):
            owner=auth.username_for_account_id(identity['account_id'])
            if not owner or auth.account_id(owner)!=identity['account_id']: raise ValueError('LaTeX account changed')
            return ProviderContext(identity['account_id'],owner,repository.generation(),False,identity['workspace_id'])
        scope=LatexFilesScope(facade=files,context_for=context_for,repository=repository,
            session_current=human_session_authority(session_manager=sessions,auth_manager=auth))
        adapter=LatexAdapter(scope=scope,artifact_store=artifacts,barrier=barrier,owner_for_account=auth.username_for_account_id)
        publisher=MediaFilesPublisher(facade=files,context_for=context_for,fetch_artifact=adapter.fetch_artifact)
        coordinator=MediaCoordinator(barrier=barrier,account_for_owner=auth.account_id,owner_for_account=auth.username_for_account_id,
            scope_current=scope.current,control_current=scope.control,adapters={adapter.adapter_id:adapter},publisher=publisher)
        return LatexHumanService(coordinator=coordinator,scope=scope,profile_selection=None,barrier=barrier,recovery_context_for=recovery_context_for,artifact_store=artifacts)

    def _recording_service(request: Request, workspace: str, *, before_spawn=None):
        from src.openclank.recording_human import RecordingFilesScope, RecordingHumanService
        from src.openclank.media_adapters.recording import RecordingAdapter
        from src.openclank.media_files import MediaFilesPublisher, human_session_authority
        from src.openclank.media_jobs import MediaCoordinator
        state=request.app.state
        auth,barrier,sessions,artifacts=(getattr(state,key,None) for key in ('auth_manager','account_request_barrier','session_manager','media_artifact_store'))
        if not all((auth,barrier,sessions,artifacts)): raise HTTPException(503,'Recording jobs unavailable')
        files=facade(request,copal_workspace=workspace)
        async def context_for(identity):
            owner=auth.username_for_account_id(identity['account_id'])
            if not owner or auth.account_id(owner)!=identity['account_id'] or repository.generation()!=identity['policy_generation']: raise ValueError('Recording authority changed')
            return ProviderContext(identity['account_id'],owner,identity['policy_generation'],False,identity['workspace_id'])
        async def recovery_context_for(identity):
            owner=auth.username_for_account_id(identity['account_id'])
            if not owner or auth.account_id(owner)!=identity['account_id']: raise ValueError('Recording account changed')
            return ProviderContext(identity['account_id'],owner,repository.generation(),False,identity['workspace_id'])
        scope=RecordingFilesScope(facade=files,context_for=context_for,session_current=human_session_authority(session_manager=sessions,auth_manager=auth))
        adapter=RecordingAdapter(scope=scope,artifact_store=artifacts,barrier=barrier,owner_for_account=auth.username_for_account_id,before_spawn=before_spawn)
        publisher=MediaFilesPublisher(facade=files,context_for=context_for,fetch_artifact=adapter.fetch_artifact)
        coordinator=MediaCoordinator(barrier=barrier,account_for_owner=auth.account_id,owner_for_account=auth.username_for_account_id,
            scope_current=scope.current,control_current=scope.control,adapters={adapter.adapter_id:adapter},publisher=publisher)
        return RecordingHumanService(coordinator=coordinator,scope=scope,profile_selection=None,barrier=barrier,recovery_context_for=recovery_context_for,artifact_store=artifacts)

    def _trim_service(request: Request, workspace: str):
        from src.openclank.trim_files import TrimFilesScope
        from src.openclank.trim_human import TrimHumanService
        from src.openclank.trim_contract import source_evidence
        from src.openclank.media_adapters.trim import TrimAdapter
        from src.openclank.media_files import MediaFilesPublisher, human_session_authority
        from src.openclank.media_jobs import MediaCoordinator
        state=request.app.state;account,owner=_media_human(request)
        auth,barrier,sessions,artifacts=(getattr(state,key,None) for key in ('auth_manager','account_request_barrier','session_manager','media_artifact_store'))
        token=request.cookies.get('open_clank_session')
        if not all((auth,barrier,sessions,artifacts,token)) or auth.get_username_for_token(token)!=owner: raise HTTPException(503,'Trim unavailable')
        files=facade(request,copal_workspace=workspace)
        original_session=human_session_authority(session_manager=sessions,auth_manager=auth)
        async def session_current(identity):
            try:
                key=str(owner).strip().lower()
                return bool(_media_human(request)==(account,owner) and state.auth_manager is auth and state.account_request_barrier is barrier
                    and request.cookies.get('open_clank_session')==token and auth.get_username_for_token(token)==owner
                    and auth.account_id(owner)==account and auth.username_for_account_id(account)==owner
                    and not barrier._fenced(key) and not (barrier._absolute_fence and barrier._absolute_fence(key))
                    and identity.get('account_id')==account and await original_session(identity))
            except Exception:return False
        async def context_for(identity):
            if not await session_current(identity) or repository.generation()!=identity['policy_generation']: raise ValueError('Trim authority changed')
            return ProviderContext(account,owner,identity['policy_generation'],False,identity['workspace_id'])
        async def recovery_context_for(identity):
            if not await session_current(identity): raise ValueError('Trim account changed')
            return ProviderContext(account,owner,repository.generation(),False,identity['workspace_id'])
        scope=TrimFilesScope(facade=files,context_for=context_for,session_current=session_current)
        adapter=TrimAdapter(scope=scope,artifact_store=artifacts,barrier=barrier,owner_for_account=auth.username_for_account_id)
        publisher=MediaFilesPublisher(facade=files,context_for=context_for,fetch_artifact=adapter.fetch_artifact,source_evidence_for=source_evidence)
        coordinator=MediaCoordinator(barrier=barrier,account_for_owner=auth.account_id,owner_for_account=auth.username_for_account_id,
            scope_current=scope.current,control_current=scope.control,adapters={adapter.adapter_id:adapter},publisher=publisher)
        return TrimHumanService(coordinator=coordinator,scope=scope,profile_selection=None,barrier=barrier,recovery_context_for=recovery_context_for,artifact_store=artifacts)

    def _blender_service_for_state(state, workspace: str, session_current, before_dispatch, *, four_headings=False, rig_clip=False, material_bake=False):
        from src import bg_jobs
        from src.openclank.blender_provider import admitted_blender_profile, qualify_blender_profile, INTERFACE
        from src.openclank.blender_launcher import launch_fixed
        from src.openclank.blender_human import BlenderHumanService
        from src.openclank.media_adapters.blender import BlenderAdapter, bind_child_profile
        from src.openclank.media_adapters.blender_four import BlenderFourAdapter
        from src.openclank.media_adapters.blender_rig import BlenderRigAdapter
        from src.openclank.media_adapters.blender_material import BlenderMaterialAdapter
        from src.openclank.media_files import BlenderFilesScope, MediaFilesPublisher, human_session_authority
        from src.openclank.media_jobs import MediaCoordinator
        auth, barrier, store, sessions, artifacts = (getattr(state, key, None) for key in
            ('auth_manager','account_request_barrier','provider_store','session_manager','media_artifact_store'))
        if not all((auth, barrier, store, sessions, artifacts)):
            raise HTTPException(503, 'Blender jobs unavailable')
        files = facade_for_state(state, copal_workspace=workspace)
        async def context_for(identity):
            owner = auth.username_for_account_id(identity['account_id'])
            if not owner or auth.account_id(owner) != identity['account_id'] or repository.generation() != identity['policy_generation']:
                raise ValueError('media authority changed')
            return ProviderContext(identity['account_id'], owner, identity['policy_generation'], False, identity['workspace_id'])
        def resolve(identity):
            binding = identity['resource_binding']
            owner = auth.username_for_account_id(identity['account_id'])
            return admitted_blender_profile(store=store, auth_manager=auth, account_barrier=barrier, artifact_store=artifacts,
                owner=owner, ownerAccountID=identity['account_id'], connectionID=binding['connectionID'],
                connectionRevision=binding['connectionRevision'], interface=binding['interface'])
        scope = BlenderFilesScope(facade=files, context_for=context_for,
            session_current=session_current, resolve_profile=resolve)
        async def profile_for(identity):
            async with resolve(identity) as (profile, check):
                await asyncio.to_thread(qualify_blender_profile, profile)
                await check()
                return profile.material_bake_child_profile() if material_bake else (profile.rig_child_profile() if rig_clip else (profile.four_heading_child_profile() if four_headings else profile.child_profile()))
        async def launcher(child, source, dispatch_admission):
            job = await asyncio.to_thread(bg_jobs.get_media, child['job_id'], account_id=child['account_id'])
            if not job or job.get('local_child') != child: raise ValueError('local child changed')
            identity = job['identity']
            owner = auth.username_for_account_id(child['account_id'])
            async with resolve(identity) as (profile, check):
                await check()
                configured = profile.material_bake_child_profile() if material_bake else (profile.rig_child_profile() if rig_clip else (profile.four_heading_child_profile() if four_headings else profile.child_profile()))
                if material_bake:
                    expected = {**configured, 'input_sha256':hashlib.sha256(source).hexdigest(), 'resolution':identity['resource_binding']['resolution'], 'recipe':'blender-material-channels-v1'}
                else:
                    expected = bind_child_profile(configured, hashlib.sha256(source).hexdigest(), adapter=configured['adapter'])
                if rig_clip: expected['rig']=identity['resource_binding']['rig']
                if expected != child['profile']:
                    raise ValueError('local profile changed')
                def admission():
                    dispatch_admission()
                    if auth.account_id(owner) != child['account_id'] or auth.username_for_account_id(child['account_id']) != owner:
                        raise ValueError('local account changed')
                await launch_fixed(child, source, profile=profile, artifact_store=artifacts, admission=admission, barrier=barrier, owner=owner, **({'before_dispatch':before_dispatch} if before_dispatch is not None else {}))
        adapter = (BlenderMaterialAdapter if material_bake else (BlenderRigAdapter if rig_clip else (BlenderFourAdapter if four_headings else BlenderAdapter)))(profile_for=profile_for, capture_source=scope.source, launch_fixed=launcher, artifact_store=artifacts)
        publisher = MediaFilesPublisher(facade=files, context_for=context_for, fetch_artifact=adapter.fetch_artifact)
        coordinator = MediaCoordinator(barrier=barrier, account_for_owner=auth.account_id, owner_for_account=auth.username_for_account_id,
            scope_current=scope.current, control_current=scope.control, adapters={adapter.adapter_id:adapter}, publisher=publisher,
            foreground_runtime=lambda:getattr(getattr(state,'mimo_supervisor',None),'_s05_foreground_runtime',None), foreground_profile=lambda:getattr(state,'s05_profile',None),
            foreground_provider=lambda:getattr(state,'canonical_memory_provider',None) or getattr(state,'memory_provider',None))
        async def selection(owner, account, connection_id):
            row = await asyncio.to_thread(store.get_connection, owner=owner, connection_id=connection_id)
            identity = {'account_id':account,'resource_binding':{'connectionID':connection_id,'connectionRevision':row.revision,'interface':INTERFACE}}
            async with resolve(identity) as (profile, check):
                if material_bake: await asyncio.to_thread(profile.material_bake_child_profile)
                elif four_headings: await asyncio.to_thread(profile.four_heading_child_profile)
                elif rig_clip: await asyncio.to_thread(profile.rig_child_profile)
                await check()
                return profile.job_binding()
        async def recovery_context_for(identity):
            owner = auth.username_for_account_id(identity['account_id'])
            if not owner or auth.account_id(owner) != identity['account_id']:
                raise ValueError('media account changed')
            return ProviderContext(identity['account_id'], owner, repository.generation(), False, identity['workspace_id'])
        return BlenderHumanService(coordinator=coordinator, scope=scope, profile_selection=selection, barrier=barrier,
                                   recovery_context_for=recovery_context_for, artifact_store=artifacts)

    def _blender_service(request: Request, workspace: str, *, four_headings=False, rig_clip=False, material_bake=False, job_identity=None, original=None):
        from src.openclank.media_files import human_session_authority
        state=request.app.state
        current=human_session_authority(session_manager=state.session_manager, auth_manager=state.auth_manager)
        if material_bake:
            account,owner,factory,credential=original if original is not None else _material_original(request)
            session_current=current
            async def current(identity):
                return bool(getattr(state,'capture_original_credential',None) is factory and credential() is True
                    and _media_human(request)==(account,owner) and identity.get('account_id')==account
                    and identity.get('workspace_id')==workspace and await session_current(identity) and credential() is True)
            async def before_dispatch():
                if not job_identity or not await current(job_identity):
                    raise ValueError('material authority changed')
            return _blender_service_for_state(state,workspace,current,before_dispatch,material_bake=True)
        return _blender_service_for_state(state,workspace,current,None,four_headings=four_headings,rig_clip=rig_clip)

    def _sprixen_service(request: Request, workspace: str):
        from src.openclank.sprixen_provider import admitted_sprixen_profile, _current, INTERFACE
        from src.openclank.sprixen_human import SprixenHumanService
        from src.openclank.media_adapters.sprixen import SprixenAdapter
        from src.openclank.media_files import SprixenFilesScope, MediaFilesPublisher, human_session_authority
        from src.openclank.media_jobs import MediaCoordinator
        state=request.app.state
        auth, barrier, store, sessions, artifacts=(getattr(state,key,None) for key in
            ('auth_manager','account_request_barrier','provider_store','session_manager','media_artifact_store'))
        if not all((auth,barrier,store,sessions,artifacts)): raise HTTPException(503,'Sprixen jobs unavailable')
        files=facade(request,copal_workspace=workspace)
        async def context_for(identity):
            owner=auth.username_for_account_id(identity['account_id'])
            if not owner or auth.account_id(owner)!=identity['account_id'] or repository.generation()!=identity['policy_generation']:
                raise ValueError('media authority changed')
            return ProviderContext(identity['account_id'],owner,identity['policy_generation'],False,identity['workspace_id'])
        def resolve(identity, job=None):
            owner=auth.username_for_account_id(identity['account_id'])
            return admitted_sprixen_profile(store=store,auth_manager=auth,account_barrier=barrier,owner=owner,identity=identity,job=job)
        scope=SprixenFilesScope(facade=files,context_for=context_for,
            session_current=human_session_authority(session_manager=sessions,auth_manager=auth),resolve_profile=resolve)
        adapter=SprixenAdapter(resolve_profile=resolve,artifact_store=artifacts)
        publisher=MediaFilesPublisher(facade=files,context_for=context_for,fetch_artifact=adapter.fetch_artifact)
        coordinator=MediaCoordinator(barrier=barrier,account_for_owner=auth.account_id,owner_for_account=auth.username_for_account_id,
            scope_current=scope.current,control_current=scope.control,adapters={adapter.adapter_id:adapter},publisher=publisher,
            foreground_runtime=lambda:getattr(getattr(state,'mimo_supervisor',None),'_s05_foreground_runtime',None),foreground_profile=lambda:getattr(state,'s05_profile',None),
            foreground_provider=lambda:getattr(state,'canonical_memory_provider',None) or getattr(state,'memory_provider',None))
        async def selection(owner,account,connection_id,provider_account_id):
            connection=await asyncio.to_thread(store.get_connection,owner=owner,connection_id=connection_id)
            selected=await asyncio.to_thread(store.get_account,owner=owner,account_id=provider_account_id)
            profile=await asyncio.to_thread(_current,store=store,auth=auth,owner=owner,host_account=account,
                connection_id=connection_id,connection_revision=connection.revision,provider_account_id=provider_account_id,
                provider_account_revision=selected.revision,credential_version=selected.credential_version)
            return profile.job_binding()
        async def recovery_context_for(identity):
            owner=auth.username_for_account_id(identity['account_id'])
            if not owner or auth.account_id(owner)!=identity['account_id']: raise ValueError('media account changed')
            return ProviderContext(identity['account_id'],owner,repository.generation(),False,identity['workspace_id'])
        return SprixenHumanService(coordinator=coordinator,scope=scope,profile_selection=selection,barrier=barrier,
            recovery_context_for=recovery_context_for,artifact_store=artifacts)

    def _sprixen_animation_service(request: Request, workspace: str):
        # Legacy jobs keep their status/collection owner, but no project-bound
        # animation may be submitted (including a previously prepared job).
        from src.openclank.media_adapters.base import NotDispatched
        from src.openclank.media_adapters.sprixen_animation import SprixenAnimationAdapter
        class ReadOnlyAnimationAdapter(SprixenAnimationAdapter):
            async def submit(self, request, job, admission):
                raise NotDispatched('Project animation submission is unavailable')
        base = _sprixen_service(request, workspace)
        adapter = ReadOnlyAnimationAdapter(resolve_profile=base.scope.resolve_profile, artifact_store=request.app.state.media_artifact_store)
        base.coordinator.adapters = {adapter.adapter_id:adapter}
        base.coordinator.publisher.fetch_artifact = adapter.fetch_artifact
        return base

    def _meshy_service(request: Request, workspace: str):
        from src.openclank.meshy_provider import admitted_meshy_profile, read_original_preview, _current
        from src.openclank.meshy_human import MeshyHumanService
        from src.openclank.media_adapters.meshy import MeshyAdapter, MeshyRefineAdapter
        from src.openclank.media_files import MeshyFilesScope, MediaFilesPublisher, human_session_authority
        from src.openclank.media_jobs import MediaCoordinator
        state=request.app.state
        auth,barrier,store,sessions,artifacts=(getattr(state,key,None) for key in
            ('auth_manager','account_request_barrier','provider_store','session_manager','media_artifact_store'))
        if not all((auth,barrier,store,sessions,artifacts)) or not callable(getattr(store,'meshy_job_credential',None)):
            raise HTTPException(503,'Meshy jobs unavailable')
        files=facade(request,copal_workspace=workspace)
        async def context_for(identity):
            owner=auth.username_for_account_id(identity['account_id'])
            if not owner or auth.account_id(owner)!=identity['account_id'] or repository.generation()!=identity['policy_generation']:
                raise ValueError('media authority changed')
            return ProviderContext(identity['account_id'],owner,identity['policy_generation'],False,identity['workspace_id'])
        def resolve(identity,job=None):
            owner=auth.username_for_account_id(identity['account_id'])
            return admitted_meshy_profile(store=store,auth_manager=auth,account_barrier=barrier,owner=owner,identity=identity,job=job)
        scope=MeshyFilesScope(facade=files,context_for=context_for,
            session_current=human_session_authority(session_manager=sessions,auth_manager=auth),resolve_profile=resolve)
        adapter=MeshyAdapter(resolve_profile=resolve,artifact_store=artifacts)
        async def source_current(identity): return await service.refine_source_current(identity)
        refine=MeshyRefineAdapter(resolve_profile=resolve,artifact_store=artifacts,source_current=source_current)
        publisher=MediaFilesPublisher(facade=files,context_for=context_for,fetch_artifact=adapter.fetch_artifact)
        coordinator=MediaCoordinator(barrier=barrier,account_for_owner=auth.account_id,owner_for_account=auth.username_for_account_id,
            scope_current=scope.current,control_current=scope.control,adapters={adapter.adapter_id:adapter,refine.adapter_id:refine},publisher=publisher,
            foreground_runtime=lambda:getattr(getattr(state,'mimo_supervisor',None),'_s05_foreground_runtime',None),
            foreground_profile=lambda:getattr(state,'s05_profile',None),
            foreground_provider=lambda:getattr(state,'canonical_memory_provider',None) or getattr(state,'memory_provider',None))
        async def selection(owner,account,connection_id,provider_account_id):
            connection=await asyncio.to_thread(store.get_connection,owner=owner,connection_id=connection_id)
            selected=await asyncio.to_thread(store.get_account,owner=owner,account_id=provider_account_id)
            profile=await asyncio.to_thread(_current,store=store,auth=auth,owner=owner,host_account=account,
                connection_id=connection_id,connection_revision=connection.revision,provider_account_id=provider_account_id,
                provider_account_revision=selected.revision,credential_version=selected.credential_version)
            return profile.job_binding()
        async def recovery_context_for(identity):
            owner=auth.username_for_account_id(identity['account_id'])
            if not owner or auth.account_id(owner)!=identity['account_id']: raise ValueError('media account changed')
            return ProviderContext(identity['account_id'],owner,repository.generation(),False,identity['workspace_id'])
        service=MeshyHumanService(coordinator=coordinator,scope=scope,profile_selection=selection,barrier=barrier,
            recovery_context_for=recovery_context_for,artifact_store=artifacts)
        async def preview_read(identity):
            owner=auth.username_for_account_id(identity['account_id'])
            return await read_original_preview(store=store,auth_manager=auth,account_barrier=barrier,owner=owner,identity=identity,source_current=source_current)
        service.preview_read=preview_read
        return service

    def _tripo_service_for_state(state, workspace: str, session_current, before_dispatch=None):
        from src.openclank.tripo_provider import admitted_tripo_profile, _current
        from src.openclank.tripo_human import TripoHumanService
        from src.openclank.media_adapters.tripo import TripoAdapter
        from src.openclank.media_files import TripoFilesScope, MediaFilesPublisher, human_session_authority
        from src.openclank.media_jobs import MediaCoordinator
        auth,barrier,store,sessions,artifacts=(getattr(state,key,None) for key in
            ('auth_manager','account_request_barrier','provider_store','session_manager','media_artifact_store'))
        if not all((auth,barrier,store,sessions,artifacts)) or not callable(getattr(store,'tripo_job_credential',None)):
            raise HTTPException(503,'Tripo jobs unavailable')
        files=facade_for_state(state,copal_workspace=workspace)
        async def context_for(identity):
            owner=auth.username_for_account_id(identity['account_id'])
            if not owner or auth.account_id(owner)!=identity['account_id'] or repository.generation()!=identity['policy_generation']:
                raise ValueError('media authority changed')
            return ProviderContext(identity['account_id'],owner,identity['policy_generation'],False,identity['workspace_id'])
        def resolve(identity,job=None):
            owner=auth.username_for_account_id(identity['account_id'])
            return admitted_tripo_profile(store=store,auth_manager=auth,account_barrier=barrier,owner=owner,identity=identity,job=job)
        scope=TripoFilesScope(facade=files,context_for=context_for,
            session_current=session_current,resolve_profile=resolve)
        adapter=TripoAdapter(resolve_profile=resolve,artifact_store=artifacts,before_dispatch=before_dispatch)
        publisher=MediaFilesPublisher(facade=files,context_for=context_for,fetch_artifact=adapter.fetch_artifact)
        coordinator=MediaCoordinator(barrier=barrier,account_for_owner=auth.account_id,owner_for_account=auth.username_for_account_id,
            scope_current=scope.current,control_current=scope.control,adapters={adapter.adapter_id:adapter},publisher=publisher,
            foreground_runtime=lambda:getattr(getattr(state,'mimo_supervisor',None),'_s05_foreground_runtime',None),
            foreground_profile=lambda:getattr(state,'s05_profile',None),
            foreground_provider=lambda:getattr(state,'canonical_memory_provider',None) or getattr(state,'memory_provider',None))
        async def selection(owner,account,connection_id,provider_account_id):
            connection=await asyncio.to_thread(store.get_connection,owner=owner,connection_id=connection_id)
            selected=await asyncio.to_thread(store.get_account,owner=owner,account_id=provider_account_id)
            profile=await asyncio.to_thread(_current,store=store,auth=auth,owner=owner,host_account=account,
                connection_id=connection_id,connection_revision=connection.revision,provider_account_id=provider_account_id,
                provider_account_revision=selected.revision,credential_version=selected.credential_version)
            return profile.job_binding()
        async def recovery_context_for(identity):
            owner=auth.username_for_account_id(identity['account_id'])
            if not owner or auth.account_id(owner)!=identity['account_id']: raise ValueError('media account changed')
            return ProviderContext(identity['account_id'],owner,repository.generation(),False,identity['workspace_id'])
        service=TripoHumanService(coordinator=coordinator,scope=scope,profile_selection=selection,barrier=barrier,
            recovery_context_for=recovery_context_for,artifact_store=artifacts)
        return service

    def _fal_service_for_state(state, workspace: str, session_current, before_dispatch=None):
        from src.openclank.fal_provider import admitted_fal_profile, _current
        from src.openclank.fal_human import FalHumanService
        from src.openclank.media_adapters.fal import FalAdapter
        from src.openclank.media_files import FalFilesScope, MediaFilesPublisher, human_session_authority
        from src.openclank.media_jobs import MediaCoordinator
        auth,barrier,store,sessions,artifacts=(getattr(state,key,None) for key in
            ('auth_manager','account_request_barrier','provider_store','session_manager','media_artifact_store'))
        if not all((auth,barrier,store,sessions,artifacts)) or not callable(getattr(store,'fal_job_credential',None)):
            raise HTTPException(503,'Fal jobs unavailable')
        files=facade_for_state(state,copal_workspace=workspace)
        async def context_for(identity):
            owner=auth.username_for_account_id(identity['account_id'])
            if not owner or auth.account_id(owner)!=identity['account_id'] or repository.generation()!=identity['policy_generation']:
                raise ValueError('media authority changed')
            return ProviderContext(identity['account_id'],owner,identity['policy_generation'],False,identity['workspace_id'])
        def resolve(identity,job=None):
            owner=auth.username_for_account_id(identity['account_id'])
            return admitted_fal_profile(store=store,auth_manager=auth,account_barrier=barrier,owner=owner,identity=identity,job=job)
        scope=FalFilesScope(facade=files,context_for=context_for,
            session_current=session_current,resolve_profile=resolve)
        adapter=FalAdapter(resolve_profile=resolve,artifact_store=artifacts,before_dispatch=before_dispatch)
        publisher=MediaFilesPublisher(facade=files,context_for=context_for,fetch_artifact=adapter.fetch_artifact)
        coordinator=MediaCoordinator(barrier=barrier,account_for_owner=auth.account_id,owner_for_account=auth.username_for_account_id,
            scope_current=scope.current,control_current=scope.control,adapters={adapter.adapter_id:adapter},publisher=publisher,
            foreground_runtime=lambda:getattr(getattr(state,'mimo_supervisor',None),'_s05_foreground_runtime',None),
            foreground_profile=lambda:getattr(state,'s05_profile',None),
            foreground_provider=lambda:getattr(state,'canonical_memory_provider',None) or getattr(state,'memory_provider',None))
        async def selection(owner,account,connection_id,provider_account_id):
            connection=await asyncio.to_thread(store.get_connection,owner=owner,connection_id=connection_id)
            selected=await asyncio.to_thread(store.get_account,owner=owner,account_id=provider_account_id)
            profile=await asyncio.to_thread(_current,store=store,auth=auth,owner=owner,host_account=account,
                connection_id=connection_id,connection_revision=connection.revision,provider_account_id=provider_account_id,
                provider_account_revision=selected.revision,credential_version=selected.credential_version)
            return profile.job_binding()
        async def recovery_context_for(identity):
            owner=auth.username_for_account_id(identity['account_id'])
            if not owner or auth.account_id(owner)!=identity['account_id']: raise ValueError('media account changed')
            return ProviderContext(identity['account_id'],owner,repository.generation(),False,identity['workspace_id'])
        service=FalHumanService(coordinator=coordinator,scope=scope,profile_selection=selection,barrier=barrier,
            recovery_context_for=recovery_context_for,artifact_store=artifacts)
        return service

    def _voidless_service_for_state(state, workspace: str, session_current, before_dispatch):
        from src.openclank.voidless_provider import admitted_voidless_profile, _current
        from src.openclank.media_human import HumanMediaService
        from src.openclank.media_adapters.voidless import VoidlessAdapter
        from src.openclank.media_files import VoidlessFilesScope, MediaFilesPublisher, human_session_authority
        from src.openclank.media_jobs import MediaCoordinator
        auth,barrier,store,sessions,artifacts=(getattr(state,key,None) for key in
            ('auth_manager','account_request_barrier','provider_store','session_manager','media_artifact_store'))
        if not all((auth,barrier,store,sessions,artifacts)) or not callable(getattr(store,'voidless_job_credential',None)):
            raise HTTPException(503,'Voidless jobs unavailable')
        files=facade_for_state(state,copal_workspace=workspace)
        async def context_for(identity):
            owner=auth.username_for_account_id(identity['account_id'])
            if not owner or auth.account_id(owner)!=identity['account_id'] or repository.generation()!=identity['policy_generation']:
                raise ValueError('media authority changed')
            return ProviderContext(identity['account_id'],owner,identity['policy_generation'],False,identity['workspace_id'])
        def resolve(identity,job=None):
            owner=auth.username_for_account_id(identity['account_id'])
            return admitted_voidless_profile(store=store,auth_manager=auth,account_barrier=barrier,owner=owner,identity=identity,job=job)
        scope=VoidlessFilesScope(facade=files,context_for=context_for,
            session_current=session_current,resolve_profile=resolve)
        adapter=VoidlessAdapter(resolve_profile=resolve,artifact_store=artifacts,before_dispatch=before_dispatch,scope_current=scope.current)
        publisher=MediaFilesPublisher(facade=files,context_for=context_for,fetch_artifact=adapter.fetch_artifact)
        coordinator=MediaCoordinator(barrier=barrier,account_for_owner=auth.account_id,owner_for_account=auth.username_for_account_id,
            scope_current=scope.current,control_current=scope.control,adapters={adapter.adapter_id:adapter},publisher=publisher,
            foreground_runtime=lambda:getattr(getattr(state,'mimo_supervisor',None),'_s05_foreground_runtime',None),
            foreground_profile=lambda:getattr(state,'s05_profile',None),
            foreground_provider=lambda:getattr(state,'canonical_memory_provider',None) or getattr(state,'memory_provider',None))
        async def selection(owner,account,connection_id,provider_account_id):
            connection=await asyncio.to_thread(store.get_connection,owner=owner,connection_id=connection_id)
            selected=await asyncio.to_thread(store.get_account,owner=owner,account_id=provider_account_id)
            profile=await asyncio.to_thread(_current,store=store,auth=auth,owner=owner,host_account=account,
                connection_id=connection_id,connection_revision=connection.revision,provider_account_id=provider_account_id,
                provider_account_revision=selected.revision,credential_version=selected.credential_version)
            return profile.job_binding()
        async def recovery_context_for(identity):
            owner=auth.username_for_account_id(identity['account_id'])
            if not owner or auth.account_id(owner)!=identity['account_id']: raise ValueError('media account changed')
            return ProviderContext(identity['account_id'],owner,repository.generation(),False,identity['workspace_id'])
        service=HumanMediaService(coordinator=coordinator,scope=scope,profile_selection=selection,barrier=barrier,
            recovery_context_for=recovery_context_for,artifact_store=artifacts)
        return service

    def _voidless_human_service(request, workspace, identity, original):
        from src.openclank.media_files import human_session_authority
        state=request.app.state
        session=human_session_authority(session_manager=state.session_manager,auth_manager=state.auth_manager)
        async def current(candidate):
            return bool(_material_original_current(request,original) and candidate.get('account_id')==original[0]
                and candidate.get('session_id')==identity['session_id'] and candidate.get('workspace_id')==workspace
                and repository.generation()==identity['policy_generation'] and await session(candidate)
                and _material_original_current(request,original))
        async def before_dispatch():
            if not await current(identity): raise ValueError('Voidless authority changed')
            return True
        return _voidless_service_for_state(state,workspace,current,before_dispatch),before_dispatch

    def _fal_mesh_service_for_state(state, workspace: str, session_current, before_dispatch=None):
        from src.openclank.fal_provider import admitted_fal_profile, admitted_fal_image_mesh_profile, _current
        from src.openclank.fal_hunyuan_human import FalImageToMeshHumanService
        from src.openclank.media_adapters.fal_hunyuan import FalHunyuanAdapter
        from src.openclank.media_files import ImageToMeshFilesScope, MediaFilesPublisher, human_session_authority
        from src.openclank.media_jobs import MediaCoordinator
        auth,barrier,store,sessions,artifacts=(getattr(state,key,None) for key in
            ('auth_manager','account_request_barrier','provider_store','session_manager','media_artifact_store'))
        if not all((auth,barrier,store,sessions,artifacts)) or not callable(getattr(store,'fal_job_credential',None)):
            raise HTTPException(503,'Fal jobs unavailable')
        files=facade_for_state(state,copal_workspace=workspace)
        async def context_for(identity):
            owner=auth.username_for_account_id(identity['account_id'])
            if not owner or auth.account_id(owner)!=identity['account_id'] or repository.generation()!=identity['policy_generation']:
                raise ValueError('media authority changed')
            return ProviderContext(identity['account_id'],owner,identity['policy_generation'],False,identity['workspace_id'])
        def resolve(identity,job=None):
            owner=auth.username_for_account_id(identity['account_id'])
            return admitted_fal_profile(store=store,auth_manager=auth,account_barrier=barrier,owner=owner,identity=identity,job=job)
        def resolve_claim(identity,job=None):
            owner=auth.username_for_account_id(identity['account_id'])
            return admitted_fal_image_mesh_profile(store=store,auth_manager=auth,account_barrier=barrier,owner=owner,identity=identity,job=job)
        scope=ImageToMeshFilesScope(facade=files,context_for=context_for,
            session_current=session_current,resolve_profile=resolve)
        adapter=FalHunyuanAdapter(resolve_profile=resolve_claim,artifact_store=artifacts,source=scope.source,scope_current=scope.current,before_dispatch=before_dispatch)
        publisher=MediaFilesPublisher(facade=files,context_for=context_for,fetch_artifact=adapter.fetch_artifact)
        coordinator=MediaCoordinator(barrier=barrier,account_for_owner=auth.account_id,owner_for_account=auth.username_for_account_id,
            scope_current=scope.current,control_current=scope.control,adapters={adapter.adapter_id:adapter},publisher=publisher,
            foreground_runtime=lambda:getattr(getattr(state,'mimo_supervisor',None),'_s05_foreground_runtime',None),
            foreground_profile=lambda:getattr(state,'s05_profile',None),
            foreground_provider=lambda:getattr(state,'canonical_memory_provider',None) or getattr(state,'memory_provider',None))
        async def selection(owner,account,connection_id,provider_account_id):
            connection=await asyncio.to_thread(store.get_connection,owner=owner,connection_id=connection_id)
            selected=await asyncio.to_thread(store.get_account,owner=owner,account_id=provider_account_id)
            profile=await asyncio.to_thread(_current,store=store,auth=auth,owner=owner,host_account=account,
                connection_id=connection_id,connection_revision=connection.revision,provider_account_id=provider_account_id,
                provider_account_revision=selected.revision,credential_version=selected.credential_version)
            return profile.job_binding()
        async def recovery_context_for(identity):
            owner=auth.username_for_account_id(identity['account_id'])
            if not owner or auth.account_id(owner)!=identity['account_id']: raise ValueError('media account changed')
            return ProviderContext(identity['account_id'],owner,repository.generation(),False,identity['workspace_id'])
        service=FalImageToMeshHumanService(coordinator=coordinator,scope=scope,profile_selection=selection,barrier=barrier,
            recovery_context_for=recovery_context_for,artifact_store=artifacts)
        return service

    def _tripo_service(request: Request, workspace: str):
        from src.openclank.media_files import human_session_authority
        state=request.app.state
        current=human_session_authority(session_manager=getattr(state,'session_manager',None),auth_manager=getattr(state,'auth_manager',None))
        return _tripo_service_for_state(state,workspace,current)

    def _meshy_rig_service_for_state(state, workspace, session_current, before_dispatch):
        from src.openclank.meshy_provider import admitted_meshy_profile, _current
        from src.openclank.meshy_rig_human import MeshyRigHumanService
        from src.openclank.meshy_rig_files import MeshyRigFilesScope
        from src.openclank.media_adapters.meshy_rig import MeshyRigAdapter
        from src.openclank.media_files import MediaFilesPublisher
        from src.openclank.media_jobs import MediaCoordinator
        auth,barrier,store,artifacts=(getattr(state,key,None) for key in
            ('auth_manager','account_request_barrier','provider_store','media_artifact_store'))
        if not all((auth,barrier,store,artifacts)) or not callable(session_current) or not callable(before_dispatch):
            raise ValueError('Meshy rig unavailable')
        files=facade_for_state(state,copal_workspace=workspace)
        async def context_for(identity):
            owner=auth.username_for_account_id(identity['account_id'])
            if not owner or auth.account_id(owner)!=identity['account_id'] or repository.generation()!=identity['policy_generation']:
                raise ValueError('media authority changed')
            return ProviderContext(identity['account_id'],owner,identity['policy_generation'],False,identity['workspace_id'])
        def resolve(identity,job=None):
            owner=auth.username_for_account_id(identity['account_id'])
            return admitted_meshy_profile(store=store,auth_manager=auth,account_barrier=barrier,owner=owner,identity=identity,job=job)
        scope=MeshyRigFilesScope(facade=files,context_for=context_for,session_current=session_current,resolve_profile=resolve)
        adapter=MeshyRigAdapter(resolve_profile=resolve,artifact_store=artifacts,source=scope.source,source_current=scope.current,before_dispatch=before_dispatch)
        publisher=MediaFilesPublisher(facade=files,context_for=context_for,fetch_artifact=adapter.fetch_artifact)
        coordinator=MediaCoordinator(barrier=barrier,account_for_owner=auth.account_id,owner_for_account=auth.username_for_account_id,
            scope_current=scope.current,control_current=scope.control,adapters={adapter.adapter_id:adapter},publisher=publisher,
            foreground_runtime=lambda:getattr(getattr(state,'mimo_supervisor',None),'_s05_foreground_runtime',None),
            foreground_profile=lambda:getattr(state,'s05_profile',None),
            foreground_provider=lambda:getattr(state,'canonical_memory_provider',None) or getattr(state,'memory_provider',None))
        async def selection(owner,account,connection_id,provider_account_id):
            connection=await asyncio.to_thread(store.get_connection,owner=owner,connection_id=connection_id)
            selected=await asyncio.to_thread(store.get_account,owner=owner,account_id=provider_account_id)
            profile=await asyncio.to_thread(_current,store=store,auth=auth,owner=owner,host_account=account,
                connection_id=connection_id,connection_revision=connection.revision,provider_account_id=provider_account_id,
                provider_account_revision=selected.revision,credential_version=selected.credential_version)
            return profile.job_binding()
        async def recovery_context_for(identity):
            owner=auth.username_for_account_id(identity['account_id'])
            if not owner or auth.account_id(owner)!=identity['account_id']: raise ValueError('media account changed')
            return ProviderContext(identity['account_id'],owner,repository.generation(),False,identity['workspace_id'])
        return MeshyRigHumanService(coordinator=coordinator,scope=scope,profile_selection=selection,barrier=barrier,
            recovery_context_for=recovery_context_for,artifact_store=artifacts)

    def _meshy_rig_human_service(request, workspace, identity, original):
        from src.openclank.media_files import human_session_authority
        state=request.app.state
        session=human_session_authority(session_manager=state.session_manager,auth_manager=state.auth_manager)
        async def current(candidate):
            return bool(_material_original_current(request,original) and candidate.get('account_id')==original[0]
                and candidate.get('session_id')==identity['session_id'] and candidate.get('workspace_id')==workspace
                and repository.generation()==identity['policy_generation'] and await session(candidate)
                and _material_original_current(request,original))
        async def before_dispatch():
            if not await current(identity): raise ValueError('Rig authority changed')
            return True
        return _meshy_rig_service_for_state(state,workspace,current,before_dispatch),before_dispatch

    def _service_for_job(request, job, original=None):
        if (job.get('identity') or {}).get('adapter_id') == 'trim-webm-v1':
            return _trim_service(request,job['workspace_id'])
        if (job.get('identity') or {}).get('adapter_id') == 'screen-record-v1':
            return _recording_service(request,job['workspace_id'])
        if (job.get('identity') or {}).get('adapter_id') == 'latex-pdflatex-v1':
            return _latex_service(request,job['workspace_id'])
        if (job.get('identity') or {}).get('adapter_id') == 'voidless-image-v1':
            captured=original if original is not None else _material_original(request)
            return _voidless_human_service(request,job['workspace_id'],job['identity'],captured)[0]
        if (job.get('identity') or {}).get('adapter_id') == 'fal-flux-schnell-v1':
            from src.openclank.media_files import human_session_authority
            return _fal_service_for_state(request.app.state,job['workspace_id'],human_session_authority(session_manager=request.app.state.session_manager,auth_manager=request.app.state.auth_manager))
        if (job.get('identity') or {}).get('adapter_id') == 'fal-hunyuan-image3d-v1':
            from src.openclank.media_files import human_session_authority
            current=human_session_authority(session_manager=request.app.state.session_manager,auth_manager=request.app.state.auth_manager)
            async def before_dispatch():
                if not await current(job['identity']): raise ValueError('media authority changed')
            return _fal_mesh_service_for_state(request.app.state,job['workspace_id'],current,before_dispatch)
        if (job.get('identity') or {}).get('adapter_id') == 'meshy-rig-v1':
            captured=original if original is not None else _material_original(request)
            return _meshy_rig_human_service(request,job['workspace_id'],job['identity'],captured)[0]
        if (job.get('identity') or {}).get('adapter_id') == 'comfyui-material-v1':
            captured=original if original is not None else _material_original(request)
            account,owner=captured[:2]
            service,_=_comfy_material_human_service(request,job['workspace_id'],account,owner,job['identity'],captured)
            return service
        if (job.get('identity') or {}).get('adapter_id') == 'hunyuan3d-v2-shape-v1':
            captured=original if original is not None else _material_original(request)
            account,owner=captured[:2]
            service,_=_hunyuan_human_service(request,job['workspace_id'],account,owner,job['identity'],captured)
            return service
        if (job.get('identity') or {}).get('adapter_id') == 'trellis-image3d-v1':
            captured=original if original is not None else _material_original(request)
            account,owner=captured[:2]
            service,_=_trellis_human_service(request,job['workspace_id'],account,owner,job['identity'],captured)
            return service
        if (job.get('identity') or {}).get('adapter_id') == 'material-pack-v1':
            account,owner,factory,credential=original if original is not None else _material_original(request)
            service,_=_material_human_service(request,job['workspace_id'],account,owner,job['identity'],factory,credential)
            return service
        if (job.get('identity') or {}).get('adapter_id') == 'tripo-text3d-v1':
            return _tripo_service(request, job['workspace_id'])
        if (job.get('identity') or {}).get('adapter_id') in {'sprite-sheet-v1','sprite-sheet-crops-v1'}:
            return _sprite_sheet_service(request,job['workspace_id'])
        if (job.get('identity') or {}).get('adapter_id') == 'sprixen-animation-v1':
            return _sprixen_animation_service(request, job['workspace_id'])
        if (job.get('identity') or {}).get('adapter_id') in {'meshy-preview-v1','meshy-refine-v1'}:
            return _meshy_service(request, job['workspace_id'])
        if (job.get('identity') or {}).get('adapter_id') == 'sprixen-sprite-v1':
            return _sprixen_service(request, job['workspace_id'])
        if (job.get('identity') or {}).get('adapter_id') in {'blender-glb-png-v1','blender-glb-four-heading-v1','blender-glb-rig-clip-v1','blender-material-bake-v1'}:
            return _blender_service(request, job['workspace_id'],four_headings=(job['identity']['adapter_id']=='blender-glb-four-heading-v1'),rig_clip=(job['identity']['adapter_id']=='blender-glb-rig-clip-v1'),material_bake=(job['identity']['adapter_id']=='blender-material-bake-v1'),job_identity=job['identity'],original=original)
        return _media_service(request, job['workspace_id'])    # snapshot behaviour: older records (empty or unknown adapter id) stay readable

    # The factory is installed only with the existing artifact custody owner;
    # a provider result must not be published from a transient remote locator.
    if application_state is not None and getattr(application_state, 'media_artifact_store', None) is not None:
        application_state.media_human_service_factory = _media_service
        application_state.sprixen_human_service_factory = _sprixen_service
        if callable(getattr(getattr(application_state,'provider_store',None),'tripo_job_credential',None)):
            application_state.tripo_human_service_factory = _tripo_service
        if callable(getattr(getattr(application_state,'provider_store',None),'meshy_job_credential',None)):
            application_state.meshy_human_service_factory = _meshy_service
            application_state.meshy_rig_agent_service_factory = lambda *, workspace, session_current, before_dispatch: _meshy_rig_service_for_state(application_state,workspace,session_current,before_dispatch)
        if (sys.platform == 'linux' and not getattr(sys, 'frozen', False)
                and re.fullmatch(r'python(?:3(?:\.\d+)?)?', os.path.basename(sys.executable))):
            application_state.blender_human_service_factory = _blender_service
            def blender_agent_service_factory(*, workspace, session_current, before_dispatch, operation, variant='front'):
                if (operation not in {'material.bake','image.render'} or variant not in {'front','four_heading','rig_clip'}
                        or (operation=='material.bake' and variant!='front') or not callable(session_current) or not callable(before_dispatch)):
                    raise ValueError('Blender authority unavailable')
                return _blender_service_for_state(application_state,workspace,session_current,before_dispatch,
                    material_bake=operation=='material.bake',four_headings=variant=='four_heading',rig_clip=variant=='rig_clip')
            application_state.blender_agent_service_factory = blender_agent_service_factory

    def _media_available(request: Request) -> bool:
        state = request.app.state
        return (getattr(state, 'media_human_service_factory', None) is _media_service
            and all(getattr(state, key, None) is not None for key in
                    ('auth_manager','account_request_barrier','provider_store','session_manager','media_artifact_store')))

    def _media_failure(exc: Exception):
        from src.openclank.media_jobs import MediaUnavailable
        if isinstance(exc, MediaUnavailable) and str(exc) in {'media scope changed', 'media source changed'}:
            raise HTTPException(409, {'code':'source_stale', 'message':'Source or destination authority changed or expired; explicit recovery is required.'}) from exc
        raise HTTPException(409, 'Media job unavailable or changed') from exc

    @router.get('/media/jobs')
    async def list_human_media(request: Request, response: Response, after: str = '', limit: int = Query(default=50, ge=1, le=100)):
        response.headers['Cache-Control'] = 'private, no-store'
        if not _media_available(request): raise HTTPException(503, 'Media jobs unavailable')
        from src import bg_jobs
        from src.openclank.media_human import HumanMediaService
        account, owner = _media_human(request)
        try:
            state=request.app.state;auth=getattr(state,'auth_manager',None);barrier=getattr(state,'account_request_barrier',None);token=request.cookies.get('open_clank_session')
            page = await asyncio.to_thread(bg_jobs.list_media, account_id=account, owner=owner, after=after, limit=limit)
            key=str(owner).strip().lower()
            if (not auth or not barrier or not token or _media_human(request)!=(account,owner) or state.auth_manager is not auth
                or state.account_request_barrier is not barrier or request.cookies.get('open_clank_session')!=token
                or auth.get_username_for_token(token)!=owner or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner
                or barrier._fenced(key) or (barrier._absolute_fence and barrier._absolute_fence(key))): raise ValueError('media authority changed')
            return {'items':[{**HumanMediaService.project(row), 'detached':row['chat_detached'], 'recovery':row['recovery']} for row in page['items']],
                    'nextCursor':page['next_cursor'], 'omitted':page['omitted']}
        except Exception as exc:
            raise HTTPException(409, 'Media job list unavailable') from exc

    @router.post('/media/inspect')
    async def inspect_human_media(request: Request, body: HumanMediaInspectRequest):
        if not _media_available(request): raise HTTPException(503, 'Media jobs unavailable')
        from src.openclank.media_adapters.comfyui import inspect_recipe
        from src.openclank.media_files import human_session_authority
        account, owner = _media_human(request)
        sessions = getattr(request.app.state, 'session_manager', None)
        auth = getattr(request.app.state, 'auth_manager', None)
        if sessions is None or auth is None: raise HTTPException(503, 'Media jobs unavailable')
        try:
            session = await asyncio.to_thread(sessions.get_session, body.sessionID)
            workspace = str(getattr(session, 'workspace_id', None) or 'default')
            identity = {'account_id':account, 'session_id':body.sessionID, 'workspace_id':workspace}
            current = human_session_authority(session_manager=sessions, auth_manager=auth)
            if not await current(identity): raise ValueError('session unavailable')
            evidence, raw = await facade(request, copal_workspace=workspace).capture_media_recipe(
                context(request, workspace=workspace), resource_ref=body.recipeRef)
            _recipe, summary = inspect_recipe(raw)
            if not await current(identity): raise ValueError('session changed')
            return {'name':evidence['recipe_name'], 'sha256':evidence['recipe_sha256'],
                    'nodeClasses':summary['node_classes'], 'slots':summary['slots']}
        except Exception as exc:
            raise HTTPException(409, 'Media recipe unavailable or changed') from exc

    def _mesh_profile_revision(binding):
        fields = ('connectionID','connectionRevision','providerAccountID','providerAccountRevision','credentialVersion','interface')
        return hashlib.sha256(json.dumps([binding[key] for key in fields],separators=(',', ':'),ensure_ascii=True).encode()).hexdigest()

    # Per-operation facts the Files menu renders (label, what it needs from the user). Not a vendor list: every
    # profile row below is attached to one of these operations by what the profile can do.
    _MEDIA_OPERATIONS = {
        'image.generate': {'label': 'Generate image'},
        'mesh.generate': {'label': 'Generate 3D model'},
        'animation.generate': {'label': 'Animate sprite'},
        'material.bake': {'label': 'Bake material maps', 'form': {'resolution_choices': [256, 512, 1024, 2048], 'output': {'noun': 'ZIP', 'default_name': 'material.zip'}}},
        'blender.render': {'label': 'Render GLB image', 'form': {'output': {'noun': 'PNG', 'default_name': 'render.png'}}},
        'blender.four_headings': {'label': 'Four-heading sheet', 'form': {'output': {'noun': 'PNG', 'default_name': 'render.png'}}},
        'blender.rig_pose': {'label': 'Joint pose clip', 'form': {'select_joint': True, 'output': {'noun': 'PNG', 'default_name': 'render.png'}}},
    }
    _BLENDER_OPERATIONS = ('material.bake', 'blender.render', 'blender.four_headings', 'blender.rig_pose')

    def _with_operation_facts(listing):
        facts = _MEDIA_OPERATIONS.get
        rows = []
        for row in listing['profiles']:
            known = facts(row.get('operation')) or {}
            extra = {**({'operation_label': known['label']} if 'label' in known else {}), **({'form': known['form']} if 'form' in known else {})}
            rows.append({**row, **extra})
        return {**listing, 'profiles': rows}

    async def _human_media_listing(request: Request, body: HumanMediaProfilesRequest):
        # Each row carries its own 'family' (the vendor whose lister and service produced it); /media/profiles strips it, prepare dispatches on it.
        from src.openclank.media_files import human_session_authority
        account, owner = _media_human(request)
        state = request.app.state
        session = await asyncio.to_thread(state.session_manager.get_session, body.sessionID)
        workspace = str(getattr(session, 'workspace_id', None) or 'default')
        identity = {'account_id':account, 'session_id':body.sessionID, 'workspace_id':workspace}
        current = human_session_authority(session_manager=state.session_manager, auth_manager=state.auth_manager)
        generation = repository.generation()
        async def check():
            if (_media_human(request)!=(account,owner) or repository.generation()!=generation or not await current(identity)
                or _media_human(request)!=(account,owner) or repository.generation()!=generation):
                raise ValueError('media authority changed')
        await check()
        if body.operation == 'video.trim':
            from src.openclank import recording_child
            service=_trim_service(request,workspace)
            helper=await recording_child.trim_readiness()
            if not helper:return {'profiles':[],'partial':False}
            try:revision=helper['sha256']
            finally:os.close(helper['fd'])
            if not await service.scope.session_current(identity): raise ValueError('trim authority changed')
            return {'profiles':[{'profile_id':'trim-webm-v1','label':'Local WebM trim','revision':revision,
                'operation':'video.trim','input_kinds':['host_video'],'output_kinds':['video'],
                'output_mime':'video/webm','metered':False,'cancel_available':True,'family':'trim'}],'partial':False}
        if body.operation == 'mesh.generate':
            listed = await asyncio.to_thread(state.provider_store.list_human_tripo_profiles, owner=owner) if callable(getattr(state,'tripo_human_service_factory',None)) else {'profiles':[],'partial':False}
            await check()
            service=None; partial=listed['partial']
            if listed['profiles']:
                try: service=_tripo_service(request, workspace)
                except Exception: listed={'profiles':[]}; partial=True    # one vendor's service cannot be built: skip its rows, mark partial
            profiles=[]
            for row in listed['profiles']:
                try:
                    binding=await service._bounded_read(service.profile_selection(owner,account,row['connection_id'],row['account_id']))
                    await check()
                    if (binding.get('connectionRevision')!=row['connection_revision'] or binding.get('providerAccountRevision')!=row['account_revision']
                            or binding.get('credentialVersion')!=row['credential_version']): raise ValueError('profile changed')
                    profiles.append({'profile_id':row['account_id'],'label':row['label'],'revision':_mesh_profile_revision(binding),
                        'operation':'mesh.generate','input_kinds':['text_prompt'],'output_kinds':['mesh'],
                        'output_mime':'model/gltf-binary','metered':True,'cancel_available':False,'family':'tripo'})
                except Exception:
                    await check(); partial=True
            fal=await asyncio.to_thread(state.provider_store.list_human_fal_profiles,owner=owner)
            await check()
            async def before_dispatch():
                await check()
            try: mesh=_fal_mesh_service_for_state(state,workspace,current,before_dispatch)
            except Exception: mesh=None; partial=partial or bool(fal['profiles']); fal={'profiles':[],'partial':False}    # partial only if this owner has fal rows being left out
            from src.openclank.media_human import model_for_profile
            mesh_model=model_for_profile('fal-hunyuan-image3d-v1','1')
            for row in fal['profiles']:
                try:
                    binding=await mesh._bounded_read(mesh.profile_selection(owner,account,row['connection_id'],row['account_id']))
                    await check()
                    if (binding.get('connectionRevision')!=row['connection_revision'] or binding.get('providerAccountRevision')!=row['account_revision'] or binding.get('credentialVersion')!=row['credential_version']): raise ValueError('profile changed')
                    if mesh_model is None: raise ValueError('model unavailable')
                    profiles.append({'profile_id':row['account_id'],'label':row['label'],'revision':_mesh_profile_revision(binding),'model':mesh_model,
                        'operation':'mesh.generate','input_kinds':['image'],'output_kinds':['mesh'],
                        'output_mime':'model/gltf-binary','metered':True,'cancel_available':False,'family':'fal-mesh'})
                except Exception:
                    await check(); partial=True
            await check()
            return {'profiles':profiles[:20],'partial':partial or fal['partial'] or len(profiles)>20}
        if body.operation in _BLENDER_OPERATIONS:
            try: connections=await asyncio.to_thread(state.provider_store.list_connections, owner=owner)
            except Exception: return {'profiles':[], 'partial':True}
            await check()
            local=[c for c in connections if c.family_id=='blender' and c.kind=='local' and c.enabled and isinstance(c.id,str) and c.id]
            outputs={'material.bake':(['host_glb'],['archive'],'application/zip'),}
            inputs,kinds,mime=outputs.get(body.operation,(['host_glb'],['image'],'image/png'))
            return {'profiles':[{'profile_id':c.id,'label':str(c.label or c.id)[:120],'revision':c.revision,'operation':body.operation,
                    'input_kinds':inputs,'output_kinds':kinds,'output_mime':mime,'metered':False,'cancel_available':True,'family':'blender'}
                    for c in local[:20]], 'partial':len(local)>20}
        if body.operation != 'image.generate':
            return {'profiles':[], 'partial':False}
        from src.openclank.media_files import human_session_authority
        fal_listed=await asyncio.to_thread(state.provider_store.list_human_fal_profiles,owner=owner)
        skipped=False    # a vendor whose service cannot be built is skipped; partial is set only if it had rows for this owner
        try: fal_service=_fal_service_for_state(state,workspace,human_session_authority(session_manager=state.session_manager,auth_manager=state.auth_manager))
        except Exception: fal_service=None; skipped=skipped or bool(fal_listed['profiles']); fal_listed={'profiles':[],'partial':False}
        voidless_listed=await asyncio.to_thread(state.provider_store.list_human_voidless_profiles,owner=owner)
        async def list_guard():
            await check(); return True
        try: voidless_service=_voidless_service_for_state(state,workspace,human_session_authority(session_manager=state.session_manager,auth_manager=state.auth_manager),list_guard)
        except Exception: voidless_service=None; skipped=skipped or bool(voidless_listed['profiles']); voidless_listed={'profiles':[],'partial':False}
        listed = await asyncio.to_thread(state.provider_store.list_agent_comfy_profiles, owner=owner)
        await check()
        try: service = _media_service(request, workspace)
        except Exception: service=None; skipped=skipped or bool(listed['profiles']); listed={'profiles':[],'partial':False}
        profiles=[]; partial=listed['partial'] or skipped
        for row in listed['profiles']:
            try:
                binding=await service._bounded_read(service.profile_selection(owner,account,row['connection_id']))
                await check()
                if binding.get('connectionRevision')!=row['revision']: raise ValueError('profile changed')
                profiles.append({'profile_id':row['connection_id'],'label':row['label'],'revision':row['revision'],
                    'operation':'image.generate','input_kinds':['workflow_recipe'],'output_kinds':['image'],'family':'comfy'})
            except Exception:
                await check(); partial=True
        for row in fal_listed['profiles']:
            try:
                binding=await fal_service._bounded_read(fal_service.profile_selection(owner,account,row['connection_id'],row['account_id']))
                await check()
                if (binding.get('connectionRevision')!=row['connection_revision'] or binding.get('providerAccountRevision')!=row['account_revision'] or binding.get('credentialVersion')!=row['credential_version']): raise ValueError('profile changed')
                profiles.append({'profile_id':row['account_id'],'label':row['label'],'revision':_mesh_profile_revision(binding),
                    'operation':'image.generate','input_kinds':['text_prompt'],'output_kinds':['image'],'output_mime':'image/png','metered':True,'cancel_available':False,'family':'fal'})
            except Exception:
                await check();partial=True
        for row in voidless_listed['profiles']:
            try:
                binding=await voidless_service._bounded_read(voidless_service.profile_selection(owner,account,row['connection_id'],row['account_id']))
                await check()
                if (binding.get('connectionRevision')!=row['connection_revision'] or binding.get('providerAccountRevision')!=row['account_revision'] or binding.get('credentialVersion')!=row['credential_version']): raise ValueError('profile changed')
                profiles.append({'profile_id':row['account_id'],'label':row['label']+' · 64px PNG','revision':_mesh_profile_revision(binding),
                    'operation':'image.generate','input_kinds':['text_prompt'],'output_kinds':['image'],'output_mime':'image/png','metered':True,'cancel_available':False,'family':'voidless'})
            except Exception:
                await check();partial=True
        partial=partial or fal_listed['partial'] or voidless_listed['partial'] or len(profiles)>20
        profiles=profiles[:20]
        await check()
        return {'profiles':profiles,'partial':partial}

    async def _human_media_profiles(request: Request, body: HumanMediaProfilesRequest):
        listing=_with_operation_facts(await _human_media_listing(request,body))
        return {**listing,'profiles':[{k:v for k,v in row.items() if k!='family'} for row in listing['profiles']]}

    async def _human_media_catalog(request: Request, body: HumanMediaProfilesRequest):
        """operation '*': every menu operation with its compatible configured profiles (or the reason there are none)."""
        operations=[]
        for operation,facts in _MEDIA_OPERATIONS.items():
            try: listing=await _human_media_profiles(request,HumanMediaProfilesRequest(sessionID=body.sessionID,operation=operation))
            except Exception: listing={'profiles':[],'partial':True}
            profiles=listing['profiles']
            operations.append({'operation':operation,'label':facts['label'],'profiles':profiles,'partial':bool(listing.get('partial')),
                **({} if profiles else {'reason':'No configured profile can perform this operation.' if not listing.get('partial') else 'Profiles could not be read right now.'})})
        return {'operations':operations}

    async def _selected_profile(request: Request, body, input_kind):
        """Resolve body.profile_id to its OWN family: it must be listed by exactly one family and that family must take input_kind.
        Returns the listed row, or None (unlisted, listed by several families, or the family has no such input kind)."""
        try: listing=await _human_media_listing(request,HumanMediaProfilesRequest(sessionID=body.sessionID,operation=body.operation))
        except Exception as exc: raise HTTPException(409,'Media profiles unavailable or changed') from exc
        rows=[row for row in listing['profiles'] if row['profile_id']==body.profile_id]
        if len({row['family'] for row in rows})!=1 or input_kind not in rows[0]['input_kinds']: return None
        return rows[0]

    @router.post('/media/profiles')
    async def human_media_profiles(request: Request, body: HumanMediaProfilesRequest):
        if not _media_available(request): raise HTTPException(503,'Media jobs unavailable')
        try: return await (_human_media_catalog if body.operation=='*' else _human_media_profiles)(request,body)
        except Exception as exc: raise HTTPException(409,'Media profiles unavailable or changed') from exc

    @router.post('/media/material/prepare')
    async def prepare_material(request: Request):
        account,owner,factory,credential=_material_original(request)
        body=await _material_body(request)
        if (type(body) is not dict or set(body)!={'sessionID','sources','destination','parameters'} or type(body['sessionID']) is not str
            or not body['sessionID'] or len(body['sessionID'])>256 or type(body['sources']) is not list
            or type(body['destination']) is not dict or set(body['destination'])!={'resource_ref','name'}
            or any(type(body['destination'].get(k)) is not str or not body['destination'][k] for k in ('resource_ref','name'))
            or body['parameters']!={}): raise HTTPException(422,'Invalid material request')
        state=request.app.state;generation=repository.generation()
        if credential() is not True or _media_human(request)!=(account,owner):raise HTTPException(409,'Material authority changed')
        session=await asyncio.to_thread(state.session_manager.get_session,body['sessionID'])
        if not session or session.owner!=owner or getattr(session,'incognito',False):raise HTTPException(409,'Material session unavailable')
        workspace=str(getattr(session,'workspace_id',None) or 'default')
        identity={'account_id':account,'session_id':body['sessionID'],'workspace_id':workspace,'policy_generation':generation}
        service,current=_material_human_service(request,workspace,account,owner,identity,factory,credential)
        await current()
        try:
            prepared=await service.prepare_material(account=account,owner=owner,workspace=workspace,generation=generation,session_id=body['sessionID'],sources=body['sources'],destination_ref=body['destination']['resource_ref'],destination_name=body['destination']['name'],parameters=body['parameters'])
            await current();return prepared
        except Exception as exc:raise HTTPException(409,'Material preparation unavailable or changed') from exc

    @router.post('/media/material/run')
    async def run_material(request: Request):
        account,owner,factory,credential=_material_original(request)
        body=await _material_body(request)
        try:parsed=HumanMediaRunRequest.model_validate(body)
        except Exception as exc:raise HTTPException(422,'Invalid material request') from exc
        if parsed.parameters!={}:raise HTTPException(422,'Invalid material parameters')
        from src import bg_jobs
        job=await asyncio.to_thread(bg_jobs.get_media,parsed.jobID,account_id=account)
        if not job or job.get('owner')!=owner or (job.get('identity') or {}).get('adapter_id')!='material-pack-v1':raise HTTPException(404,'Material job unavailable')
        service,current=_material_human_service(request,job['workspace_id'],account,owner,job['identity'],factory,credential)
        try:
            await current();result=await service.run(account=account,owner=owner,job_id=parsed.jobID,revision=parsed.revision,parameters={})
            await current();return result
        except Exception as exc:_media_failure(exc)

    @router.post('/media/operation/prepare')
    async def prepare_human_media_operation(request: Request):
        original=_material_original(request)
        raw=await _material_body(request)
        try: body=HumanMediaOperationPrepareRequest.model_validate(raw)
        except ValidationError as exc: raise HTTPException(422,'Invalid media request') from exc
        if not _material_original_current(request,original): raise HTTPException(409,'Media authority changed')
        if body.operation == 'video.trim':
            if not _media_available(request): raise HTTPException(503,'Media jobs unavailable')
            if (body.profile_id!='trim-webm-v1' or not isinstance(body.profile_revision,str)
                or not re.fullmatch(r'[0-9a-f]{64}',body.profile_revision) or len(body.inputs)!=1
                or set(body.inputs[0])!={'kind','resource_ref','expected_revision'} or body.inputs[0].get('kind')!='host_video'
                or not isinstance(body.inputs[0].get('resource_ref'),str) or not isinstance(body.inputs[0].get('expected_revision'),dict)
                or set(body.parameters)!={'start_ms','end_ms'} or any(type(body.parameters.get(k)) is not int for k in ('start_ms','end_ms'))
                or set(body.destination)!={'resource_ref','name'} or not all(isinstance(body.destination.get(k),str) for k in ('resource_ref','name'))):
                raise HTTPException(422,'Unsupported trim inputs')
            account,owner=_media_human(request);state=request.app.state
            session=await asyncio.to_thread(state.session_manager.get_session,body.sessionID)
            if session.owner!=owner or getattr(session,'incognito',False): raise HTTPException(409,'Media session unavailable')
            workspace=str(getattr(session,'workspace_id',None) or 'default');service=_trim_service(request,workspace)
            try:
                return await service.prepare_trim(account=account,owner=owner,workspace=workspace,generation=repository.generation(),
                    session_id=body.sessionID,source_ref=body.inputs[0]['resource_ref'],source_revision=body.inputs[0]['expected_revision'],
                    destination_ref=body.destination['resource_ref'],destination_name=body.destination['name'],
                    start_ms=body.parameters['start_ms'],end_ms=body.parameters['end_ms'],expected_helper_sha=body.profile_revision)
            except Exception as exc: raise HTTPException(409,'Trim preparation unavailable or changed') from exc
        if body.operation == 'mesh.generate' and len(body.inputs)==1 and body.inputs[0].get('kind')=='image':
            from src.openclank.media_files import human_session_authority
            if (set(body.inputs[0])!={'kind','resource_ref'} or not isinstance(body.inputs[0].get('resource_ref'),str)
                or set(body.destination)!={'resource_ref','name'} or not all(isinstance(body.destination.get(k),str) for k in ('resource_ref','name'))
                or set(body.parameters)-{'seed'} or ('seed' in body.parameters and (type(body.parameters['seed']) is not int or not 0<=body.parameters['seed']<=2147483647))
                or not isinstance(body.profile_revision,str) or not re.fullmatch(r'[0-9a-f]{64}',body.profile_revision)):
                raise HTTPException(422,'Unsupported media operation or inputs')
            selected=await _selected_profile(request,body,'image')
            if (selected is None or selected['family']!='fal-mesh' or selected['revision']!=body.profile_revision
                    or not isinstance(selected.get('model'),dict)): raise HTTPException(409,'Media profile changed')
            account,owner=_media_human(request);state=request.app.state;generation=repository.generation()
            session=await asyncio.to_thread(state.session_manager.get_session,body.sessionID)
            if session.owner!=owner or getattr(session,'incognito',False): raise HTTPException(409,'Media session unavailable')
            workspace=str(getattr(session,'workspace_id',None) or 'default')
            current=human_session_authority(session_manager=state.session_manager,auth_manager=state.auth_manager)
            identity={'account_id':account,'session_id':body.sessionID,'workspace_id':workspace}
            async def before_dispatch():
                if _media_human(request)!=(account,owner) or repository.generation()!=generation or not await current(identity) or _media_human(request)!=(account,owner) or repository.generation()!=generation: raise ValueError('media authority changed')
            rows=await asyncio.to_thread(state.provider_store.list_human_fal_profiles,owner=owner)
            matches=[x for x in rows['profiles'] if x['account_id']==body.profile_id]
            if len(matches)!=1: raise HTTPException(409,'Media profile unavailable')
            row=matches[0];await before_dispatch()
            service=_fal_mesh_service_for_state(state,workspace,current,before_dispatch)
            prepared=await service.prepare_image_mesh(account=account,owner=owner,workspace=workspace,generation=generation,session_id=body.sessionID,connection_id=row['connection_id'],provider_account_id=row['account_id'],source_ref=body.inputs[0]['resource_ref'],destination_ref=body.destination['resource_ref'],destination_name=body.destination['name'],parameters=body.parameters)
            await before_dispatch()
            if _mesh_profile_revision(prepared['profile'])!=body.profile_revision: raise HTTPException(409,'Media profile changed')
            from src.openclank.media_human import model_for_job
            job=await asyncio.to_thread(service._job,account,owner,prepared['jobID'])
            await before_dispatch()
            original=job.get('identity') or {}
            if (job.get('status')!='prepared' or job.get('revision')!=prepared['revision']
                    or original.get('account_id')!=account or original.get('owner')!=owner
                    or original.get('session_id')!=body.sessionID or original.get('workspace_id')!=workspace
                    or original.get('policy_generation')!=generation or original.get('operation')!=body.operation
                    or original.get('resource_binding',{}).get('image_source')!=prepared['source']
                    or any(original.get('resource_binding',{}).get(k)!=v for k,v in prepared['profile'].items())):
                raise HTTPException(409,'Media review changed')
            model=model_for_job(job)
            if model is None or model!=selected['model']: raise HTTPException(409,'Media model changed')
            prepared['model']=model
            return prepared
        if body.operation == 'mesh.generate':
            if (len(body.inputs)!=1 or set(body.inputs[0])!={'kind','text'} or body.inputs[0].get('kind')!='text_prompt'
                or not isinstance(body.inputs[0].get('text'),str) or body.parameters!={}
                or set(body.destination)!={'resource_ref','name'} or not all(isinstance(body.destination.get(k),str) for k in ('resource_ref','name'))
                or not isinstance(body.profile_revision,str) or not re.fullmatch(r'[0-9a-f]{64}',body.profile_revision)):
                raise HTTPException(422,'Unsupported media operation or inputs')
            selected=await _selected_profile(request,body,'text_prompt')
            if selected is None or selected['family']!='tripo' or selected['revision']!=body.profile_revision: raise HTTPException(409,'Media profile unavailable or changed')
            account,owner=_media_human(request)
            try:
                row=await asyncio.to_thread(request.app.state.provider_store.get_account,owner=owner,account_id=body.profile_id)
                prepared=await prepare_tripo_media(request,TripoMediaPrepareRequest(sessionID=body.sessionID,connectionID=row.connection_id,
                    providerAccountID=body.profile_id,destinationRef=body.destination['resource_ref'],destinationName=body.destination['name'],
                    parameters={'prompt':body.inputs[0]['text']}))
                if _mesh_profile_revision(prepared['profile'])!=body.profile_revision: raise ValueError('profile changed')
                return prepared
            except (ValidationError, ValueError, KeyError) as exc:
                raise HTTPException(409,'Media profile unavailable or changed') from exc
        if body.operation=='image.generate' and len(body.inputs)==1 and body.inputs[0].get('kind')=='text_prompt':
            from src.openclank.media_files import human_session_authority
            if set(body.inputs[0])!={'kind','text'} or body.parameters!={} or set(body.destination)!={'resource_ref','name'}: raise HTTPException(422,'Unsupported media inputs')
            selected=await _selected_profile(request,body,'text_prompt')
            if selected is None or selected['revision']!=body.profile_revision: raise HTTPException(409,'Media profile changed')
            account,owner=_media_human(request);state=request.app.state
            session=await asyncio.to_thread(state.session_manager.get_session,body.sessionID)
            if session.owner!=owner or getattr(session,'incognito',False): raise HTTPException(409,'Media session unavailable')
            workspace=str(getattr(session,'workspace_id',None) or 'default')
            voidless_rows=await asyncio.to_thread(state.provider_store.list_human_voidless_profiles,owner=owner)
            matches=[row for row in voidless_rows['profiles'] if row['account_id']==body.profile_id]
            if len(matches)>1: raise HTTPException(409,'Media profile unavailable')
            if selected['family']=='voidless':
                if not matches: raise HTTPException(409,'Media profile unavailable')
                generation=repository.generation()
                identity={'account_id':account,'session_id':body.sessionID,'workspace_id':workspace,'policy_generation':generation}
                service,before_dispatch=_voidless_human_service(request,workspace,identity,original)
                await before_dispatch();row=matches[0]
                prepared=await service.prepare_voidless(account=account,owner=owner,workspace=workspace,generation=generation,
                    session_id=body.sessionID,connection_id=row['connection_id'],provider_account_id=row['account_id'],
                    destination_ref=body.destination['resource_ref'],destination_name=body.destination['name'],parameters={'prompt':body.inputs[0]['text']})
                await before_dispatch()
                if _mesh_profile_revision(prepared['profile'])!=body.profile_revision: raise HTTPException(409,'Media profile changed')
                return prepared
            rows=await asyncio.to_thread(state.provider_store.list_human_fal_profiles,owner=owner)
            matches=[row for row in rows['profiles'] if row['account_id']==body.profile_id]
            if len(matches)!=1: raise HTTPException(409,'Media profile unavailable')
            row=matches[0];service=_fal_service_for_state(state,workspace,human_session_authority(session_manager=state.session_manager,auth_manager=state.auth_manager))
            generation=repository.generation()
            prepared=await service.prepare_text_image(account=account,owner=owner,workspace=workspace,generation=generation,session_id=body.sessionID,connection_id=row['connection_id'],provider_account_id=row['account_id'],destination_ref=body.destination['resource_ref'],destination_name=body.destination['name'],parameters={'prompt':body.inputs[0]['text']})
            current=human_session_authority(session_manager=state.session_manager,auth_manager=state.auth_manager)
            if (_media_human(request)!=(account,owner) or repository.generation()!=generation or not await current({'account_id':account,'session_id':body.sessionID,'workspace_id':workspace}) or _mesh_profile_revision(prepared['profile'])!=body.profile_revision): raise HTTPException(409,'Media profile changed')
            return prepared
        if (body.operation!='image.generate' or len(body.inputs)!=1 or set(body.inputs[0])!={'kind','resource_ref'}
            or body.inputs[0].get('kind')!='workflow_recipe' or not isinstance(body.inputs[0].get('resource_ref'),str)
            or set(body.destination)!={'resource_ref','name'} or not all(isinstance(body.destination.get(k),str) for k in ('resource_ref','name'))):
            raise HTTPException(422,'Unsupported media operation or inputs')
        selected=await _selected_profile(request,body,'workflow_recipe')
        if selected is None or selected['family']!='comfy': raise HTTPException(409,'Media profile unavailable or changed')
        try:
            prepared=HumanMediaPrepareRequest(sessionID=body.sessionID,connectionID=body.profile_id,
                recipeRef=body.inputs[0]['resource_ref'],destinationRef=body.destination['resource_ref'],
                destinationName=body.destination['name'],parameters=body.parameters)
        except ValidationError as exc: raise HTTPException(422,'Invalid media operation') from exc
        return await prepare_human_media(request,prepared)

    @router.post('/media/prepare')
    async def prepare_human_media(request: Request, body: HumanMediaPrepareRequest):
        if not _media_available(request): raise HTTPException(503, 'Media jobs unavailable')
        account, owner = _media_human(request)
        sessions = getattr(request.app.state, 'session_manager', None)
        if sessions is None: raise HTTPException(503, 'Media jobs unavailable')
        try:
            session = await asyncio.to_thread(sessions.get_session, body.sessionID)
            if session.owner != owner or getattr(session, 'incognito', False): raise ValueError('session unavailable')
            workspace = str(getattr(session, 'workspace_id', None) or 'default')
            service = _media_service(request, workspace)
            return await service.prepare(account=account, owner=owner, workspace=workspace, generation=repository.generation(),
                session_id=body.sessionID, connection_id=body.connectionID, recipe_ref=body.recipeRef,
                destination_ref=body.destinationRef, destination_name=body.destinationName, parameters=body.parameters, output_profile=body.outputProfile)
        except Exception as exc:
            raise HTTPException(409, 'Media preparation unavailable or changed') from exc

    @router.post('/media/sprixen/prepare')
    async def prepare_sprixen_media(request: Request, body: SprixenMediaPrepareRequest):
        if not callable(getattr(request.app.state, 'sprixen_human_service_factory', None)):
            raise HTTPException(503, 'Sprixen jobs unavailable')
        account, owner = _media_human(request)
        sessions = getattr(request.app.state, 'session_manager', None)
        try:
            session=await asyncio.to_thread(sessions.get_session,body.sessionID)
            if session.owner != owner or getattr(session,'incognito',False): raise ValueError('session unavailable')
            workspace=str(getattr(session,'workspace_id',None) or 'default')
            return await _sprixen_service(request,workspace).prepare_sprite(account=account,owner=owner,workspace=workspace,
                generation=repository.generation(),session_id=body.sessionID,connection_id=body.connectionID,
                provider_account_id=body.providerAccountID,destination_ref=body.destinationRef,
                destination_name=body.destinationName,parameters=body.parameters)
        except Exception as exc:
            raise HTTPException(409,'Sprixen preparation unavailable or changed') from exc

    @router.post('/media/tripo/prepare')
    async def prepare_tripo_media(request: Request, body: TripoMediaPrepareRequest):
        if not callable(getattr(request.app.state, 'tripo_human_service_factory', None)):
            raise HTTPException(503,'Tripo jobs unavailable')
        account,owner=_media_human(request)
        sessions=getattr(request.app.state,'session_manager',None)
        try:
            session=await asyncio.to_thread(sessions.get_session,body.sessionID)
            if session.owner!=owner or getattr(session,'incognito',False): raise ValueError('session unavailable')
            workspace=str(getattr(session,'workspace_id',None) or 'default')
            return await _tripo_service(request,workspace).prepare_text3d(account=account,owner=owner,workspace=workspace,
                generation=repository.generation(),session_id=body.sessionID,connection_id=body.connectionID,
                provider_account_id=body.providerAccountID,destination_ref=body.destinationRef,
                destination_name=body.destinationName,parameters=body.parameters)
        except Exception as exc:
            raise HTTPException(409,'Tripo preparation unavailable or changed') from exc

    @router.post('/media/meshy/prepare')
    async def prepare_meshy_media(request: Request, body: MeshyMediaPrepareRequest):
        if not callable(getattr(request.app.state,'meshy_human_service_factory',None)):
            raise HTTPException(503,'Meshy jobs unavailable')
        account,owner=_media_human(request)
        sessions=getattr(request.app.state,'session_manager',None)
        try:
            session=await asyncio.to_thread(sessions.get_session,body.sessionID)
            if session.owner!=owner or getattr(session,'incognito',False): raise ValueError('session unavailable')
            workspace=str(getattr(session,'workspace_id',None) or 'default')
            return await _meshy_service(request,workspace).prepare_preview(account=account,owner=owner,workspace=workspace,
                generation=repository.generation(),session_id=body.sessionID,connection_id=body.connectionID,
                provider_account_id=body.providerAccountID,destination_ref=body.destinationRef,
                destination_name=body.destinationName,parameters=body.parameters)
        except Exception as exc:
            raise HTTPException(409,'Meshy preparation unavailable or changed') from exc

    @router.post('/media/meshy/refine/prepare')
    async def prepare_meshy_refine(request: Request, body: MeshyRefinePrepareRequest):
        if not callable(getattr(request.app.state,'meshy_human_service_factory',None)):
            raise HTTPException(503,'Meshy jobs unavailable')
        account,owner=_media_human(request)
        sessions=getattr(request.app.state,'session_manager',None)
        try:
            session=await asyncio.to_thread(sessions.get_session,body.sessionID)
            if session.owner!=owner or getattr(session,'incognito',False): raise ValueError('session unavailable')
            workspace=str(getattr(session,'workspace_id',None) or 'default')
            return await _meshy_service(request,workspace).prepare_refine(account=account,owner=owner,workspace=workspace,
                generation=repository.generation(),session_id=body.sessionID,source_job_id=body.sourceJobID,
                destination_ref=body.destinationRef,destination_name=body.destinationName,parameters=body.parameters)
        except Exception as exc:
            raise HTTPException(409,'Meshy refine preparation unavailable or changed') from exc

    @router.post('/media/hunyuan/prepare')
    async def prepare_hunyuan_media(request: Request):
        original=_material_original(request);account,owner=original[:2]
        body=await _material_body(request)
        keys={'sessionID','connectionID','sourceRef','destinationRef','destinationName','parameters'}
        if (type(body) is not dict or set(body)!=keys or any(type(body.get(k)) is not str or not body[k] or len(body[k])>16384 for k in keys-{'parameters'})):
            raise HTTPException(422,'Invalid Hunyuan request')
        from src.openclank.hunyuan_human import validate_request
        try: validate_request({'parameters':body['parameters']})
        except Exception as exc: raise HTTPException(422,'Invalid Hunyuan request') from exc
        if not _material_original_current(request,original): raise HTTPException(409,'Hunyuan authority changed')
        state=request.app.state;generation=repository.generation()
        session=await asyncio.to_thread(state.session_manager.get_session,body['sessionID'])
        if not session or session.owner!=owner or getattr(session,'incognito',False): raise HTTPException(409,'Hunyuan session unavailable')
        workspace=str(getattr(session,'workspace_id',None) or 'default')
        identity={'account_id':account,'session_id':body['sessionID'],'workspace_id':workspace,'policy_generation':generation}
        service,current=_hunyuan_human_service(request,workspace,account,owner,identity,original)
        try:
            await current()
            result=await service.prepare_hunyuan(account=account,owner=owner,workspace=workspace,generation=generation,
                session_id=body['sessionID'],connection_id=body['connectionID'],source_ref=body['sourceRef'],
                destination_ref=body['destinationRef'],destination_name=body['destinationName'],parameters=body['parameters'])
            await current();return result
        except Exception as exc: raise HTTPException(409,'Hunyuan preparation unavailable or changed') from exc

    @router.post('/media/meshy/rig/prepare')
    async def prepare_meshy_rig(request: Request):
        original=_material_original(request); account,owner=original[:2]
        body=await _material_body(request)
        keys={'sessionID','connectionID','providerAccountID','sourceRef','destinationRef','destinationName','parameters'}
        if (type(body) is not dict or set(body)!=keys or any(type(body.get(k)) is not str or not body[k] or len(body[k])>16384 for k in keys-{'parameters'})
                or type(body['parameters']) is not dict):
            raise HTTPException(422,'Invalid rig request')
        if not _material_original_current(request,original): raise HTTPException(409,'Rig authority changed')
        state=request.app.state; generation=repository.generation()
        session=await asyncio.to_thread(state.session_manager.get_session,body['sessionID'])
        if not session or session.owner!=owner or getattr(session,'incognito',False): raise HTTPException(409,'Rig session unavailable')
        workspace=str(getattr(session,'workspace_id',None) or 'default')
        identity={'account_id':account,'session_id':body['sessionID'],'workspace_id':workspace,'policy_generation':generation}
        service,before_dispatch=_meshy_rig_human_service(request,workspace,identity,original)
        try:
            await before_dispatch()
            result=await service.prepare_rig(account=account,owner=owner,workspace=workspace,generation=generation,
                session_id=body['sessionID'],connection_id=body['connectionID'],provider_account_id=body['providerAccountID'],
                source_ref=body['sourceRef'],destination_ref=body['destinationRef'],destination_name=body['destinationName'],parameters=body['parameters'])
            await before_dispatch(); return result
        except Exception as exc: raise HTTPException(409,'Rig preparation unavailable or changed') from exc

    @router.post('/media/comfy/material/prepare')
    async def prepare_comfy_material(request: Request):
        original=_material_original(request); account,owner=original[:2]
        body=await _material_body(request)
        keys={'sessionID','connectionID','recipeRef','destinationRef','destinationName','parameters'}
        if (type(body) is not dict or set(body)!=keys or any(type(body.get(k)) is not str or not body[k] or len(body[k])>16384 for k in keys-{'parameters'})
                or type(body['parameters']) is not dict):
            raise HTTPException(422,'Invalid material request')
        if not _material_original_current(request,original): raise HTTPException(409,'Material authority changed')
        state=request.app.state;generation=repository.generation()
        session=await asyncio.to_thread(state.session_manager.get_session,body['sessionID'])
        if not session or session.owner!=owner or getattr(session,'incognito',False): raise HTTPException(409,'Material session unavailable')
        workspace=str(getattr(session,'workspace_id',None) or 'default')
        identity={'account_id':account,'session_id':body['sessionID'],'workspace_id':workspace,'policy_generation':generation}
        service,before_dispatch=_comfy_material_human_service(request,workspace,account,owner,identity,original)
        try:
            await before_dispatch()
            result=await service.prepare_generated_material(account=account,owner=owner,workspace=workspace,generation=generation,
                session_id=body['sessionID'],connection_id=body['connectionID'],recipe_ref=body['recipeRef'],
                destination_ref=body['destinationRef'],destination_name=body['destinationName'],parameters=body['parameters'])
            await before_dispatch();return result
        except Exception as exc: raise HTTPException(409,'Material preparation unavailable or changed') from exc

    @router.post('/media/trellis/prepare')
    async def prepare_trellis_media(request: Request):
        original=_material_original(request);account,owner=original[:2]
        body=await _material_body(request)
        keys={'sessionID','connectionID','sourceRef','destinationRef','destinationName','parameters'}
        if (type(body) is not dict or set(body)!=keys or any(type(body.get(k)) is not str or not body[k] or len(body[k])>16384 for k in keys-{'parameters'})):
            raise HTTPException(422,'Invalid TRELLIS request')
        from src.openclank.trellis_human import validate_request
        try: validate_request({'parameters':body['parameters']})
        except Exception as exc: raise HTTPException(422,'Invalid TRELLIS request') from exc
        if not _material_original_current(request,original): raise HTTPException(409,'TRELLIS authority changed')
        state=request.app.state;generation=repository.generation()
        session=await asyncio.to_thread(state.session_manager.get_session,body['sessionID'])
        if not session or session.owner!=owner or getattr(session,'incognito',False): raise HTTPException(409,'TRELLIS session unavailable')
        workspace=str(getattr(session,'workspace_id',None) or 'default')
        identity={'account_id':account,'session_id':body['sessionID'],'workspace_id':workspace,'policy_generation':generation}
        service,current=_trellis_human_service(request,workspace,account,owner,identity,original)
        try:
            await current()
            result=await service.prepare_trellis(account=account,owner=owner,workspace=workspace,generation=generation,
                session_id=body['sessionID'],connection_id=body['connectionID'],source_ref=body['sourceRef'],
                destination_ref=body['destinationRef'],destination_name=body['destinationName'],parameters=body['parameters'])
            await current();return result
        except Exception as exc: raise HTTPException(409,'TRELLIS preparation unavailable or changed') from exc

    @router.post('/media/material/bake/prepare')
    async def prepare_material_bake(request: Request):
        account,owner,factory,credential=_material_original(request)
        body=await _material_body(request)
        keys={'sessionID','connectionID','sourceRef','destinationRef','destinationName','resolution'}
        if (type(body) is not dict or set(body)!=keys or any(type(body.get(k)) is not str or not body[k] or len(body[k])>16384 for k in keys-{'resolution'})
                or type(body.get('resolution')) is not int or body['resolution'] not in (256,512,1024,2048)):
            raise HTTPException(422,'Invalid material bake request')
        state=request.app.state; generation=repository.generation()
        if credential() is not True or _media_human(request)!=(account,owner): raise HTTPException(409,'Material authority changed')
        session=await asyncio.to_thread(state.session_manager.get_session,body['sessionID'])
        if not session or session.owner!=owner or getattr(session,'incognito',False): raise HTTPException(409,'Material session unavailable')
        workspace=str(getattr(session,'workspace_id',None) or 'default')
        from src.openclank.media_files import human_session_authority
        session_guard=human_session_authority(session_manager=state.session_manager,auth_manager=state.auth_manager)
        identity={'account_id':account,'session_id':body['sessionID'],'workspace_id':workspace,'policy_generation':generation}
        async def current(candidate):
            return bool(getattr(state,'capture_original_credential',None) is factory and credential() is True and _media_human(request)==(account,owner)
                and repository.generation()==generation and candidate.get('account_id')==account and candidate.get('session_id')==body['sessionID']
                and candidate.get('workspace_id')==workspace and await session_guard(candidate) and credential() is True)
        async def before_dispatch():
            if not await current(identity): raise ValueError('material authority changed')
        try:
            await before_dispatch()
            service=_blender_service_for_state(state,workspace,current,before_dispatch,material_bake=True)
            prepared=await service.prepare_material_bake(account=account,owner=owner,workspace=workspace,generation=generation,session_id=body['sessionID'],connection_id=body['connectionID'],source_ref=body['sourceRef'],destination_ref=body['destinationRef'],destination_name=body['destinationName'],resolution=body['resolution'])
            await before_dispatch();return prepared
        except Exception as exc: raise HTTPException(409,'Material bake unavailable or changed') from exc

    @router.post('/media/blender/prepare')
    async def prepare_blender_media(request: Request, body: BlenderMediaPrepareRequest):
        if not callable(getattr(request.app.state, 'blender_human_service_factory', None)):
            raise HTTPException(503, 'Blender jobs unavailable')
        account, owner = _media_human(request)
        sessions = getattr(request.app.state, 'session_manager', None)
        try:
            session = await asyncio.to_thread(sessions.get_session, body.sessionID)
            if session.owner != owner or getattr(session, 'incognito', False): raise ValueError('session unavailable')
            workspace = str(getattr(session, 'workspace_id', None) or 'default')
            return await _blender_service(request, workspace).prepare_blender(account=account, owner=owner, workspace=workspace,
                generation=repository.generation(), session_id=body.sessionID, connection_id=body.connectionID,
                source_ref=body.sourceRef, destination_ref=body.destinationRef, destination_name=body.destinationName)
        except Exception as exc:
            raise HTTPException(409, 'Blender preparation unavailable or changed; check the configured executable, version, Files access and local image dependencies.') from exc

    @router.post('/media/blender/four-headings/prepare')
    async def prepare_blender_four_headings(request: Request, body: BlenderMediaPrepareRequest):
        if not callable(getattr(request.app.state,'blender_human_service_factory',None)):
            raise HTTPException(503,'Blender jobs unavailable')
        account,owner=_media_human(request)
        try:
            session=await asyncio.to_thread(request.app.state.session_manager.get_session,body.sessionID)
            if session.owner!=owner or getattr(session,'incognito',False): raise ValueError('session unavailable')
            workspace=str(getattr(session,'workspace_id',None) or 'default')
            return await _blender_service(request,workspace,four_headings=True).prepare_blender(account=account,owner=owner,workspace=workspace,
                generation=repository.generation(),session_id=body.sessionID,connection_id=body.connectionID,
                source_ref=body.sourceRef,destination_ref=body.destinationRef,destination_name=body.destinationName,four_headings=True)
        except Exception as exc:
            raise HTTPException(409,'Blender preparation unavailable or changed') from exc

    @router.post('/media/blender/rig/inspect')
    async def inspect_blender_rig(request: Request, response: Response, body: BlenderRigInspectRequest):
        from src.openclank.blender_rig_inspect import inspect_rig
        response.headers['Cache-Control']='private, no-store'
        account,owner=_media_human(request)
        try:
            session=await asyncio.to_thread(request.app.state.session_manager.get_session,body.sessionID)
            if session.owner!=owner or getattr(session,'incognito',False): raise ValueError('session unavailable')
            workspace=str(getattr(session,'workspace_id',None) or 'default'); generation=repository.generation()
            context=ProviderContext(account,owner,generation,False,workspace)
            _,data=await facade(request,copal_workspace=workspace).capture_media_glb(context,resource_ref=body.sourceRef)
            result=await asyncio.to_thread(inspect_rig,data)
            current=await asyncio.to_thread(request.app.state.session_manager.get_session,body.sessionID)
            if _media_human(request)!=(account,owner) or current.owner!=owner or getattr(current,'incognito',False) or str(getattr(current,'workspace_id',None) or 'default')!=workspace or repository.generation()!=generation:
                raise ValueError('session changed')
            return {'sourceSHA256':result['sourceSHA256'],'joints':result['joints']}
        except Exception as exc:
            raise HTTPException(409,'Rig inspection unavailable') from exc

    @router.post('/media/blender/rig/prepare')
    async def prepare_blender_rig(request: Request, body: BlenderRigPrepareRequest):
        if not callable(getattr(request.app.state,'blender_human_service_factory',None)): raise HTTPException(503,'Blender jobs unavailable')
        account,owner=_media_human(request)
        try:
            session=await asyncio.to_thread(request.app.state.session_manager.get_session,body.sessionID)
            if session.owner!=owner or getattr(session,'incognito',False): raise ValueError('session unavailable')
            workspace=str(getattr(session,'workspace_id',None) or 'default')
            return await _blender_service(request,workspace,rig_clip=True).prepare_blender(account=account,owner=owner,workspace=workspace,
                generation=repository.generation(),session_id=body.sessionID,connection_id=body.connectionID,source_ref=body.sourceRef,
                destination_ref=body.destinationRef,destination_name=body.destinationName,rig_selection=body.selected)
        except Exception as exc:
            raise HTTPException(409,'Rig preparation unavailable or changed') from exc

    @router.get('/media/latex/readiness')
    async def latex_readiness(request: Request, response: Response):
        from src.openclank.latex_child import compiler_readiness
        _media_human(request);response.headers['Cache-Control']='private, no-store'
        compiler=await asyncio.to_thread(compiler_readiness)
        if not compiler: return {'available':False,'reason':'pdflatex_unavailable'}
        os.close(compiler['fd'])
        return {'available':True,'version':'unprobed'}

    @router.get('/media/recording/audio-readiness')
    async def recording_audio_readiness(request: Request, response: Response):
        from src.openclank.recording_child import audio_recording_readiness, crop_recording_readiness
        account,owner=_media_human(request);response.headers['Cache-Control']='private, no-store'
        auth=getattr(request.app.state,'auth_manager',None);token=request.cookies.get('open_clank_session')
        if not auth or not token or auth.get_username_for_token(token)!=owner or auth.account_id(owner)!=account: raise HTTPException(403,'Audio readiness unavailable')
        helper=await audio_recording_readiness()
        if helper: os.close(helper['fd'])
        crop_helper=await crop_recording_readiness()
        if crop_helper: os.close(crop_helper['fd'])
        if (_media_human(request)!=(account,owner) or request.app.state.auth_manager is not auth
            or request.cookies.get('open_clank_session')!=token or auth.get_username_for_token(token)!=owner or auth.account_id(owner)!=account): raise HTTPException(409,'Audio readiness changed')
        return {'available':bool(helper),'cropAvailable':bool(crop_helper)}

    @router.post('/workflow-result/resolve')
    async def resolve_workflow_result(request: Request, body: dict[str, Any]):
        """Resolve a selector through original receipt and current Human Files."""
        from src.openclank.workflow_result_resolver import resolve
        account,owner=_media_human(request)
        state=request.app.state; auth=getattr(state,'auth_manager',None); barrier=getattr(state,'account_request_barrier',None)
        manager=getattr(state,'session_manager',None); token=request.cookies.get('open_clank_session')
        if not auth or not barrier or not manager or not token: raise HTTPException(409,'Saved file unavailable')
        chat=body.get('chat') if isinstance(body,dict) else None
        if not isinstance(chat,str) or not chat or len(chat)>256: raise HTTPException(409,'Saved file unavailable')
        try: session=await asyncio.to_thread(manager.get_session,chat)
        except Exception: raise HTTPException(409,'Saved file unavailable')
        if session is None: raise HTTPException(409,'Saved file unavailable')
        def current_sync():
            try:
                from src.auth_helpers import require_user, is_internal_tool_identity
                key=barrier._owner(owner)
                return bool(str(require_user(request) or '').strip().lower()==owner and not is_internal_tool_identity(owner)
                    and not getattr(request.state,'api_token',False) and not getattr(request.state,'internal_tool_authenticated',False)
                    and state.auth_manager is auth and state.account_request_barrier is barrier
                    and request.cookies.get('open_clank_session')==token and auth.get_username_for_token(token)==owner
                    and auth.account_id(owner)==account and auth.username_for_account_id(account)==owner
                    and getattr(manager,'sessions',{}).get(chat) is session and session.owner==owner and not getattr(session,'incognito',False)
                    and not getattr(session,'archived',False) and not barrier._fenced(key)
                    and not (barrier._absolute_fence and barrier._absolute_fence(key)))
            except Exception: return False
        async def current(): return current_sync()
        def durable_chat_current():
            # Archive/deletion and the durable row are authoritative. Do not
            # hydrate/touch the session repeatedly on the event-loop thread.
            from core.database import Session as DbSession
            from src.openclank.conversation_archive import get_conversation_archive
            db=session_factory()
            try:
                row=db.query(DbSession.owner,DbSession.archived).filter(DbSession.id==chat).first()
                return bool(row and row.owner==owner and not row.archived
                    and not get_conversation_archive().chat_erasure_started(owner=owner,chat_id=chat))
            finally: db.close()
        async with barrier.admitted(owner):
            if not current_sync(): raise HTTPException(409,'Saved file unavailable')
            found=await resolve(selector=body,account=account,owner=owner,repository=repository,facade=facade(request),auth=auth,session=session,current=current)
            if not found or not current_sync(): raise HTTPException(409,'Saved file unavailable')
            try: durable=await asyncio.to_thread(durable_chat_current)
            except Exception: durable=False
            if not durable or not current_sync(): raise HTTPException(409,'Saved file unavailable')
            return found

    @router.post('/media/recording/audio-select')
    async def select_recording_audio(request: Request, body: RecordingAudioSelectRequest):
        from src import bg_jobs
        account,owner=_media_human(request);job=bg_jobs.get_media(body.jobID,account_id=account)
        if not job or job.get('owner')!=owner or (job.get('identity') or {}).get('adapter_id')!='screen-record-v1': raise HTTPException(404,'Recording unavailable')
        state=request.app.state;auth=getattr(state,'auth_manager',None);barrier=getattr(state,'account_request_barrier',None);token=request.cookies.get('open_clank_session')
        if not auth or not barrier or not token or auth.get_username_for_token(token)!=owner: raise HTTPException(403,'Audio selection unavailable')
        async def request_current():
            try:
                key=str(owner).strip().lower()
                return bool(_media_human(request)==(account,owner) and state.auth_manager is auth and state.account_request_barrier is barrier
                    and request.cookies.get('open_clank_session')==token and auth.get_username_for_token(token)==owner
                    and auth.account_id(owner)==account and auth.username_for_account_id(account)==owner
                    and not barrier._fenced(key) and not (barrier._absolute_fence and barrier._absolute_fence(key)))
            except Exception:return False
        try:
            return await _recording_service(request,job['workspace_id']).select_audio(account=account,owner=owner,job_id=body.jobID,
                revision=body.revision,selection_id=body.selectionID,generation=body.generation,microphone=body.microphone,system_output=body.systemOutput,request_current=request_current,crop=body.crop.model_dump() if body.crop else None)
        except Exception as exc: raise HTTPException(409,'Audio selection unavailable or changed; check original status.') from exc

    @router.post('/media/recording/prepare')
    async def prepare_recording(request: Request, body: RecordingPrepareRequest):
        if not _media_available(request): raise HTTPException(503,'Recording unavailable')
        account,owner=_media_human(request)
        wants_audio=bool(body.cropIntent or (body.audioIntent and (body.audioIntent.microphone or body.audioIntent.systemOutput)))
        guarded=wants_audio or body.controlSession
        auth=getattr(request.app.state,'auth_manager',None);token=request.cookies.get('open_clank_session') if guarded else None
        if guarded and (not auth or not token or auth.get_username_for_token(token)!=owner): raise HTTPException(403,'Recording unavailable')
        def request_current():
            return bool(_media_human(request)==(account,owner) and request.app.state.auth_manager is auth and request.cookies.get('open_clank_session')==token and auth.get_username_for_token(token)==owner and auth.account_id(owner)==account)
        try:
            session=await asyncio.to_thread(request.app.state.session_manager.get_session,body.sessionID)
            if session.owner!=owner or getattr(session,'incognito',False): raise ValueError('session unavailable')
            workspace=str(getattr(session,'workspace_id',None) or 'default')
            if guarded and not request_current(): raise ValueError('recording authority changed')
            result=await _recording_service(request,workspace).prepare_recording(account=account,owner=owner,workspace=workspace,
                generation=repository.generation(),session_id=body.sessionID,destination_ref=body.destinationRef,destination_name=body.destinationName,
                audio_intent=({'microphone':body.audioIntent.microphone,'system_output':body.audioIntent.systemOutput} if body.audioIntent else None),crop_intent=body.cropIntent,control_session=body.controlSession)
            if guarded and not request_current(): raise ValueError('recording authority changed')
            return result
        except Exception as exc: raise HTTPException(409,'Recording preparation unavailable or changed') from exc

    @router.post('/media/recording/open')
    async def open_recording_result(request: Request, body: HumanMediaJobRequest, response: Response):
        from src import bg_jobs
        from src.openclank.recording_bundle import fresh_recording_result
        response.headers['Cache-Control'] = 'private, no-store'
        account, owner = _media_human(request)
        state = request.app.state; auth = getattr(state, 'auth_manager', None); barrier = getattr(state, 'account_request_barrier', None)
        token = request.cookies.get('open_clank_session')
        if not auth or not barrier or not token or auth.get_username_for_token(token) != owner:
            raise HTTPException(403, 'Recording result unavailable')
        try:
            job = await asyncio.wait_for(asyncio.to_thread(bg_jobs.get_media, body.jobID, account_id=account), 5)
            if not job or job.get('owner') != owner: raise ValueError('recording unavailable')
            workspace = job['identity']['workspace_id']; generation = repository.generation()
            ctx = ProviderContext(account, owner, generation, files_principal(request, repository=repository)[2], workspace)
            def check():
                try:
                    key = str(owner).strip().lower()
                    return bool(_media_human(request) == (account,owner) and state.auth_manager is auth
                        and state.account_request_barrier is barrier and request.cookies.get('open_clank_session') == token
                        and auth.get_username_for_token(token) == owner and auth.account_id(owner) == account
                        and auth.username_for_account_id(account) == owner and repository.generation() == generation
                        and files_principal(request,repository=repository)[2] == ctx.is_admin and not barrier._fenced(key)
                        and not (barrier._absolute_fence and barrier._absolute_fence(key)))
                except Exception: return False
            async def current():
                try: return await asyncio.wait_for(asyncio.to_thread(check), 5) is True
                except Exception: return False
            async with barrier.admitted(owner):
                result = await fresh_recording_result(facade=facade(request,copal_workspace=workspace), context=ctx,
                                                      job_id=body.jobID, current=current)
                if await current() is not True: raise ValueError('authority changed')
                return result
        except Exception as exc:
            raise HTTPException(409, 'Original recording result unavailable; choose the file in Files.') from exc

    @router.post('/media/recording/stop')
    async def stop_recording(request: Request, body: RecordingStopRequest):
        from src import bg_jobs
        account,owner=_media_human(request); job=bg_jobs.get_media(body.jobID,account_id=account)
        if not job or job.get('owner')!=owner or (job.get('identity') or {}).get('adapter_id')!='screen-record-v1': raise HTTPException(404,'Recording unavailable')
        try:
            return await _recording_service(request,job['workspace_id']).stop_recording(account=account,owner=owner,
                job_id=body.jobID,revision=body.revision,stop_id=body.stopID)
        except Exception as exc: raise HTTPException(409,'Recording stop unavailable or changed') from exc

    @router.post('/media/latex/prepare')
    async def prepare_latex(request: Request, body: LatexPrepareRequest):
        if not _media_available(request): raise HTTPException(503,'LaTeX jobs unavailable')
        account,owner=_media_human(request)
        try:
            session=await asyncio.to_thread(request.app.state.session_manager.get_session,body.sessionID)
            if session.owner!=owner or getattr(session,'incognito',False): raise ValueError('session unavailable')
            workspace=str(getattr(session,'workspace_id',None) or 'default')
            return await _latex_service(request,workspace).prepare_latex(account=account,owner=owner,workspace=workspace,
                generation=repository.generation(),session_id=body.sessionID,source=body.source.model_dump(),
                dependencies=[item.model_dump() for item in body.dependencies],destination_ref=body.destinationRef,destination_name=body.destinationName)
        except Exception as exc: raise HTTPException(409,'LaTeX preparation unavailable or changed') from exc

    @router.post('/media/sprite-sheet/prepare')
    async def prepare_sprite_sheet(request: Request, body: SpriteSheetPrepareRequest):
        account,owner=_media_human(request)
        try:
            session=await asyncio.to_thread(request.app.state.session_manager.get_session,body.sessionID)
            if session.owner!=owner or getattr(session,'incognito',False): raise ValueError('session unavailable')
            workspace=str(getattr(session,'workspace_id',None) or 'default')
            return await _sprite_sheet_service(request,workspace).prepare_sheet(account=account,owner=owner,workspace=workspace,
                generation=repository.generation(),session_id=body.sessionID,source_refs=body.sourceRefs,destination_ref=body.destinationRef,
                destination_name=body.destinationName,parameters=body.parameters)
        except Exception as exc: raise HTTPException(409,'Sprite sheet preparation unavailable or changed') from exc

    @router.post('/media/sprite-sheet/crops/prepare')
    async def prepare_sprite_crops(request: Request, body: SpriteSheetCropsPrepareRequest):
        account,owner=_media_human(request)
        try:
            session=await asyncio.to_thread(request.app.state.session_manager.get_session,body.sessionID)
            if session.owner!=owner or getattr(session,'incognito',False):raise ValueError('session unavailable')
            workspace=str(getattr(session,'workspace_id',None) or 'default')
            return await _sprite_sheet_service(request,workspace).prepare_crops(account=account,owner=owner,workspace=workspace,
                generation=repository.generation(),session_id=body.sessionID,source_ref=body.sourceRef,destination_ref=body.destinationRef,
                destination_name=body.destinationName,parameters=body.parameters,rectangles=body.rectangles)
        except Exception as exc:raise HTTPException(409,'Sprite sheet preparation unavailable or changed') from exc

    @router.post('/media/sprite-sheet/project')
    async def bind_sprite_sheet_project(request: Request):
        from src import bg_jobs
        account,owner=_media_human(request)
        state=request.app.state;auth=getattr(state,'auth_manager',None);barrier=getattr(state,'account_request_barrier',None)
        capture=getattr(state,'capture_original_credential',None);supervisor=getattr(state,'history_supervisor',None)
        if not auth or not barrier or not supervisor or not callable(capture): raise HTTPException(503,'Image project recovery unavailable')
        credential=capture(request,owner,account)
        if not callable(credential) or credential() is not True: raise HTTPException(403,'Session unavailable')
        async with barrier.admitted(owner):
            body=await _shared_text_request_body(request,HumanMediaJobRequest,maximum=4096)
            if (getattr(state,'auth_manager',None) is not auth or getattr(state,'account_request_barrier',None) is not barrier
                    or getattr(state,'capture_original_credential',None) is not capture or getattr(state,'history_supervisor',None) is not supervisor
                    or barrier._fenced(owner) or auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner
                    or credential() is not True): raise HTTPException(403,'Session unavailable')
            job=bg_jobs.get_media(body.jobID,account_id=account)
            if not job or job.get('owner')!=owner: raise HTTPException(404,'Media job unavailable')
            identity=job.get('identity') or {};session_manager=getattr(state,'session_manager',None)
            session=session_manager.get_session(identity.get('session_id')) if session_manager else None
            def current():
                try:
                    latest=bg_jobs.get_media(body.jobID,account_id=account)
                    return (getattr(state,'auth_manager',None) is auth and getattr(state,'account_request_barrier',None) is barrier
                        and getattr(state,'capture_original_credential',None) is capture and getattr(state,'history_supervisor',None) is supervisor
                        and getattr(state,'session_manager',None) is session_manager and not barrier._fenced(owner)
                        and auth.account_id(owner)==account and auth.username_for_account_id(account)==owner and credential() is True
                        and latest is not None and latest.get('owner')==owner and latest.get('identity')==identity
                        and latest.get('status')=='complete' and not latest.get('publication_withdrawn')
                        and latest.get('result')==job.get('result') and latest.get('artifact')==job.get('artifact')
                        and session is not None and session_manager.get_session(identity.get('session_id')) is session
                        and session.owner==owner and getattr(session,'workspace_id',None)==identity.get('workspace_id')
                        and repository.generation()==identity.get('policy_generation')
                        and not getattr(session,'archived',False) and not getattr(session,'incognito',False))
                except Exception:return False
            if not current(): raise HTTPException(409,'Sprite sheet authority changed')
            try: result = await _sprite_sheet_service(request,job['workspace_id'],project_authority=(owner,account,current)).bind_sheet_project(account=account,owner=owner,job_id=body.jobID)
            except Exception as exc: raise HTTPException(503,{'code':'project_binding_unknown','operation_id':'sprite-sheet:'+body.jobID,'message':'PNG remains committed; image project binding outcome may require reconciliation'}) from exc
            if not current(): raise HTTPException(503,{'code':'project_binding_unknown','operation_id':'sprite-sheet:'+body.jobID,'message':'PNG remains committed; image project binding outcome may require reconciliation'})
            return result

    @router.post('/media/run')
    async def run_human_media(request: Request):
        original=_material_original(request)
        try: body=HumanMediaRunRequest.model_validate(await _material_body(request))
        except HTTPException: raise
        except Exception as exc: raise HTTPException(422,'Invalid media request') from exc
        if not _material_original_current(request,original): raise HTTPException(409,'Media authority changed')
        if not _media_available(request): raise HTTPException(503, 'Media jobs unavailable')
        from src import bg_jobs
        account, owner = original[:2]
        job = bg_jobs.get_media(body.jobID, account_id=account)
        if not job or job.get('owner') != owner: raise HTTPException(404, 'Media job unavailable')
        if (job.get('identity') or {}).get('adapter_id') == 'sprixen-animation-v1':
            raise HTTPException(409, 'Project animation submission is unavailable; check the original job status')
        audio_v6=(job.get('identity',{}).get('resource_binding') or {}).get('recording_protocol') in ('record-v6','record-v7')
        auth=getattr(request.app.state,'auth_manager',None);barrier=getattr(request.app.state,'account_request_barrier',None)
        token=request.cookies.get('open_clank_session') if audio_v6 else None; generation=repository.generation()
        async def before_spawn():
            try:
                key=str(owner).strip().lower()
                return bool(auth and barrier and token and _media_human(request)==(account,owner) and request.app.state.auth_manager is auth
                    and request.app.state.account_request_barrier is barrier and request.cookies.get('open_clank_session')==token
                    and auth.get_username_for_token(token)==owner and auth.account_id(owner)==account and auth.username_for_account_id(account)==owner
                    and repository.generation()==generation and not barrier._fenced(key) and not (barrier._absolute_fence and barrier._absolute_fence(key)))
            except Exception:return False
        if audio_v6 and await before_spawn() is not True: raise HTTPException(403,'Recording unavailable')
        try:
            service=_recording_service(request,job['workspace_id'],before_spawn=before_spawn) if audio_v6 else _service_for_job(request,job,original)
            result=await service.run(account=account, owner=owner,job_id=body.jobID, revision=body.revision, parameters=body.parameters)
            if audio_v6 and await before_spawn() is not True: raise HTTPException(409,'Recording request authority changed; check original status.')
        except Exception as exc:
            _media_failure(exc)
        if not _material_original_current(request,original): raise HTTPException(503,'Media outcome unavailable; check original status')
        return result

    @router.post('/media/recording/partial-preview')
    async def preview_recording_partial(request: Request, body: HumanMediaRecoveryPreviewRequest):
        from src import bg_jobs
        account,owner=_media_human(request); job=bg_jobs.get_media(body.jobID,account_id=account)
        if not job or job.get('owner')!=owner or (job.get('identity') or {}).get('adapter_id')!='screen-record-v1': raise HTTPException(404,'Recording unavailable')
        try: return await _recording_service(request,job['workspace_id']).partial_preview(account=account,owner=owner,job_id=body.jobID,destination_ref=body.destinationRef,destination_name=body.destinationName)
        except Exception as exc: raise HTTPException(409,'Partial recording recovery unavailable') from exc

    @router.post('/media/recording/partial-save')
    async def save_recording_partial(request: Request, body: HumanMediaRecoverySaveRequest):
        from src import bg_jobs
        account,owner=_media_human(request); job=bg_jobs.get_media(body.jobID,account_id=account)
        if not job or job.get('owner')!=owner or (job.get('identity') or {}).get('adapter_id')!='screen-record-v1': raise HTTPException(404,'Recording unavailable')
        try: return await _recording_service(request,job['workspace_id']).partial_save(account=account,owner=owner,job_id=body.jobID,revision=body.revision,recovery_id=body.recoveryID)
        except Exception as exc: raise HTTPException(409,'Partial recording recovery unavailable or unknown') from exc

    @router.post('/media/recovery/preview')
    async def preview_media_recovery(request: Request, body: HumanMediaRecoveryPreviewRequest):
        if not _media_available(request): raise HTTPException(503, 'Media jobs unavailable')
        from src import bg_jobs
        account, owner = _media_human(request)
        job = bg_jobs.get_media(body.jobID, account_id=account)
        if not job or job.get('owner') != owner: raise HTTPException(404, 'Media job unavailable')
        try:
            return await _service_for_job(request, job).recovery_preview(account=account, owner=owner,
                job_id=body.jobID, destination_ref=body.destinationRef, destination_name=body.destinationName)
        except Exception as exc:
            _media_failure(exc)

    @router.post('/media/recovery/save')
    async def save_media_recovery(request: Request, body: HumanMediaRecoverySaveRequest):
        if not _media_available(request): raise HTTPException(503, 'Media jobs unavailable')
        from src import bg_jobs
        account, owner = _media_human(request)
        job = bg_jobs.get_media(body.jobID, account_id=account)
        if not job or job.get('owner') != owner: raise HTTPException(404, 'Media job unavailable')
        try:
            return await _service_for_job(request, job).recovery_save(account=account, owner=owner,
                job_id=body.jobID, revision=body.revision, recovery_id=body.recoveryID)
        except Exception as exc:
            _media_failure(exc)

    @router.post('/media/{action}')
    async def observe_human_media(request: Request, action: str, response: Response):
        original=_material_original(request)
        try: body=HumanMediaJobRequest.model_validate(await _material_body(request))
        except HTTPException: raise
        except Exception as exc: raise HTTPException(422,'Invalid media request') from exc
        if not _material_original_current(request,original): raise HTTPException(409,'Media authority changed')
        response.headers['Cache-Control'] = 'private, no-store'
        from src import bg_jobs
        account, owner = original[:2]
        job = bg_jobs.get_media(body.jobID, account_id=account)
        if not job or job.get('owner') != owner: raise HTTPException(404, 'Media job unavailable')
        audio_v6=(job.get('identity',{}).get('resource_binding') or {}).get('recording_protocol') in ('record-v6','record-v7')
        if audio_v6 and action == 'status':
            from src.openclank.media_human import HumanMediaService
            state=request.app.state; auth=getattr(state,'auth_manager',None); barrier=getattr(state,'account_request_barrier',None)
            token=request.cookies.get('open_clank_session')
            def current():
                try:
                    key=str(owner).strip().lower()
                    return bool(auth and barrier and token and _media_human(request)==(account,owner)
                        and state.auth_manager is auth and state.account_request_barrier is barrier
                        and request.cookies.get('open_clank_session')==token and auth.get_username_for_token(token)==owner
                        and auth.account_id(owner)==account and auth.username_for_account_id(account)==owner
                        and not barrier._fenced(key) and not (barrier._absolute_fence and barrier._absolute_fence(key)))
                except Exception:return False
            if not current(): raise HTTPException(403,'Media status unavailable')
            async with barrier.admitted(owner):
                row=await asyncio.to_thread(bg_jobs.get_media,body.jobID,account_id=account)
                if (not current() or not row or row.get('owner')!=owner or row.get('account_id')!=account
                    or (row.get('identity',{}).get('resource_binding') or {}).get('recording_protocol') not in ('record-v6','record-v7')):
                    raise HTTPException(409,'Media status unavailable')
                result=HumanMediaService.project(row)
                result.pop('result',None)
                result.update(cancelAvailable=False,stopAvailable=False,partialAvailable=False,partialRecovery=None)
                if _media_available(request):
                    try:
                        live=await asyncio.wait_for(_service_for_job(request,row,original).action(account=account,owner=owner,job_id=body.jobID,action='status'),5)
                        if isinstance(live,dict): result=live
                    except Exception:
                        pass
                if not current() or not _material_original_current(request,original): raise HTTPException(409,'Media status unavailable')
                return result
        if not _media_available(request): raise HTTPException(503, 'Media jobs unavailable')
        auth=getattr(request.app.state,'auth_manager',None);token=request.cookies.get('open_clank_session') if audio_v6 else None
        if audio_v6 and (not auth or not token or auth.get_username_for_token(token)!=owner): raise HTTPException(403,'Media status unavailable')
        try:
            result=await _service_for_job(request, job,original).action(account=account, owner=owner, job_id=body.jobID, action=action)
            if audio_v6 and (_media_human(request)!=(account,owner) or request.app.state.auth_manager is not auth or request.cookies.get('open_clank_session')!=token or auth.get_username_for_token(token)!=owner or auth.account_id(owner)!=account): raise HTTPException(409,'Media authority changed')
        except Exception as exc:
            _media_failure(exc)
        if not _material_original_current(request,original): raise HTTPException(503,'Media outcome unavailable; check original status')
        return result

    def _raise(error: FilesFacadeError) -> None:
        if error.code in {"resource_unavailable", "invalid_resource_ref"}:
            status = 404
        elif error.code in {"resource_ref_stale", "stale_cursor", "resource_changed", "policy_generation_changed", "idempotency_conflict", "operation_pending",
                            "managed_undo_journal_missing", "managed_undo_of_undo_unsupported", "managed_undo_move_not_complete",
                            "managed_undo_requires_cross_root", "managed_undo_journal_unreadable", "managed_undo_unavailable_platform", "managed_undo_unavailable"}:
            status = 409
        elif error.code == "invalid_range":
            status = 416
        elif error.code in {"provider_unavailable", "mutation_outcome_unknown"}:
            status = 503
        else:
            status = 400
        # The safe code is stable; raw provider errors/origin IDs are not.
        detail = {"code": error.code, "message": str(error)}
        if error.code == 'mutation_outcome_unknown':
            detail.update(uncertain=True, retryable=False)
        raise HTTPException(status, detail=detail) from error

    def _generation_guard(expected: int) -> Callable[[], bool]:
        """Throttled fail-closed policy-generation probe for in-flight streams.

        A scoped reset bumps the policy generation; streams that resolved their
        ref before the reset must not keep flowing afterwards. The probe is
        time-throttled so a large transfer costs at most a handful of SQLite
        reads, and any store error is treated as a change.
        """
        state = {"next_probe": 0.0}

        def intact() -> bool:
            now = time.monotonic()
            if now < state["next_probe"]:
                return True
            state["next_probe"] = now + 0.2
            try:
                return repository.generation() == expected
            except Exception:
                return False

        return intact

    def _policy_aborted() -> FilesFacadeError:
        return FilesFacadeError(
            "file policy changed during transfer; retry with a fresh reference",
            code="policy_generation_changed",
        )

    @router.get("/roots")
    async def roots(
        request: Request,
        copal_workspace: str = Query(default="default", min_length=1, max_length=64),
    ):
        try:
            return await facade(request, copal_workspace=copal_workspace).roots(context(request))
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/children")
    async def children(body: ChildrenRequest, request: Request):
        try:
            return await facade(request).children(
                context_for_ref(request, body.parent_ref),
                parent_ref=body.parent_ref,
                cursor=body.cursor,
                limit=body.limit,
                sort=body.sort,
                query=body.query,
            )
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/stat")
    async def stat(body: StatRequest, request: Request):
        try:
            return await facade(request).stat(context_for_ref(request, body.resource_ref), resource_ref=body.resource_ref, purpose=body.purpose)
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/inspect/safetensors")
    async def inspect_safetensors(body: InspectFormatRequest, request: Request):
        try:
            ctx = context_for_ref(request, body.resource_ref)
            result = await facade(request).inspect_safetensors(ctx, resource_ref=body.resource_ref, expected_revision=body.expected_revision)
            if not _generation_guard(ctx.policy_generation)():
                raise FilesFacadeError("File policy changed", code="policy_generation_changed")
            return result
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/checkpoint/capabilities")
    async def checkpoint_capabilities(body: CheckpointCapabilityRequest, request: Request, response: Response):
        """Explicit preview prepares existing Files credentials and checks both owners."""
        response.headers["Cache-Control"]="private, no-store"
        account,owner=_media_human(request);state=request.app.state
        auth=getattr(state,"auth_manager",None);barrier=getattr(state,"account_request_barrier",None);supervisor=getattr(state,"history_supervisor",None)
        ctx=context_for_ref(request,body.root_ref)
        if not auth or not barrier or not supervisor or ctx.owner_subject_id!=account or ctx.owner_username!=owner:return {"supported":False}
        from src.openclank.files_service_client import _checkpoint_owned_thread
        try:
            async with barrier.admitted(owner):
                service=facade(request,copal_workspace=ctx.workspace_id);provider,ref=service._provider_for_ref(ctx,body.root_ref,capability="children")
                if provider.name!="host" or ref.kind not in {"folder","directory"}:return {"supported":False}
                root=dict(await provider.authorize_checkpoint_root(ctx,origin_id=ref.origin_id,expected_revision=body.root_revision));process=supervisor.process
                def valid():return bool(_media_human(request)==(account,owner) and auth.account_id(owner)==account and auth.username_for_account_id(account)==owner
                    and repository.generation()==ctx.policy_generation and process is not None and supervisor.process is process and process.poll() is None)
                if not valid() or not await _checkpoint_owned_thread(lambda:supervisor.checkpoint_capability(root["history_actor"],account,root["root_id"])):return {"supported":False}
                # This explicitly prepares the already-approved Files-only role;
                # it does not issue a registration or checkpoint.
                client=client_for_owner(owner,app_scope=dict(root["scope"]),transport="framed")
                req=client._request_envelope("capabilities",path=root["root"],payload={})
                result=await _checkpoint_owned_thread(lambda:client._request_sync(req))
                operations=result.get("operations")
                if (not isinstance(operations,list) or len(operations)>256 or any(not isinstance(name,str) or not name or len(name)>128 for name in operations)
                    or len(set(operations))!=len(operations)):return {"supported":False}
                if not valid() or dict(await provider.authorize_checkpoint_root(ctx,origin_id=ref.origin_id,expected_revision=body.root_revision))!=root:return {"supported":False}
                return {"supported":all(name in operations for name in ("register_checkpoint_file","checkpoint_prepare","checkpoint_finalize"))}
        except Exception:return {"supported":False}

    async def _checkpoint_restore_authority(request: Request):
        account, owner = _media_human(request)
        auth = getattr(request.app.state, 'auth_manager', None)
        barrier = getattr(request.app.state, 'account_request_barrier', None)
        supervisor = getattr(request.app.state, 'history_supervisor', None)
        if auth is None or barrier is None or supervisor is None:
            raise HTTPException(503, 'Checkpoint restore unavailable')
        principal = files_principal(request, repository=repository)
        if principal[0] != account or principal[1] != owner:
            raise HTTPException(403, 'Checkpoint restore unavailable')
        ctx = ProviderContext(account, owner, repository.generation(), bool(principal[2]), None)
        async def current():
            try:
                return (_media_human(request) == (account, owner) and request.app.state.auth_manager is auth
                    and request.app.state.account_request_barrier is barrier and request.app.state.history_supervisor is supervisor
                    and auth.account_id(owner) == account and auth.username_for_account_id(account) == owner
                    and files_principal(request, repository=repository)[2] == ctx.is_admin
                    and repository.generation() == ctx.policy_generation)
            except Exception:
                return False
        return account, owner, auth, barrier, supervisor, ctx, current

    @router.post('/checkpoint/restore-preview')
    async def checkpoint_restore_preview(body: CheckpointRestorePreviewRequest, request: Request, response: Response):
        response.headers['Cache-Control'] = 'private, no-store'
        from src.openclank import checkpoint_restore
        try:
            account, owner, auth, barrier, supervisor, ctx, current = await _checkpoint_restore_authority(request)
            return await checkpoint_restore.preview(facade(request),ctx,checkpoint_id=body.checkpoint_id,
                resource_id=body.resource_id,auth=auth,barrier=barrier,supervisor=supervisor,current=current)
        except HTTPException: raise
        except Exception as exc: raise HTTPException(409,'Checkpoint restore unavailable or stale') from exc

    @router.post('/checkpoint/restore-confirm')
    async def checkpoint_restore_confirm(body: CheckpointRestoreConfirmRequest, request: Request, response: Response):
        response.headers['Cache-Control'] = 'private, no-store'
        from src.openclank import checkpoint_restore
        try:
            account, owner, auth, barrier, supervisor, ctx, current = await _checkpoint_restore_authority(request)
            return await checkpoint_restore.confirm(facade(request),ctx,checkpoint_id=body.checkpoint_id,
                resource_id=body.resource_id,request_id=body.request_id,expected_digest=body.expected_digest,
                auth=auth,barrier=barrier,supervisor=supervisor,current=current,
                original_current=lambda: (getattr(request.app.state,'auth_manager',None) is auth
                    and getattr(request.app.state,'account_request_barrier',None) is barrier
                    and getattr(request.app.state,'history_supervisor',None) is supervisor))
        except HTTPException: raise
        except Exception as exc: raise HTTPException(409,'Checkpoint restore unavailable or stale') from exc

    @router.post('/checkpoint/restore-status')
    async def checkpoint_restore_status(body: CheckpointRestoreStatusRequest, request: Request, response: Response):
        response.headers['Cache-Control'] = 'private, no-store'
        from src.openclank import checkpoint_restore
        account, owner = _media_human(request)
        state = request.app.state
        auth = getattr(state, 'auth_manager', None); barrier = getattr(state, 'account_request_barrier', None)
        supervisor = getattr(state, 'history_supervisor', None)
        if auth is None or barrier is None or supervisor is None:
            raise HTTPException(503, 'Checkpoint restore unavailable')
        try:
            return await checkpoint_restore.status(repository,owner=owner,account=account,auth=auth,barrier=barrier,
                supervisor=supervisor,request_id=body.request_id,expected_digest=body.expected_digest,
                original_current=lambda: (getattr(state,'auth_manager',None) is auth
                    and getattr(state,'account_request_barrier',None) is barrier
                    and getattr(state,'history_supervisor',None) is supervisor))
        except Exception as exc: raise HTTPException(503,'Checkpoint restore unavailable') from exc

    @router.post("/checkpoint/compare")
    async def checkpoint_compare(body: CheckpointCompareRequest, request: Request, response: Response):
        response.headers["Cache-Control"]="private, no-store"
        account,owner=_media_human(request);state=request.app.state
        auth=getattr(state,"auth_manager",None);barrier=getattr(state,"account_request_barrier",None);supervisor=getattr(state,"history_supervisor",None)
        ctx=context_for_ref(request,body.root_ref)
        if ctx.owner_subject_id!=account or ctx.owner_username!=owner or not auth or not barrier or not supervisor:raise HTTPException(503,"Checkpoint comparison unavailable")
        async def current():
            try:return (_media_human(request)==(account,owner) and auth.account_id(owner)==account and auth.username_for_account_id(account)==owner and repository.generation()==ctx.policy_generation)
            except Exception:return False
        try:
            from src.openclank.checkpoint_comparison import compare_checkpoint_file
            async with barrier.admitted(owner):
                if not await current():raise ValueError("authority changed")
                source=await facade(request,copal_workspace=ctx.workspace_id).checkpoint_comparison_source(ctx,root_ref=body.root_ref,
                    original_root_revision=body.original_root_revision,items=[item.model_dump() for item in body.items],request_id=body.request_id,
                    resource_id=body.resource_id,auth=auth,barrier=barrier,history_supervisor=supervisor,current=current)
                result=await compare_checkpoint_file(source)
                if not await current():raise ValueError("authority changed")
                return result
        except Exception:raise HTTPException(503,"Checkpoint comparison unavailable")

    async def _selected_output_run(request, *, items=None, review=None, token=None, first_ref=None):
        from src.openclank.selected_output_export import preview, export
        account,owner=_media_human(request)
        state=request.app.state; auth=getattr(state,'auth_manager',None); barrier=getattr(state,'account_request_barrier',None)
        original=_material_original(request)
        if not auth or not barrier: raise HTTPException(503,'Selected export unavailable')
        # All selected refs share the context bound in the encrypted reviewed plan.
        ctx=context_for_ref(request,items[0]['ref'] if items else first_ref)
        async def current():
            try:
                return (_material_original_current(request,original) and state.auth_manager is auth and state.account_request_barrier is barrier
                    and auth.account_id(owner)==account and auth.username_for_account_id(account)==owner
                    and context(request).is_admin == ctx.is_admin and repository.generation()==ctx.policy_generation)
            except Exception:return False
        try:
            async with barrier.admitted(owner):
                if not await current(): raise ValueError('authority changed')
                files=facade(request,copal_workspace=ctx.workspace_id)
                async with asyncio.timeout(30):
                    if token is None:
                        return await preview(files,ctx,items,review.omissions,review.license,review.attribution,review.privacy,current)
                    return await export(files,ctx,token,current,first_ref=first_ref)
        except Exception: raise HTTPException(503,'Selected export unavailable or changed')

    @router.post('/selected-output/preview')
    async def selected_output_preview(body: SelectedOutputPreview, request: Request, response: Response):
        response.headers['Cache-Control']='private, no-store'
        return await _selected_output_run(request,items=[item.model_dump() for item in body.items],review=body)

    @router.post('/selected-output/export')
    async def selected_output_export(body: SelectedOutputExport, request: Request):
        data=await _selected_output_run(request,token=body.previewToken,first_ref=body.firstRef)
        return Response(data,media_type='application/zip',headers={'Content-Disposition':'attachment; filename="selected-outputs.zip"','Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'})

    @router.post("/checkpoint/capture")
    async def checkpoint_capture(body: CheckpointRegistrationRequest, request: Request, response: Response):
        response.headers["Cache-Control"]="private, no-store"
        account,owner=_media_human(request)
        state=request.app.state;auth=getattr(state,"auth_manager",None);barrier=getattr(state,"account_request_barrier",None);supervisor=getattr(state,"history_supervisor",None)
        ctx=context_for_ref(request,body.root_ref)
        if ctx.owner_subject_id!=account or ctx.owner_username!=owner or not auth or not barrier or not supervisor:
            raise HTTPException(503,"Checkpoint unavailable")
        async def current():
            try:
                return (_media_human(request)==(account,owner) and auth.account_id(owner)==account
                    and auth.username_for_account_id(account)==owner and repository.generation()==ctx.policy_generation)
            except Exception:return False
        try:
            return await facade(request,copal_workspace=ctx.workspace_id).selected_checkpoint_operation(ctx,root_ref=body.root_ref,
                root_revision=body.root_revision,items=[item.model_dump() for item in body.items],request_id=body.request_id,
                auth=auth,barrier=barrier,history_supervisor=supervisor,current=current,status_only=body.status_only)
        except FilesFacadeError as error:_raise(error)
        except Exception:raise HTTPException(503,"Checkpoint unavailable")

    @router.post("/checkpoint/registration")
    async def checkpoint_registration(body: CheckpointRegistrationRequest, request: Request, response: Response):
        response.headers["Cache-Control"]="private, no-store"
        account,owner=_media_human(request)
        state=request.app.state;auth=getattr(state,"auth_manager",None);barrier=getattr(state,"account_request_barrier",None);supervisor=getattr(state,"history_supervisor",None)
        ctx=context_for_ref(request,body.root_ref)
        if ctx.owner_subject_id!=account or ctx.owner_username!=owner or not auth or not barrier or not supervisor:
            raise HTTPException(503,"Registration unavailable")
        async def current():
            try:
                return (_media_human(request)==(account,owner) and auth.account_id(owner)==account
                    and auth.username_for_account_id(account)==owner and repository.generation()==ctx.policy_generation)
            except Exception:return False
        try:
            return await facade(request,copal_workspace=ctx.workspace_id).register_selected_checkpoint_files(ctx,root_ref=body.root_ref,
                root_revision=body.root_revision,items=[item.model_dump() for item in body.items],request_id=body.request_id,
                auth=auth,barrier=barrier,history_supervisor=supervisor,current=current,status_only=body.status_only)
        except FilesFacadeError as error:_raise(error)
        except Exception:raise HTTPException(503,"Registration unavailable")

    @router.post("/inspect/gguf")
    async def inspect_gguf(body: InspectFormatRequest, request: Request):
        try:
            ctx = context_for_ref(request, body.resource_ref)
            result = await facade(request).inspect_safetensors(ctx, resource_ref=body.resource_ref, expected_revision=body.expected_revision, format_id='gguf')
            if not _generation_guard(ctx.policy_generation)():
                raise FilesFacadeError("File policy changed", code="policy_generation_changed")
            return result
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/inspect/asar")
    async def inspect_asar(body: InspectFormatRequest, request: Request):
        try:
            ctx = context_for_ref(request, body.resource_ref)
            result = await facade(request).inspect_safetensors(ctx, resource_ref=body.resource_ref, expected_revision=body.expected_revision, format_id='asar')
            if not _generation_guard(ctx.policy_generation)():
                raise FilesFacadeError("File policy changed", code="policy_generation_changed")
            return result
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/inspect/wasm")
    async def inspect_wasm(body: InspectFormatRequest, request: Request):
        try:
            ctx = context_for_ref(request, body.resource_ref)
            result = await facade(request).inspect_safetensors(ctx, resource_ref=body.resource_ref, expected_revision=body.expected_revision, format_id='wasm')
            if not _generation_guard(ctx.policy_generation)():
                raise FilesFacadeError("File policy changed", code="policy_generation_changed")
            return result
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/inspect/elf")
    async def inspect_elf(body: InspectFormatRequest, request: Request):
        try:
            ctx = context_for_ref(request, body.resource_ref)
            result = await facade(request).inspect_safetensors(ctx, resource_ref=body.resource_ref, expected_revision=body.expected_revision, format_id='elf')
            if not _generation_guard(ctx.policy_generation)():
                raise FilesFacadeError("File policy changed", code="policy_generation_changed")
            return result
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/inspect/pe")
    async def inspect_pe(body: InspectFormatRequest, request: Request):
        try:
            ctx = context_for_ref(request, body.resource_ref)
            result = await facade(request).inspect_safetensors(ctx, resource_ref=body.resource_ref, expected_revision=body.expected_revision, format_id='pe')
            if not _generation_guard(ctx.policy_generation)():
                raise FilesFacadeError("File policy changed", code="policy_generation_changed")
            return result
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/inspect/macho")
    async def inspect_macho(body: InspectFormatRequest, request: Request):
        try:
            ctx = context_for_ref(request, body.resource_ref)
            result = await facade(request).inspect_safetensors(ctx, resource_ref=body.resource_ref, expected_revision=body.expected_revision, format_id='macho')
            if not _generation_guard(ctx.policy_generation)():
                raise FilesFacadeError("File policy changed", code="policy_generation_changed")
            return result
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/inspect/sqlite")
    async def inspect_sqlite(body: InspectFormatRequest, request: Request):
        try:
            ctx = context_for_ref(request, body.resource_ref)
            result = await facade(request).inspect_safetensors(ctx, resource_ref=body.resource_ref, expected_revision=body.expected_revision, format_id='sqlite')
            if not _generation_guard(ctx.policy_generation)():
                raise FilesFacadeError("File policy changed", code="policy_generation_changed")
            return result
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/inspect/fastfile")
    async def inspect_fastfile(body: InspectFormatRequest, request: Request):
        try:
            ctx = context_for_ref(request, body.resource_ref)
            result = await facade(request).inspect_safetensors(ctx, resource_ref=body.resource_ref, expected_revision=body.expected_revision, format_id='fastfile')
            if not _generation_guard(ctx.policy_generation)():
                raise FilesFacadeError("File policy changed", code="policy_generation_changed")
            return result
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/reveal")
    async def reveal(body: RevealRequest, request: Request):
        try:
            if body.expected_revision is None:
                return await facade(request).reveal(context_for_ref(request, body.resource_ref), resource_ref=body.resource_ref)

            from src.openclank.resource_refs import ResourceRefError

            owner = context(request)
            try:
                ref = resolve_resource_ref(
                    body.resource_ref, expected_owner_subject_id=owner.owner_subject_id,
                    current_policy_generation=owner.policy_generation, required_capability="stat",
                )
            except ResourceRefError as error:
                raise FilesFacadeError(str(error), code=error.code) from error
            if ref.provider != "host" or ref.kind != "file" or not ref.workspace_id or not ref.location_id:
                raise FilesFacadeError("Workspace match is unavailable", code="resource_unavailable")
            try:
                workspace = repository.get_workspace(ref.workspace_id)
                if workspace.archived or workspace.owner_subject_id != owner.owner_subject_id or workspace.location_id != ref.location_id:
                    raise FilesFacadeError("Workspace match is unavailable", code="resource_unavailable")
                binding = resolve_workspace_binding(
                    repository, workspace, subject_id=owner.owner_subject_id, is_admin=False,
                    purpose="app_folder", observation_only=True,
                )
                root = Path(binding.path)
                if root.resolve(strict=True) != root:
                    raise FilesFacadeError("Workspace match is unavailable", code="resource_unavailable")
            except (OSError, RuntimeError, FilePolicyError, WorkspacePolicyServiceError) as error:
                raise FilesFacadeError("Workspace match is unavailable", code="resource_unavailable") from error

            scoped = replace(owner, is_admin=False, workspace_id=workspace.id)
            result = await facade(request).reveal(
                scoped, resource_ref=body.resource_ref, expected_revision=body.expected_revision.model_dump(),
                workspace_root=root,
            )
            if context(request) != owner or repository.generation() != scoped.policy_generation:
                raise FilesFacadeError("File policy changed", code="policy_generation_changed")
            return result
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/reissue")
    async def reissue_exact(body: StatRequest, request: Request):
        """Reauthorize one stale managed exact-view identity.

        The submitted ref is still sealed and immutable-owner-bound, but its
        old expiry/generation grants no authority here. The facade re-stats the
        current provider record before minting a fresh ResourceRef.
        """

        try:
            return await facade(request).reissue_exact(
                context_for_ref(request, body.resource_ref),
                resource_ref=body.resource_ref,
            )
        except FilesFacadeError as error:
            _raise(error)

    @router.get("/places")
    async def places(request: Request):
        try:
            return await facade(request).places(context(request))
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/places")
    async def save_place(body: PlaceRequest, request: Request):
        try:
            return await facade(request).save_place(context(request), resource_ref=body.resource_ref)
        except FilesFacadeError as error:
            _raise(error)

    @router.delete("/places/{place_id}")
    async def remove_place(place_id: str, request: Request):
        if not re.fullmatch(r"place-[0-9a-f]{32}", str(place_id or "")):
            raise HTTPException(404, "Files place is unavailable")
        try:
            return await facade(request).remove_place(context(request), place_id=place_id)
        except FilesFacadeError as error:
            _raise(error)

    @router.get("/recents")
    async def recents(request: Request):
        try:
            return await facade(request).recents(context(request))
        except FilesFacadeError as error:
            _raise(error)

    @router.delete("/recents")
    async def clear_recents(request: Request):
        try:
            return await facade(request).clear_recents(context(request))
        except FilesFacadeError as error:
            _raise(error)

    @router.get("/saved-searches")
    async def saved_searches(request: Request):
        try:
            return await facade(request).saved_searches(context(request))
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/saved-searches")
    async def save_search(body: SavedSearchRequest, request: Request):
        try:
            return await facade(request).save_search(
                context(request),
                name=body.name,
                provider=body.provider,
                query=body.query,
                sort=body.sort,
            )
        except FilesFacadeError as error:
            _raise(error)

    @router.delete("/saved-searches/{search_id}")
    async def remove_saved_search(search_id: str, request: Request):
        if not re.fullmatch(r"search-[0-9a-f]{32}", str(search_id or "")):
            raise HTTPException(404, "Saved search is unavailable")
        try:
            return await facade(request).remove_saved_search(
                context(request),
                search_id=search_id,
            )
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/workspace-content-search")
    async def workspace_content_search(body: WorkspaceContentSearchRequest, request: Request, response: Response):
        from src.openclank.workspace_source_search import search_workspace_contents

        account, owner = _media_human(request)
        state = request.app.state
        auth = getattr(state, "auth_manager", None)
        barrier = getattr(state, "account_request_barrier", None)
        if auth is None or barrier is None:
            raise HTTPException(503, "Workspace source search is unavailable")

        def current():
            return (getattr(state, "files_policy_repository", None) is repository
                    and getattr(state, "auth_manager", None) is auth
                    and getattr(state, "account_request_barrier", None) is barrier
                    and auth.account_id(owner) == account and auth.username_for_account_id(account) == owner)

        response.headers["Cache-Control"] = "private, no-store"
        try:
            async with barrier.admitted(owner):
                return await search_workspace_contents(repository=repository, auth_manager=auth,
                    account=account, owner=owner, workspace_id=body.workspace_id,
                    expected_revision=body.expected_revision, query=body.query, current=current)
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/search")
    async def search(
        body: SearchRequest,
        request: Request,
        copal_workspace: str = Query(default="default", min_length=1, max_length=64),
    ):
        try:
            return await facade(request, copal_workspace=copal_workspace).search(
                context(request),
                query=body.query,
                limit=body.limit,
                sort=body.sort,
            )
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/resolve-resource")
    async def resolve_resource(body: ResolveResourceRequest, request: Request):
        if body.resource_key.provider == "files" and (getattr(request.state, "internal_tool_authenticated", False) or getattr(request.state, "api_token", False)):
            raise HTTPException(403, "Named human account required")
        try:
            workspace = body.resource_key.workspace_id or "default"
            if body.resource_key.provider != "files":
                return await facade(request, copal_workspace=workspace).resolve_resource(context(request), resource_key=body.resource_key.model_dump(exclude_none=True))
            original = context(request)
            factory = getattr(request.app.state, "capture_original_credential", None)
            if not callable(factory):
                raise HTTPException(503, "Named human account unavailable")
            try:
                current = factory(request, original.owner_username, original.owner_subject_id)
            except Exception as exc:
                raise HTTPException(403, "Named human account unavailable") from exc
            result = await facade(request, copal_workspace=workspace).resolve_resource(original, resource_key=body.resource_key.model_dump(exclude_none=True))
            if (getattr(request.app.state, "capture_original_credential", None) is not factory
                    or current() is not True or context(request) != original):
                raise HTTPException(409, "Gallery image authority changed")
            return result
        except FilesFacadeError as error:
            _raise(error)

    def _binder_catalog(request: Request):
        from routes import prefs_routes as prefs
        from src.openclank.binder_catalog import BinderCatalogError
        username, account = prefs.binder_owner(request)
        if context(request).owner_subject_id != account:
            raise HTTPException(409, "Authenticated account changed")
        try:
            return username, account, prefs.load_binder_catalog(username, account)
        except BinderCatalogError as exc:
            raise HTTPException(409, {"code": exc.code, "message": str(exc)}) from exc

    @router.get("/binders")
    async def list_binders(request: Request):
        _, _, catalog = _binder_catalog(request)
        return catalog

    @router.post("/binders/resolve")
    async def resolve_binder(body: BinderResolveRequest, request: Request):
        username, account, catalog = _binder_catalog(request)
        descriptor = catalog["descriptors"].get(body.binder_id)
        if catalog["revision"] < body.expected_catalog_revision or not descriptor or descriptor != body.expected_descriptor or descriptor["revision"] != body.expected_descriptor_revision or body.binder_id not in catalog["configuredIds"] or not descriptor["enabled"]:
            raise HTTPException(409, "Binder changed or is unavailable")
        source = descriptor["source"]
        ctx = context(request, workspace=source["workspaceId"])
        if ctx.owner_subject_id != account:
            raise HTTPException(409, "Authenticated account changed")
        try:
            resource = await facade(request, copal_workspace=source["workspaceId"]).resolve_binder_folder(ctx, source=source)
            new_user, new_account, latest = _binder_catalog(request)
            if new_user != username or new_account != account or latest["revision"] < catalog["revision"] or latest["descriptors"].get(body.binder_id) != descriptor or body.binder_id not in latest["configuredIds"] or repository.generation() != ctx.policy_generation:
                raise FilesFacadeError("Binder changed during resolution", code="resource_ref_stale")
            home = None
            if body.open_home and descriptor["id"] == "default-journal" and source == {"workspaceId": "default", "root": [], "corpus": "notes"}:
                from src.auth_helpers import copal_owner_for_user
                result = await request.app.state.copal_bridge.call("open_default_journal", {
                    "owner": copal_owner_for_user(username), "workspace_id": source["workspaceId"],
                })
                home = result.get("documentId")
            return {"homeDocumentId": home, "binderId": body.binder_id, "catalogRevision": latest["revision"], "descriptorRevision": descriptor["revision"], "resource": resource}
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/binders/preview")
    async def preview_binder(body: BinderPreviewRequest, request: Request):
        from routes import prefs_routes as prefs
        from src.auth_helpers import copal_owner_for_user
        from src.openclank.binder_catalog import BinderCatalogError
        from src.openclank.copal_loose import LooseCopalRepository
        from src.openclank.binder_catalog_migration import PreflightError, inventory_vault, propose_catalog
        import hashlib

        username, account, catalog = _binder_catalog(request)
        if account != body.expected_account_id or catalog["revision"] != body.expected_catalog_revision:
            raise HTTPException(409, "Binder catalog or account changed")
        first, second = context_for_ref(request, body.source_ref), context_for_ref(request, body.untouched_ref)
        if first.owner_subject_id != account or second.owner_subject_id != account or first.owner_username != second.owner_username or first.policy_generation != second.policy_generation:
            raise HTTPException(409, "Authenticated account changed")
        bridge = getattr(request.app.state, "copal_bridge", None)
        if not isinstance(bridge, LooseCopalRepository):
            raise HTTPException(409, "This Copal source cannot be inventoried read-only")
        owner = copal_owner_for_user(first.owner_username)
        try:
            source = await facade(request, copal_workspace=first.workspace_id).describe_binder_folder(first, resource_ref=body.source_ref)
            untouched = await facade(request, copal_workspace=second.workspace_id).describe_binder_folder(second, resource_ref=body.untouched_ref)
            if source == untouched:
                raise PreflightError("Select a distinct untouched source")
            def safe_vault(workspace: str) -> Path:
                root = bridge.data_dir
                vault = bridge._vault(owner, workspace)
                for path in (root, root / bridge._segment(owner), vault):
                    if path.is_symlink() or not path.is_dir():
                        raise PreflightError("The selected Copal scope is unavailable or symlinked")
                return vault
            selected_vault, untouched_vault = safe_vault(source["workspaceId"]), safe_vault(untouched["workspaceId"])
            async def capture():
                one = await asyncio.to_thread(inventory_vault, selected_vault, expected_owner=owner)
                two = one if selected_vault == untouched_vault else await asyncio.to_thread(inventory_vault, untouched_vault, expected_owner=owner)
                return one, two
            one, two = await capture()
            check_one, check_two = await capture()
            if one["fingerprint"] != check_one["fingerprint"] or two["fingerprint"] != check_two["fingerprint"]:
                raise PreflightError("The selected source changed during preview")
            if prefs.binder_owner(request) != (username, account) or repository.generation() != first.policy_generation or _binder_catalog(request)[2] != catalog:
                raise PreflightError("The account, policy or catalog changed during preview")
            current_source = await facade(request, copal_workspace=first.workspace_id).describe_binder_folder(first, resource_ref=body.source_ref)
            current_untouched = await facade(request, copal_workspace=second.workspace_id).describe_binder_folder(second, resource_ref=body.untouched_ref)
            if current_source != source or current_untouched != untouched:
                raise PreflightError("A selected folder changed during preview")
            if prefs.binder_owner(request) != (username, account) or repository.generation() != first.policy_generation or _binder_catalog(request)[2] != catalog:
                raise PreflightError("The account, policy or catalog changed during preview")
            patch = propose_catalog(account_id=account, catalog=catalog, binder_id=body.binder_id, display_name=body.display_name, source=source, expected_revision=catalog["revision"])
            return {"accountId":account, "source":source, "untouched":untouched,
                    "rootFingerprint":hashlib.sha256(str(bridge.data_dir).encode()).hexdigest(),
                    "sourceInventory":one, "untouchedInventory":two, "proposal":patch}
        except FilesFacadeError as error:
            _raise(error)
        except OSError as error:
            raise HTTPException(409, "A selected Copal scope could not be read") from error
        except (PreflightError, BinderCatalogError) as error:
            raise HTTPException(409, str(error)) from error

    @router.post("/binders/conversions")
    async def apply_binder_conversion(body: BinderApplyRequest, request: Request):
        from routes import prefs_routes as prefs
        from src.auth_helpers import copal_owner_for_user
        from src.openclank.binder_catalog import BinderCatalogError, apply_conversion, mark_conversion_review, operation_digest, rollback_conversion, settle_conversion, storage_parts
        from src.openclank.copal_commit_lock import copal_commit_lock
        from src.openclank.copal_loose import LooseCopalRepository
        from src.openclank.binder_catalog_migration import PreflightError, inventory_vault, propose_catalog
        import hashlib

        username, account, catalog = _binder_catalog(request)
        if account != body.expected_account_id:
            raise HTTPException(409, "Authenticated account changed")
        try:
            with prefs.PREFERENCE_LOCK:
                _, operations = storage_parts(prefs._load_for_user(username).get(prefs._BINDER_CATALOG_KEY), account)
                old = operations.get(body.operation_id)
            if old is None and catalog["revision"] != body.expected_catalog_revision:
                raise PreflightError("Binder catalog changed after preview")
            first, second = context_for_ref(request, body.source_ref), context_for_ref(request, body.untouched_ref)
            if first.owner_subject_id != account or second.owner_subject_id != account or first.owner_username != second.owner_username or first.policy_generation != second.policy_generation:
                raise PreflightError("Authenticated account or policy changed")
            bridge = getattr(request.app.state, "copal_bridge", None)
            if not isinstance(bridge, LooseCopalRepository) or hashlib.sha256(str(bridge.data_dir).encode()).hexdigest() != body.root_fingerprint:
                raise PreflightError("Configured Copal source changed")
            source = await facade(request, copal_workspace=first.workspace_id).describe_binder_folder(first, resource_ref=body.source_ref)
            untouched = await facade(request, copal_workspace=second.workspace_id).describe_binder_folder(second, resource_ref=body.untouched_ref)
            if source == untouched:
                raise PreflightError("Select a distinct untouched source")
            operation_request = {"accountId":account, "expectedRevision":body.expected_catalog_revision, "catalog":body.catalog,
                "operationId":body.operation_id, "binderId":body.binder_id, "displayName":body.display_name,
                "source":source, "untouched":untouched, "sourceFingerprint":body.source_fingerprint,
                "untouchedFingerprint":body.untouched_fingerprint, "rootFingerprint":body.root_fingerprint}
            if old is not None:
                if old["requestDigest"] != operation_digest(operation_request):
                    raise BinderCatalogError("Conversion operation payload changed", "revision_conflict")
                if old["state"] != "pending":
                    return {"operationId":body.operation_id, "receipt":old}
            else:
                proposed = propose_catalog(account_id=account, catalog=catalog, binder_id=body.binder_id, display_name=body.display_name, source=source, expected_revision=body.expected_catalog_revision)["request"]
                if proposed != body.catalog:
                    raise PreflightError("The reviewed catalog proposal changed")
            owner = copal_owner_for_user(first.owner_username)
            def execute():
                def scope_path(workspace: str) -> Path:
                    root = bridge.data_dir
                    vault = bridge._vault(owner, workspace)
                    if any(path.is_symlink() or not path.is_dir() for path in (root, root / bridge._segment(owner), vault)):
                        raise PreflightError("A selected Copal scope is unavailable or symlinked")
                    return vault
                with copal_commit_lock(bridge.data_dir):
                    selected, other = scope_path(source["workspaceId"]), scope_path(untouched["workspaceId"])
                    def capture():
                        one = inventory_vault(selected, expected_owner=owner)
                        two = one if selected == other else inventory_vault(other, expected_owner=owner)
                        if one["missingIds"] or two["missingIds"]:
                            raise PreflightError("A selected source has missing records")
                        return one["fingerprint"], two["fingerprint"]
                    def review(*, rollback: bool):
                        with prefs.PREFERENCE_LOCK:
                            if prefs.binder_owner(request) != (username, account) or repository.generation() != first.policy_generation:
                                raise PreflightError("Authenticated account or policy changed")
                            current = prefs._load_for_user(username)
                            value, receipt = mark_conversion_review(current.get(prefs._BINDER_CATALOG_KEY), account_id=account, operation_id=body.operation_id)
                            current[prefs._BINDER_CATALOG_KEY] = value
                            prefs._save_for_user(username, current, allow_binder_catalog=True)
                            if rollback:
                                try:
                                    value, receipt = rollback_conversion(value, account_id=account, operation_id=body.operation_id)
                                    current[prefs._BINDER_CATALOG_KEY] = value
                                    prefs._save_for_user(username, current, allow_binder_catalog=True)
                                except BinderCatalogError:
                                    pass
                            return receipt
                    captured = capture()
                    if captured != (body.source_fingerprint, body.untouched_fingerprint):
                        if old is None:
                            raise PreflightError("A selected source changed after preview")
                        return review(rollback=True)
                    with prefs.PREFERENCE_LOCK:
                        if prefs.binder_owner(request) != (username, account) or repository.generation() != first.policy_generation:
                            raise PreflightError("Authenticated account or policy changed")
                        current = prefs._load_for_user(username)
                        value, receipt = apply_conversion(current.get(prefs._BINDER_CATALOG_KEY), account_id=account, operation_id=body.operation_id, request=operation_request, incoming=body.catalog, expected_revision=body.expected_catalog_revision)
                        if old is None:
                            current[prefs._BINDER_CATALOG_KEY] = value
                            prefs._save_for_user(username, current, allow_binder_catalog=True)
                    try:
                        checked = capture()
                    except (OSError, PreflightError):
                        return review(rollback=False)
                    if checked != (body.source_fingerprint, body.untouched_fingerprint):
                        return review(rollback=True)
                    with prefs.PREFERENCE_LOCK:
                        if prefs.binder_owner(request) != (username, account) or repository.generation() != first.policy_generation:
                            raise PreflightError("Authenticated account or policy changed")
                        current = prefs._load_for_user(username)
                        value, receipt = settle_conversion(current.get(prefs._BINDER_CATALOG_KEY), account_id=account, operation_id=body.operation_id)
                        current[prefs._BINDER_CATALOG_KEY] = value
                        prefs._save_for_user(username, current, allow_binder_catalog=True)
                    return receipt
            receipt = await asyncio.to_thread(execute)
            return {"operationId":body.operation_id, "receipt":receipt}
        except FilesFacadeError as error:
            _raise(error)
        except OSError as error:
            raise HTTPException(409, "A selected Copal scope could not be read") from error
        except (PreflightError, BinderCatalogError) as error:
            raise HTTPException(409, str(error)) from error

    @router.get("/binders/conversions/{operation_id}")
    async def binder_conversion_receipt(operation_id: str, request: Request):
        from routes import prefs_routes as prefs
        from src.openclank.binder_catalog import BinderCatalogError, storage_parts
        username, account, _ = _binder_catalog(request)
        try:
            with prefs.PREFERENCE_LOCK:
                _, operations = storage_parts(prefs._load_for_user(username).get(prefs._BINDER_CATALOG_KEY), account)
                receipt = operations.get(operation_id)
            if receipt is None:
                raise HTTPException(404, "Conversion receipt unavailable")
            return {"operationId":operation_id, "receipt":receipt}
        except BinderCatalogError as error:
            raise HTTPException(409, str(error)) from error

    @router.post("/binders/conversions/{operation_id}/rollback")
    async def rollback_binder_conversion(operation_id: str, request: Request):
        from routes import prefs_routes as prefs
        from src.openclank.binder_catalog import BinderCatalogError, rollback_conversion
        username, account, _ = _binder_catalog(request)
        try:
            with prefs.PREFERENCE_LOCK:
                if prefs.binder_owner(request) != (username, account):
                    raise BinderCatalogError("Authenticated account changed", "account_mismatch")
                current = prefs._load_for_user(username)
                value, receipt = rollback_conversion(current.get(prefs._BINDER_CATALOG_KEY), account_id=account, operation_id=operation_id)
                current[prefs._BINDER_CATALOG_KEY] = value
                prefs._save_for_user(username, current, allow_binder_catalog=True)
            return {"operationId":operation_id, "receipt":receipt}
        except BinderCatalogError as error:
            raise HTTPException(409, str(error)) from error

    @router.post("/binders/configure")
    async def configure_binder(body: BinderCreateRequest, request: Request):
        from routes import prefs_routes as prefs
        from src.openclank.binder_catalog import BinderCatalogError, prepare_storage_update, store_catalog
        username, account, catalog = _binder_catalog(request)
        if account != body.expected_account_id or catalog["revision"] != body.expected_catalog_revision:
            raise HTTPException(409, "Binder catalog or account changed")
        ctx = context_for_ref(request, body.resource_ref)
        if ctx.owner_subject_id != account:
            raise HTTPException(409, "Authenticated account changed")
        try:
            source = await facade(request, copal_workspace=ctx.workspace_id).describe_binder_folder(ctx, resource_ref=body.resource_ref)
            if prefs.binder_owner(request) != (username, account) or repository.generation() != ctx.policy_generation:
                raise FilesFacadeError("Binder authorization changed", code="resource_ref_stale")
            binder_id = f"binder-{uuid.uuid4().hex}"
            proposed = {**catalog, "configuredIds": [*catalog["configuredIds"], binder_id], "descriptors": {**catalog["descriptors"], binder_id: {"id": binder_id, "displayName": body.name, "provider": "copal", "source": source, "enabled": True, "revision": 0}}}
            if body.make_default or proposed["defaultId"] is None:
                proposed["defaultId"] = binder_id
            with prefs.PREFERENCE_LOCK:
                if prefs.binder_owner(request) != (username, account) or repository.generation() != ctx.policy_generation:
                    raise FilesFacadeError("Binder authorization changed", code="resource_ref_stale")
                current = prefs._load_for_user(username)
                stored = current.get(prefs._BINDER_CATALOG_KEY)
                updated = prepare_storage_update(stored, proposed, account, body.expected_catalog_revision)
                current[prefs._BINDER_CATALOG_KEY] = store_catalog(stored, updated, account)
                prefs._save_for_user(username, current, allow_binder_catalog=True)
            return updated
        except BinderCatalogError as error:
            raise HTTPException(409 if error.code in {"revision_conflict", "account_mismatch"} else 422, {"code": error.code, "message": str(error)}) from error
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/transfers/root-requirement")
    async def transfer_root_requirement(body: TransferRootPreflightRequest, request: Request):
        try:
            return await facade(request).transfer_root_requirement(context(request), generation=body.generation,
                sources=[item.model_dump(exclude_none=True) for item in body.sources], destination_ref=body.destination_ref)
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/transfers")
    async def transfer_resources(body: TransferRequest, request: Request):
        try:
            return await facade(request).transfer_resources(
                context(request), operation_id=body.operation_id, generation=body.generation,
                kind=body.kind, sources=[item.model_dump() for item in body.sources],
                destination_ref=body.destination_ref, collision=body.collision,
                managed_root=body.managed_root.model_dump(exclude_none=True) if body.managed_root else None,
                destination_root=body.destination_root.model_dump(exclude_none=True) if body.destination_root else None,
            )
        except FilesFacadeError as error:
            _raise(error)

    @router.get("/operations/{operation_id}")
    async def operation_receipt(operation_id: str, request: Request):
        try:
            return await facade(request).operation_receipt(context(request), operation_id=operation_id)
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/operations/{operation_id}/folder-cleanup")
    async def cleanup_managed_folder(operation_id: str, request: Request, item_id: str = Query(min_length=1, max_length=128),
                                     managed_root: ManagedTransferRoot | None = None):
        try:
            return await facade(request).cleanup_managed_folder(context(request), operation_id=operation_id, item_id=item_id,
                managed_root=managed_root.model_dump(exclude_none=True) if managed_root else None)
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/operations/{operation_id}/undo-move")
    async def undo_managed_move(operation_id: str, request: Request, item_id: str = Query(min_length=1, max_length=128)):
        """Undo one completed cross-root file move. Idempotent: the undo id is derived, so a retry is the same undo."""
        try:
            return await facade(request).undo_managed_move(context(request), operation_id=operation_id, item_id=item_id)
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/imports")
    async def import_file(
        request: Request,
        file: UploadFile = File(...),
        metadata: str = Form(...),
    ):
        try:
            parsed = json.loads(metadata)
            if not isinstance(parsed, dict):
                raise FilesFacadeError("import metadata is invalid", code="invalid_resource_request")
            if 'videoFrameSource' not in parsed:
                return await facade(request).import_file(context(request), upload=file, metadata=parsed)
            account, owner = _media_human(request)
            state=request.app.state; auth=getattr(state,'auth_manager',None); barrier=getattr(state,'account_request_barrier',None)
            if auth is None or barrier is None or not isinstance(parsed.get('destination_ref'),str):
                raise FilesFacadeError('Video frame import unavailable',code='provider_unavailable')
            token=request.cookies.get('open_clank_session')
            if not isinstance(token,str) or not token or auth.get_username_for_token(token)!=owner:
                raise FilesFacadeError('Video frame session unavailable',code='resource_unavailable')
            ctx=context_for_ref(request,parsed['destination_ref'])
            if ctx.owner_subject_id != account or ctx.owner_username != owner:
                raise FilesFacadeError('Video frame import unavailable',code='resource_unavailable')
            async def current():
                try:
                    key=str(owner).strip().lower()
                    return (_media_human(request)==(account,owner) and state.auth_manager is auth and state.account_request_barrier is barrier
                        and request.cookies.get('open_clank_session')==token and auth.get_username_for_token(token)==owner
                        and not barrier._fenced(key) and not (barrier._absolute_fence and barrier._absolute_fence(key))
                        and auth.account_id(owner)==account and auth.username_for_account_id(account)==owner
                        and files_principal(request,repository=repository)[2]==ctx.is_admin and repository.generation()==ctx.policy_generation)
                except Exception: return False
            async with barrier.admitted(owner):
                if not await current(): raise FilesFacadeError('Video frame import unavailable',code='resource_changed')
                return await facade(request,copal_workspace=ctx.workspace_id).import_file(ctx,upload=file,metadata=parsed,video_current=current)
        except json.JSONDecodeError as error:
            _raise(FilesFacadeError("import metadata is invalid", code="invalid_resource_request"))
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/recording-bundle")
    async def recording_bundle(request: Request):
        from routes.session_routes import _verify_session_owner
        from src.openclank.recording_bundle import BundleUnavailable, build_recording_bundle
        try:
            body = await request.json()
            keys = {'version','session_id','source_ref','expected_revision','feedback_ids','include_source',
                    'operation_id','item_id','destination_ref','name'}
            if (type(body) is not dict or set(body) not in (keys, keys | {'original_job_id'}) or type(body['version']) is not int or body['version'] != 1
                or ('original_job_id' in body and (type(body['original_job_id']) is not str or not re.fullmatch(r'[a-f0-9]{32}',body['original_job_id'])))
                or any(type(body[k]) is not str or not body[k] or len(body[k].encode('utf-8')) > limit
                       for k, limit in (('session_id',512),('source_ref',16384),('operation_id',128),
                                        ('item_id',128),('destination_ref',16384),('name',240)))):
                raise FilesFacadeError('Recording bundle selection is invalid', code='invalid_resource_request')
            account, owner = _media_human(request)
            state = request.app.state
            auth = getattr(state, 'auth_manager', None)
            barrier = getattr(state, 'account_request_barrier', None)
            token = request.cookies.get('open_clank_session')
            if not auth or not barrier or not token or auth.get_username_for_token(token) != owner:
                raise FilesFacadeError('Recording bundle authority unavailable', code='resource_unavailable')
            ctx = context_for_ref(request, body['destination_ref'])
            if ctx.owner_subject_id != account or ctx.owner_username != owner:
                raise FilesFacadeError('Recording bundle authority unavailable', code='resource_unavailable')
            selected_facade = facade(request, copal_workspace=ctx.workspace_id)
            selector = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()

            def session_current():
                try:
                    key = str(owner).strip().lower()
                    return bool(_media_human(request) == (account, owner) and state.auth_manager is auth
                        and state.account_request_barrier is barrier and request.cookies.get('open_clank_session') == token
                        and auth.get_username_for_token(token) == owner and auth.account_id(owner) == account
                        and auth.username_for_account_id(account) == owner and not barrier._fenced(key)
                        and not (barrier._absolute_fence and barrier._absolute_fence(key))
                        and repository.generation() == ctx.policy_generation
                        and files_principal(request, repository=repository)[2] == ctx.is_admin
                        and _verify_session_owner(request, body['session_id']) == owner)
                except Exception:
                    return False

            async def current():
                try: return await asyncio.wait_for(asyncio.to_thread(session_current), 5) is True
                except (TimeoutError, Exception): return False

            async def original_status():
                stored = selected_facade._load_operation(ctx, body['operation_id'])
                if stored is None:
                    return None
                receipt = stored.get('receipt') or {}
                binding = (receipt.get('_import_binding') or {}).get('derived_source') or {}
                if binding.get('request_digest') != selector or stored.get('generation') != ctx.policy_generation:
                    raise FilesFacadeError('Operation identity was already used', code='idempotency_conflict')
                return await selected_facade.operation_receipt(ctx, operation_id=body['operation_id'])

            async with barrier.admitted(owner):
                if await current() is not True:
                    raise FilesFacadeError('Recording bundle authority changed', code='resource_changed')
                prior = await original_status()
                if prior is not None:
                    if await current() is not True:
                        raise FilesFacadeError('Recording bundle authority changed', code='resource_changed')
                    return prior
                destination = await selected_facade.capture_media_destination(ctx, resource_ref=body['destination_ref'])
                async with build_recording_bundle(facade=selected_facade, context=ctx, source_ref=body['source_ref'],
                        expected_revision=body['expected_revision'], session_id=body['session_id'], owner=owner,
                        account=account, feedback_ids=body['feedback_ids'], include_source=body['include_source'],
                        name=body['name'], current=current, session_current=session_current, original_job_id=body.get('original_job_id')) as bundle:
                    evidence = dict(bundle.source_evidence, request_digest=selector)
                    try:
                        result = await selected_facade.import_file(ctx, upload=bundle,
                            metadata={'operation_id':body['operation_id'], 'item_id':body['item_id'],
                                      'generation':ctx.policy_generation, 'destination_ref':body['destination_ref'],
                                      'name':body['name'], 'collision':'fail'}, source_evidence=evidence,
                            before_commit=bundle.current, expected_destination_revision=destination['destination_revision'])
                        if await current() is not True:
                            raise FilesFacadeError('Recording bundle authority changed', code='resource_changed')
                        return result
                    except FilesFacadeError as error:
                        if error.code == 'idempotency_conflict':
                            prior = await original_status()
                            if prior is not None:
                                if await current() is not True:
                                    raise FilesFacadeError('Recording bundle authority changed', code='resource_changed')
                                return prior
                        raise
        except (BundleUnavailable, ValueError, TypeError, UnicodeError):
            _raise(FilesFacadeError('Recording bundle is unavailable', code='resource_unavailable'))
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/iwi-png-copy")
    async def iwi_png_copy(body: IwiCopyRequest, request: Request):
        try:
            return await facade(request).save_iwi_png_copy(context(request), request=body.model_dump())
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/attachments/prepare")
    async def prepare_attachment(body: AttachmentPrepareRequest, request: Request, copal_workspace: str = Query(default="default", min_length=1, max_length=64)):
        try:
            return await facade(request, copal_workspace=copal_workspace).prepare_attachment(
                context(request, workspace=copal_workspace), operation_id=body.operation_id, generation=body.generation,
                source=body.source.model_dump(exclude_none=True), target=body.target.model_dump(exclude_none=True), mode=body.mode,
            )
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/attachments/prepare-upload")
    async def prepare_attachment_upload(request: Request, copal_workspace: str = Query(default="default", min_length=1, max_length=64)):
        from src.upload_limits import FILES_IMPORT_MAX_BYTES
        limit = min(FILES_IMPORT_MAX_BYTES, 10 * 1024 * 1024)
        try:
            encoded = request.headers.get('x-openclank-attachment', '')
            if not encoded or len(encoded) > 65536 or not re.fullmatch(r'[A-Za-z0-9_-]+', encoded):
                raise ValueError('invalid metadata')
            meta = AttachmentUploadMetadata.model_validate_json(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
            if (meta.target.kind != 'host_document' or meta.target.expected_revision is None
                or len(meta.name.encode('utf-8')) > 240 or len(meta.mime.encode('utf-8')) > 128
                or any(ord(c) < 32 or ord(c) == 127 for c in meta.name + meta.mime)
                or any(c in meta.name for c in ('/', '\\')) or meta.name in ('.', '..')):
                raise ValueError('invalid metadata')
            if request.headers.get('content-encoding', 'identity').lower() != 'identity':
                raise ValueError('unsupported content encoding')
            declared = request.headers.get('content-length')
            if declared is not None and (not declared.isascii() or not declared.isdigit() or int(declared) > limit):
                raise ValueError('upload exceeds limit')
            ctx = context(request, workspace=copal_workspace)
            files = facade(request, copal_workspace=copal_workspace)
            if meta.generation != ctx.policy_generation or not files._generation_current(ctx):
                raise ValueError('policy changed')
            provider, ref = files._provider_for_ref(ctx, meta.target.resource_ref, capability='write')
            target_entry = await provider.stat(ctx, origin_id=ref.origin_id)
            if ref.provider != 'host' or target_entry.origin_id != ref.origin_id or 'write' not in target_entry.capabilities:
                raise ValueError('target unavailable')
            files._check_revision(target_entry, meta.target.expected_revision.model_dump())
            if meta.target.root_ref is not None:
                root_provider, root_ref = files._provider_for_ref(ctx, meta.target.root_ref, capability='write')
                root_entry = await root_provider.stat(ctx, origin_id=root_ref.origin_id)
                if (root_ref.provider != 'host' or root_provider is not provider or root_entry.origin_id != root_ref.origin_id
                    or root_entry.kind not in {'folder','virtual_folder','provider_root'} or not {'write','children'}.issubset(root_entry.capabilities)):
                    raise ValueError('root unavailable')
                files._check_revision(root_entry, meta.target.expected_root_revision.model_dump())
            chunks = bytearray()
            async with asyncio.timeout(30):
                async for part in request.stream():
                    if len(chunks) + len(part) > limit:
                        raise ValueError('upload exceeds limit')
                    chunks.extend(part)
            if not chunks or (declared is not None and len(chunks) != int(declared)):
                raise ValueError('invalid upload length')
            data = bytes(chunks)
            source = {'kind':'client_upload','name':meta.name,'mime':meta.mime,'sha256':hashlib.sha256(data).hexdigest(),'size':len(data)}
            return await files.prepare_attachment(ctx, operation_id=meta.operation_id, generation=meta.generation,
                source=source, target=meta.target.model_dump(exclude_none=True), mode=meta.mode, upload_bytes=data)
        except FilesFacadeError as error:
            _raise(error)
        except (ValueError, TypeError, TimeoutError):
            raise HTTPException(400, 'Attachment upload unavailable or invalid') from None

    @router.get("/attachments/{operation_id}", response_model=AttachmentReceiptResponse)
    async def attachment_receipt(operation_id: str, request: Request, copal_workspace: str = Query(default="default", min_length=1, max_length=64)):
        try:
            return await facade(request, copal_workspace=copal_workspace).attachment_receipt(
                context(request, workspace=copal_workspace), operation_id=operation_id,
            )
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/bases/query")
    async def query_base(body: BaseQueryRequest, request: Request):
        try:
            return await facade(request).query_base_resource(
                context(request), base_ref=body.base_ref, expected_revision=body.expected_revision.model_dump() if body.expected_revision else None,
                corpus_ref=body.corpus_ref, generation=body.generation, view_id=body.view_id,
                query=body.query, page=body.page, page_size=body.page_size,
                context_ref=body.context_ref, draft_definition=body.draft_definition,
            )
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/action/root-requirement")
    async def action_root_requirement(body: ActionRootPreflightRequest, request: Request):
        try:
            return await facade(request).action_root_requirement(context_for_ref(request, body.resource_ref),
                generation=body.generation, resource_ref=body.resource_ref, name=body.name,
                expected_revision=body.expected_revision.model_dump() if body.expected_revision else None)
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/action")
    async def action(body: ActionRequest, request: Request):
        try:
            if body.action == "open":
                if body.args:
                    raise HTTPException(422, "Open does not accept arguments")
            elif body.action in {"rename", "move"} and (set(body.args) != {"name"} or not isinstance(body.args.get("name"), str) or not body.args["name"].strip()):
                raise HTTPException(422, "Rename and move require one non-empty name")
            elif body.action == "trash" and body.args:
                raise HTTPException(422, "This action does not accept arguments")
            elif body.action == "restore" and body.args:
                # Only a Host undo carries args: exactly one closed trash entry. The facade rechecks it and refuses other providers.
                try:
                    if set(body.args) != {"entry"} or not isinstance(body.args["entry"], dict):
                        raise ValueError
                    TrashEntryRequest.model_validate(body.args["entry"])
                except ValueError:
                    raise HTTPException(422, "Restore accepts one trash entry") from None
            elif body.action in {"favorite.set", "archive.set"} and (set(body.args) != {"value"} or not isinstance(body.args.get("value"), bool)):
                raise HTTPException(422, "This action requires one boolean value")
            return await facade(request).action(
                context_for_ref(request, body.resource_ref),
                resource_ref=body.resource_ref,
                action=body.action,
                args=body.args,
                action_id=body.action_id,
                expected_revision=body.expected_revision.model_dump() if body.expected_revision else None,
                managed_root=body.managed_root.model_dump(exclude_none=True) if body.managed_root else None,
            )
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/open-resource")
    async def open_resource(body: OpenResourceRequest, request: Request):
        try:
            return await facade(request).open_payload(context_for_ref(request, body.resource_ref), resource_ref=body.resource_ref,
                                                      expected_revision=body.expected_revision, purpose=body.purpose)
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/host-apps")
    async def host_apps(body: OpenResourceRequest, request: Request):
        try:
            return await facade(request).host_applications(
                context_for_ref(request, body.resource_ref), resource_ref=body.resource_ref,
            )
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/open-host")
    async def open_host(body: HostOpenRequest, request: Request):
        try:
            return await facade(request).open_on_host(
                context_for_ref(request, body.resource_ref), resource_ref=body.resource_ref, app_id=body.app_id,
            )
        except FilesFacadeError as error:
            _raise(error)

    def _shared_text_authority(request: Request):
        from src.auth_helpers import require_user, effective_user, is_internal_tool_identity
        from src.published_files import PublishedFileService
        user=require_user(request); state=request.app.state
        auth=getattr(state,'auth_manager',None); barrier=getattr(state,'account_request_barrier',None)
        if (not auth or not barrier or getattr(state,'files_policy_repository',None) is not repository
            or is_internal_tool_identity(user) or getattr(request.state,'api_token',False)
            or getattr(request.state,'internal_tool_authenticated',False) or (effective_user(request) or user)!=user):
            raise HTTPException(403,'Shared text unavailable')
        account=auth.account_id(user); session_token=request.cookies.get('open_clank_session')
        if (not account or auth.username_for_account_id(account)!=user or not session_token
            or auth.get_username_for_token(session_token)!=user):raise HTTPException(403,'Shared text unavailable')
        factory=getattr(state,'capture_original_credential',None)
        if not callable(factory): raise HTTPException(503,'Shared text unavailable')
        try: credential=factory(request,user,account)
        except Exception as exc: raise HTTPException(403,'Shared text unavailable') from exc
        if not callable(credential) or credential() is not True: raise HTTPException(403,'Shared text unavailable')
        def current_sync():
            try:
                return (getattr(state,'capture_original_credential',None) is factory and credential() is True
                    and getattr(state,'auth_manager',None) is auth and getattr(state,'account_request_barrier',None) is barrier
                    and getattr(state,'files_policy_repository',None) is repository and require_user(request)==user
                    and not is_internal_tool_identity(user) and not getattr(request.state,'api_token',False)
                    and not getattr(request.state,'internal_tool_authenticated',False) and (effective_user(request) or user)==user
                    and auth.account_id(user)==account and auth.username_for_account_id(account)==user
                    and auth.get_username_for_token(session_token)==user)
            except Exception:return False
        async def current(): return current_sync()
        current.sync = current_sync
        def provider_for_owner(owner_context):
            return facade_for_state(state,copal_workspace=owner_context.workspace_id)._providers.get('host')
        return account,auth,barrier,current,provider_for_owner,PublishedFileService(session_factory=session_factory)

    @router.get('/shared-text/events')
    async def shared_text_events(request: Request, publication_id: str = Query(min_length=1, max_length=128)):
        """Best-effort invalidations; every subscriber remains independently authorized."""
        from src.openclank.shared_resource_authority import recipient_live_target
        from src.openclank.resource_event_stream import resource_invalidations
        try:
            account,auth,barrier,current,provider_for_owner,service=_shared_text_authority(request)
            async def authorize():
                async with recipient_live_target(service=service,publication_id=publication_id,recipient_account=account,
                        auth=auth,barrier=barrier,store=repository,provider_for_owner=provider_for_owner,original_current=current) as target:
                    if not await target.current(check_revision=False): return None
                    return (target.owner_subject_id,target.resource['resource_id'])
            key=await authorize()
            if not key: raise ValueError('Shared text unavailable')
            handle=await resource_invalidations.subscribe(*key,authorize)
        except HTTPException: raise
        except Exception as exc: raise HTTPException(409,'Shared text unavailable or changed') from exc

        async def stream():
            try:
                if await authorize()!=key: return
                yield b'event: ready\ndata: {}\n\n'
                while True:
                    item=await handle.next(timeout=15)
                    if item is None:
                        yield b': keepalive\n\n'
                    else:
                        data=json.dumps(item,separators=(',',':'))
                        yield f"event: {item['kind']}\ndata: {data}\n\n".encode('utf-8')
            except (PermissionError,ValueError):
                return
            finally:
                handle.close()
        return StreamingResponse(stream(),media_type='text/event-stream',headers={
            'Cache-Control':'private, no-store','X-Accel-Buffering':'no','X-Content-Type-Options':'nosniff'})

    def _shared_text_core(request):
        from src.openclank.shared_text_service import SharedTextService
        account,auth,barrier,current,provider_for_owner,service=_shared_text_authority(request)
        return account,SharedTextService(service=service,auth=auth,barrier=barrier,store=repository,
            provider_for_owner=provider_for_owner,original_current=current)

    def _shared_feedback_current(request, target, auth, barrier, current, source_current=lambda: True):
        def check():
            try:
                return (current.sync() is True and source_current() is True
                    and not barrier._fenced(target.owner) and not barrier._fenced(auth.username_for_account_id(target.recipient_subject_id))
                    and auth.account_id(target.owner) == target.owner_subject_id
                    and auth.username_for_account_id(target.owner_subject_id) == target.owner
                    and auth.is_admin(target.owner) == target.context.is_admin
                    and getattr(request.app.state, 'files_policy_repository', None) is repository)
            except Exception: return False
        return check

    def _shared_feedback_public(result, auth, account):
        def entry(value):
            item=dict(value); author=item.pop('author')
            try:
                name=auth.username_for_account_id(author)
                if not name or auth.account_id(name)!=author: name=None
            except Exception: name=None
            item['authorLabel']=name or 'Unavailable author'
            item['authorIsCurrent']=author==account
            return item
        result=dict(result)
        if 'entries' in result: result['entries']=[entry(value) for value in result['entries']]
        if 'entry' in result: result['entry']=entry(result['entry'])
        return result

    @router.get('/shared-feedback/profile')
    async def shared_feedback_profile(request: Request, response: Response):
        from src.openclank.shared_feedback import _same_database, _profile
        response.headers['Cache-Control']='private, no-store'
        account,auth,barrier,current,provider_for_owner,service=_shared_text_authority(request)
        try:
            if not await current(): raise ValueError('Shared feedback unavailable')
            _same_database(repository,service)
            with repository._connect() as connection: _profile(connection)
            if not await current(): raise ValueError('Shared feedback unavailable')
            return {'version':1,'available':True}
        except Exception as exc: raise HTTPException(409,'Shared feedback setup unavailable') from exc

    @router.post('/shared-feedback/list')
    async def shared_feedback_list(request: Request, response: Response):
        from src.openclank.shared_resource_authority import recipient_live_target
        from src.openclank.shared_feedback import list_annotations
        response.headers['Cache-Control']='private, no-store'
        account,auth,barrier,current,provider_for_owner,service=_shared_text_authority(request)
        body=await _shared_text_request_body(request,SharedTextReadRequest,maximum=4096)
        try:
            async with recipient_live_target(service=service,publication_id=body.publication_id,recipient_account=account,
                    auth=auth,barrier=barrier,store=repository,provider_for_owner=provider_for_owner,original_current=current,allow_owner=True) as target:
                if not await target.current(): raise ValueError('Shared feedback unavailable')
                check=_shared_feedback_current(request,target,auth,barrier,current)
                result=await asyncio.to_thread(list_annotations,repository,service,target,check)
                if not await target.current(check_revision=False): raise ValueError('Shared feedback unavailable')
                return _shared_feedback_public(result,auth,account)
        except Exception as exc: raise HTTPException(409,'Shared feedback unavailable or changed') from exc

    @router.post('/shared-feedback/save')
    async def shared_feedback_save(request: Request, response: Response):
        from src.openclank.shared_resource_authority import recipient_live_target
        from src.openclank.shared_feedback import capture_source, save_annotation
        response.headers['Cache-Control']='private, no-store'
        account,auth,barrier,current,provider_for_owner,service=_shared_text_authority(request)
        body=await _shared_text_request_body(request,SharedFeedbackSaveRequest,maximum=16384)
        try:
            async with recipient_live_target(service=service,publication_id=body.publication_id,recipient_account=account,
                    auth=auth,barrier=barrier,store=repository,provider_for_owner=provider_for_owner,original_current=current) as target:
                if target.role not in {'commenter','editor'}: raise ValueError('Shared feedback unavailable')
                source,source_current=await capture_source(target,body.anchor,body.expected_revision)
                if not await target.current(): raise ValueError('Shared feedback unavailable')
                check=_shared_feedback_current(request,target,auth,barrier,current,source_current)
                task=asyncio.create_task(asyncio.to_thread(save_annotation,repository,service,target,check,request_key=body.request_key,
                    text=body.text,anchor=body.anchor,source=source,expected_revision=body.annotation_revision,
                    feedback_id=body.feedback_id,comment_revision=body.comment_revision))
                try: result=await asyncio.shield(task)
                except asyncio.CancelledError:
                    while not task.done():
                        try: await asyncio.shield(task)
                        except asyncio.CancelledError: continue
                        except Exception: break
                    if not task.cancelled():
                        try: task.result()
                        except Exception: pass
                    raise
                if not await target.current(check_revision=False): raise ValueError('Shared feedback outcome needs review')
                return _shared_feedback_public(result,auth,account)
        except Exception as exc: raise HTTPException(409,'Shared feedback unavailable or changed') from exc

    @router.post('/shared-image/read')
    async def shared_image_read(request: Request, response: Response):
        import base64
        from src.openclank.shared_resource_authority import recipient_live_target
        from src.openclank.shared_feedback import capture_source
        response.headers['Cache-Control']='private, no-store'
        account,auth,barrier,current,provider_for_owner,service=_shared_text_authority(request)
        body=await _shared_text_request_body(request,SharedTextReadRequest,maximum=4096)
        try:
            async with recipient_live_target(service=service,publication_id=body.publication_id,recipient_account=account,
                    auth=auth,barrier=barrier,store=repository,provider_for_owner=provider_for_owner,original_current=current) as target:
                source,source_current,info,data=await capture_source(target,{'kind':'image','x':0,'y':0,'width':1,'height':1},None,image_read=True)
                if not source_current() or not await target.current() or not source_current(): raise ValueError('Shared image changed')
                return {'version':1,'resource_id':target.resource['resource_id'],'role':target.role,
                        'revision':source['revision'],'sha256':source['sha256'],**info,
                        'bytes':base64.b64encode(data).decode('ascii')}
        except Exception as exc: raise HTTPException(409,'Shared image unavailable or changed') from exc

    @router.post('/shared-text/read')
    async def shared_text_read(request: Request, response: Response):
        response.headers['Cache-Control']='private, no-store'
        account,core=_shared_text_core(request)
        body=await _shared_text_request_body(request,SharedTextReadRequest,maximum=4096)
        try:return await core.read(recipient_account=account,publication_id=body.publication_id)
        except Exception as exc:raise HTTPException(409,'Shared text unavailable or changed') from exc

    @router.post('/shared-text/commit')
    async def shared_text_commit(request: Request, response: Response):
        response.headers['Cache-Control']='private, no-store'
        account,core=_shared_text_core(request)
        body=await _shared_text_request_body(request,SharedTextCommitRequest,maximum=4*1024*1024)
        try:
            return await core.commit(recipient_account=account,publication_id=body.publication_id,operation_id=body.operation_id,
                base_text=body.base_text,base_sha256=body.base_sha256,edits=body.edits,client_digest=body.client_digest)
        except Exception as exc:raise HTTPException(409,'Shared edit unavailable or changed') from exc

    @router.post('/shared-text/status')
    async def shared_text_status(request: Request, response: Response):
        response.headers['Cache-Control']='private, no-store'
        account,core=_shared_text_core(request)
        body=await _shared_text_request_body(request,SharedTextStatusRequest,maximum=4096)
        try:return await core.status(recipient_account=account,publication_id=body.publication_id,operation_id=body.operation_id,client_digest=body.client_digest)
        except Exception as exc:raise HTTPException(409,'Shared status unavailable or changed') from exc

    def _shared_workbook_core(request):
        from src.openclank.shared_workbook_service import SharedWorkbookService
        account,auth,barrier,current,provider_for_owner,service=_shared_text_authority(request)
        return account,SharedWorkbookService(service=service,auth=auth,barrier=barrier,store=repository,
            provider_for_owner=provider_for_owner,original_current=current)

    @router.post('/shared-workbook/read')
    async def shared_workbook_read(request: Request, response: Response):
        response.headers['Cache-Control']='private, no-store'
        account,core=_shared_workbook_core(request)
        body=await _shared_text_request_body(request,SharedTextReadRequest,maximum=4096)
        try:return await core.read(recipient_account=account,publication_id=body.publication_id)
        except Exception as exc:raise HTTPException(409,'Shared workbook unavailable or changed') from exc

    @router.post('/shared-workbook/commit')
    async def shared_workbook_commit(request: Request, response: Response):
        response.headers['Cache-Control']='private, no-store'
        account,core=_shared_workbook_core(request)
        body=await _shared_text_request_body(request,SharedWorkbookCommitRequest,maximum=4*1024*1024)
        try:
            return await core.commit(recipient_account=account,publication_id=body.publication_id,operation_id=body.operation_id,
                base_text=body.base_text,base_sha256=body.base_sha256,cells=body.cells,client_digest=body.client_digest)
        except Exception as exc:raise HTTPException(409,'Shared workbook edit unavailable or changed') from exc

    @router.post('/shared-workbook/status')
    async def shared_workbook_status(request: Request, response: Response):
        response.headers['Cache-Control']='private, no-store'
        account,core=_shared_workbook_core(request)
        body=await _shared_text_request_body(request,SharedTextStatusRequest,maximum=4096)
        try:return await core.status(recipient_account=account,publication_id=body.publication_id,operation_id=body.operation_id,client_digest=body.client_digest)
        except Exception as exc:raise HTTPException(409,'Shared workbook status unavailable or changed') from exc

    def _shared_slides_core(request):
        from src.openclank.shared_slides_service import SharedSlidesService
        account,auth,barrier,current,provider_for_owner,service=_shared_text_authority(request)
        return account,SharedSlidesService(service=service,auth=auth,barrier=barrier,store=repository,
            provider_for_owner=provider_for_owner,original_current=current)

    @router.post('/shared-slides/read')
    async def shared_slides_read(request: Request, response: Response):
        response.headers['Cache-Control']='private, no-store'
        account,core=_shared_slides_core(request)
        body=await _shared_text_request_body(request,SharedTextReadRequest,maximum=4096)
        try:return await core.read(recipient_account=account,publication_id=body.publication_id)
        except Exception as exc:raise HTTPException(409,'Shared slides unavailable or changed') from exc

    @router.post('/shared-slides/commit')
    async def shared_slides_commit(request: Request, response: Response):
        response.headers['Cache-Control']='private, no-store'
        account,core=_shared_slides_core(request)
        body=await _shared_text_request_body(request,SharedSlidesCommitRequest,maximum=4*1024*1024)
        try:
            return await core.commit(recipient_account=account,publication_id=body.publication_id,operation_id=body.operation_id,
                base_text=body.base_text,base_sha256=body.base_sha256,objects=body.objects,client_digest=body.client_digest)
        except Exception as exc:raise HTTPException(409,'Shared slides edit unavailable or changed') from exc

    @router.post('/shared-slides/status')
    async def shared_slides_status(request: Request, response: Response):
        response.headers['Cache-Control']='private, no-store'
        account,core=_shared_slides_core(request)
        body=await _shared_text_request_body(request,SharedTextStatusRequest,maximum=4096)
        try:return await core.status(recipient_account=account,publication_id=body.publication_id,operation_id=body.operation_id,client_digest=body.client_digest)
        except Exception as exc:raise HTTPException(409,'Shared slides status unavailable or changed') from exc

    @router.post("/save-raster")
    async def save_raster(request: Request, resource_ref: str = Query(min_length=1, max_length=8192),
                          expected_fingerprint: str = Query(pattern=r"^sha256:[a-f0-9]{64}:[0-9]{1,16}$")):
        import io
        from PIL import Image

        formats = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP", "image/gif": "GIF"}
        mime = request.headers.get("content-type", "").lower()
        if mime not in formats or request.headers.get("content-encoding", "identity").lower() != "identity":
            raise HTTPException(415, "Unsupported image format")
        ctx = context_for_ref(request, resource_ref)
        files = facade(request)
        try:
            provider, ref = files._provider_for_ref(ctx, resource_ref, capability="write")
            if ref.provider != "host":
                raise FilesFacadeError("Host image is unavailable", code="resource_unavailable")
            data = bytearray()
            async with asyncio.timeout(30):
                async for chunk in request.stream():
                    if len(data) + len(chunk) > 32 * 1024 * 1024:
                        raise HTTPException(413, "Encoded image exceeds the Files write limit")
                    data.extend(chunk)

            def validate():
                with Image.open(io.BytesIO(data), formats=[formats[mime]]) as image:
                    if max(image.size) > 8192 or image.width * image.height > 40_000_000:
                        raise ValueError("image exceeds pixel limit")
                    image.load()

            try:
                await asyncio.to_thread(validate)
            except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
                raise HTTPException(422, "Invalid or oversized raster image") from None
            return await provider.save_raster(ctx, origin_id=ref.origin_id, expected=expected_fingerprint,
                                              media_type=mime, content=bytes(data))
        except TimeoutError:
            raise HTTPException(408, "Image upload timed out") from None
        except FilesFacadeError as error:
            if error.code == "upload_too_large":
                raise HTTPException(413, str(error)) from error
            _raise(error)

    @router.post("/save-resource")
    async def save_resource(body: SaveResourceRequest, request: Request):
        try:
            return await facade(request).save_resource(
                context_for_ref(request, body.resource_ref),
                resource_ref=body.resource_ref,
                expected_revision=body.expected_revision.model_dump(),
                text=body.text,
            )
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/create")
    async def create_resource(body: CreateResourceRequest, request: Request):
        try:
            return await facade(request).create(
                context(request),
                parent_ref=body.parent_ref,
                name=body.name,
                text=body.text,
                action_id=body.action_id,
                operation_id=body.operation_id,
                expected_parent_revision=body.expected_parent_revision,
                status_only=body.status_only,
            )
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/create-directory")
    async def create_directory(body: CreateDirectoryRequest, request: Request):
        try:
            return await facade(request).create_directory(
                context_for_ref(request, body.parent_ref),
                parent_ref=body.parent_ref,
                name=body.name,
                operation_id=body.operation_id,
                generation=body.generation,
                expected_revision=body.expected_revision.model_dump() if body.expected_revision else None,
                collision=body.collision,
            )
        except FilesFacadeError as error:
            _raise(error)

    @router.post('/analysis/projects/initialize')
    async def initialize_analysis_project(request: Request, response: Response):
        response.headers['Cache-Control'] = 'private, no-store'
        _media_human(request)
        body = await _analysis_request_body(request, AnalysisInitializeRequest)
        try:
            service = _analysis_projects(request, body.workspaceID)
            return await service.initialize(body.operationID)
        except FilesFacadeError as error:
            _raise(error)
        except (ValueError, UnicodeError):
            raise HTTPException(409, 'Analysis project initialization is unavailable')

    @router.post('/analysis/projects/preview')
    async def preview_analysis_project(request: Request, response: Response):
        response.headers['Cache-Control'] = 'private, no-store'
        _media_human(request)
        body = await _analysis_request_body(request, AnalysisPreviewRequest)
        try:
            service = _analysis_projects(request, body.workspaceID)
            return await service.preview(body.sourceRef, body.annotationID)
        except FilesFacadeError as error:
            _raise(error)
        except (ValueError, UnicodeError):
            raise HTTPException(409, 'Analysis preview is unavailable or stale')

    @router.post('/analysis/projects/observation')
    async def save_analysis_observation(request: Request, response: Response):
        from src.openclank.analysis_projects import AnalysisPreDispatchRefusal
        response.headers['Cache-Control'] = 'private, no-store'
        _media_human(request)
        body = await _analysis_request_body(request, AnalysisObservationRequest)
        try:
            try:
                service = _analysis_projects(request, body.workspaceID)
            except HTTPException as error:
                if error.status_code == 409:
                    raise AnalysisPreDispatchRefusal('Analysis workspace was refused before dispatch') from error
                raise
            return await service.save_observation(body.model_dump(exclude_unset=True))
        except FilesFacadeError as error:
            _raise(error)
        except AnalysisPreDispatchRefusal:
            raise HTTPException(409, {'code':'analysis_pre_dispatch_refused', 'message':'Analysis observation was refused before dispatch'})
        except (ValueError, UnicodeError):
            raise HTTPException(409, 'Analysis observation is unavailable or stale')

    @router.get('/analysis/snapshots/capabilities')
    async def analysis_snapshot_capabilities(request: Request, response: Response):
        from src.openclank.analysis_snapshot_capture import DEFAULT_LIMITS
        from src.openclank.analysis_snapshot_contract import MAX_FILE_BYTES,MAX_TOTAL_BYTES
        response.headers['Cache-Control']='private, no-store';_media_human(request)
        from src.openclank.analysis_adapters.ingest import qualified_adapter_ids
        return {'version':1,'defaultLimits':dict(DEFAULT_LIMITS),'scope':['folder','selected'],
            'maximumFileBytes':MAX_FILE_BYTES,'maximumTotalBytes':MAX_TOTAL_BYTES,
            'coverage':'observed_interval','readiness':'linux_files_service_required',
            'ingestAdapters':list(qualified_adapter_ids())}   # the Trace overlay is offered only when this is non-empty

    @router.post('/analysis/snapshots/preview')
    async def analysis_snapshot_preview(request: Request, response: Response):
        from src.openclank.account_request_barrier import UtilityLifetime
        from src.openclank.analysis_snapshot_capture import capture_snapshot,seal_snapshot_preview
        from src.openclank.analysis_snapshot_contract import encode_manifest
        from src.openclank.analysis_snapshot_upload import create_snapshot_upload
        response.headers['Cache-Control']='private, no-store';account,owner=_media_human(request)
        body=await _analysis_request_body(request,AnalysisSnapshotCaptureRequest)
        state=request.app.state;auth=getattr(state,'auth_manager',None);barrier=getattr(state,'account_request_barrier',None);handler=getattr(state,'upload_handler',None)
        if not auth or not barrier or not handler:raise HTTPException(503,'Snapshot authority unavailable')
        request_hash=hashlib.sha256(json.dumps(body.model_dump(),ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8','strict')).hexdigest()
        async def drain(task):
            try:return await asyncio.shield(task)
            except asyncio.CancelledError:
                while not task.done():
                    try:await asyncio.shield(task)
                    except asyncio.CancelledError:continue
                    except Exception:break
                if not task.cancelled():
                    try:task.result()
                    except Exception:pass
                raise
        async def owned():
            if auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner:raise ValueError('account changed')
            admission=await barrier.acquire(owner)
            utility=UtilityLifetime(barrier,admission,owner,account,'analysis-snapshot-capture',uuid.uuid4().hex,request_hash,auth)
            try:
                await barrier.bind_utility(admission,utility);utility.task=asyncio.current_task()
                project=_analysis_projects(request,body.workspaceID,read_only=True);_analysis_original(request,account,owner,project)
                async def current():
                    return utility.account_current() and not utility.cancel_requested and await barrier.is_live(owner,admission)
                if not await current():raise ValueError('account unavailable')
                capture=asyncio.create_task(capture_snapshot(project=project,repository=repository,root_ref=body.rootRef,
                    root_revision=body.rootRevision,mode=body.mode,selected_refs=body.selectedRefs,dossier_id=body.dossierID,
                    dossier_revision=body.expectedDossierRevision,source_ref=body.sourceRef,limits=body.limits,current=current))
                manifest=await drain(capture)
                if manifest['account_id']!=account or not await current():raise ValueError('account changed')
                raw=encode_manifest(manifest)
                upload=asyncio.create_task(asyncio.to_thread(create_snapshot_upload,handler,authority=utility,canonical_bytes=raw))
                reference=await drain(upload)
                if not await current():raise ValueError('account changed')
                token=seal_snapshot_preview(utility,reference,manifest)
                if not await current():raise ValueError('account changed')
                return {'version':1,'state':'ready','previewToken':token,'reference':reference,
                    'preview':{'coverage':reference['coverage'],'componentCount':reference['component_count'],
                        'components':manifest['components'][:40],'shown':min(40,len(manifest['components'])),
                        'limits':manifest['limits'],'captureInterval':manifest['capture_interval'],
                        'sourceState':'captured_interval','interpretationsSelected':0,'retention':'temporary_until_publication'}}
            finally:
                utility.settled=True
                await barrier.release(admission)
        task=asyncio.create_task(owned())
        try:return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(lambda done:done.exception() if not done.cancelled() else None)
            raise
        except Exception as exc:raise HTTPException(409,'Snapshot preview is unavailable or stale') from exc

    async def retain_published_analysis(request, owner, account, provider, memory_id, selection, manifest, current):
        from src.openclank.memery_component_history import retain_components
        body = AnalysisSnapshotCompareRequest.model_validate(selection)
        if body.workspaceID != manifest['workspace_id'] or body.memoryID or body.previewToken:
            raise ValueError('analysis folder unavailable')
        project = _analysis_projects(request, body.workspaceID, read_only=True)
        _analysis_original(request, account, owner, project)
        return await retain_components(project=project, repository=repository, root_ref=body.rootRef,
            root_revision=body.rootRevision, state=request.app.state, owner=owner, account=account,
            memory_id=memory_id, provider=provider, manifest=manifest, current=current)

    if application_state is not None:
        application_state.retain_published_analysis = retain_published_analysis

    @router.post('/analysis/snapshots/compare')
    async def analysis_snapshot_compare(request: Request, response: Response):
        from src.openclank.account_request_barrier import UtilityLifetime
        from src.openclank.analysis_snapshot_capture import capture_snapshot,compare_snapshots,open_snapshot_preview
        from src.openclank.analysis_snapshot_contract import decode_manifest
        from src.openclank.analysis_snapshot_upload import verify_snapshot_upload
        response.headers['Cache-Control']='private, no-store';account,owner=_media_human(request)
        from src.openclank.analysis_snapshot_host import snapshot_named_human,with_saved_snapshot
        body=await _analysis_request_body(request,AnalysisSnapshotCompareRequest)
        if (body.previewToken is None)==(body.memoryID is None):raise HTTPException(400,'Choose one snapshot')
        state=request.app.state;auth=getattr(state,'auth_manager',None);barrier=getattr(state,'account_request_barrier',None);handler=getattr(state,'upload_handler',None)
        if not auth or not barrier or not handler:raise HTTPException(503,'Snapshot authority unavailable')
        if body.memoryID is not None:
            if snapshot_named_human(request)!=owner:raise HTTPException(403,'Named user required')
            profile=getattr(state,'s05_profile',None);provider=getattr(profile,'provider',None)
            async def compare_saved(_utility,_reference,previous,current):
                project=_analysis_projects(request,body.workspaceID,read_only=True);_analysis_original(request,account,owner,project)
                observations={}
                async def current_files():
                    _analysis_original(request,account,owner,project)
                    return _utility.account_current() and not _utility.cancel_requested and await barrier.is_live(owner,_utility.admission)
                observed=await capture_snapshot(project=project,repository=repository,root_ref=body.rootRef,
                    root_revision=body.rootRevision,mode=previous['scope']['mode'],selected_refs=[],dossier_id='',
                    dossier_revision={},source_ref='',limits=previous['limits'],current=current_files,historical_manifest=previous,observations=observations)
                if not await current_files():raise ValueError('account changed')
                await current()
                return {'version':1,'state':'compared','comparison':compare_snapshots(previous,observed,observations=observations)}
            async def saved_owned():
                return await with_saved_snapshot(request=request,provider=provider,batch_store=None,user=owner,original_account=account,
                    memory_id=body.memoryID,human_current=lambda:snapshot_named_human(request),callback=compare_saved)
            task=asyncio.create_task(saved_owned())
            try:return await asyncio.shield(task)
            except asyncio.CancelledError:
                task.add_done_callback(lambda done:done.exception() if not done.cancelled() else None);raise
            except Exception as exc:raise HTTPException(409,'Snapshot comparison is unavailable or stale') from exc
        request_hash=hashlib.sha256(json.dumps(body.model_dump(),ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8','strict')).hexdigest()
        async def owned():
            if auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner:raise ValueError('account changed')
            admission=await barrier.acquire(owner)
            utility=UtilityLifetime(barrier,admission,owner,account,'analysis-snapshot-compare',uuid.uuid4().hex,request_hash,auth)
            try:
                await barrier.bind_utility(admission,utility);utility.task=asyncio.current_task()
                async def current():return utility.account_current() and not utility.cancel_requested and await barrier.is_live(owner,admission)
                if not await current():raise ValueError('account unavailable')
                sealed=open_snapshot_preview(utility,body.previewToken)
                if sealed['workspace']!=body.workspaceID:raise ValueError('workspace changed')
                raw=await asyncio.to_thread(verify_snapshot_upload,handler,authority=utility,reference=sealed['reference'])
                if not await current():raise ValueError('account changed')
                previous=decode_manifest(raw)
                if (previous['account_id']!=account or previous['workspace_id']!=sealed['workspace']
                    or previous['project_key']!=sealed['project'] or previous['root_identity']!=sealed['root']
                    or previous['capture_interval']!=sealed['interval']):raise ValueError('snapshot changed')
                project=_analysis_projects(request,body.workspaceID,read_only=True);_analysis_original(request,account,owner,project)
                observations={}
                observed=await capture_snapshot(project=project,repository=repository,root_ref=body.rootRef,
                    root_revision=body.rootRevision,mode=previous['scope']['mode'],selected_refs=[],dossier_id='',
                    dossier_revision={},source_ref='',limits=previous['limits'],current=current,historical_manifest=previous,observations=observations)
                if not await current():raise ValueError('account changed')
                return {'version':1,'state':'compared','comparison':compare_snapshots(previous,observed,observations=observations)}
            finally:
                utility.settled=True
                await barrier.release(admission)
        task=asyncio.create_task(owned())
        try:return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(lambda done:done.exception() if not done.cancelled() else None)
            raise
        except Exception as exc:raise HTTPException(409,'Snapshot comparison is unavailable or stale') from exc

    @router.post('/analysis/results/candidates')
    async def analysis_result_candidates(request: Request, response: Response):
        from src.openclank.analysis_result_receipts import open_receipt
        response.headers['Cache-Control']='private, no-store'
        account,owner=_media_human(request)
        body=await _analysis_request_body(request,AnalysisCandidatesRequest)
        barrier=getattr(request.app.state,'account_request_barrier',None)
        if barrier is None:raise HTTPException(503,'Analysis authority unavailable')
        try:
            async with barrier.admitted(owner):
                project=_analysis_projects(request,body.workspaceID,read_only=True)
                _analysis_original(request,account,owner,project)
                await project._check()
                _,source=project.facade._provider_for_ref(project.context,body.sourceRef,capability='download')
                if source.provider!='host':raise ValueError('source unavailable')
                entry=await project.host.stat_inspection(project.context,origin_id=source.origin_id)
                if entry.origin_id!=source.origin_id or entry.kind!='file':raise ValueError('source unavailable')
                supervisor=request.app.state.mimo_supervisor;pool=supervisor.owner_lifecycle_pool();worker=pool.worker_for_owner(owner)
                bridge=getattr(worker,'_bridge',None);native=bridge.mapped_session_id(body.stableChatID) if bridge else None
                workspace=bridge.mapped_session_workspace(body.stableChatID) if bridge else None
                if not native or not workspace:raise ValueError('chat unavailable')
                key=pool._key(owner);epoch=pool._owner_lifecycle_epoch(key);runtime=pool._states.get(key)
                generation=getattr(runtime,'generation',None);process=getattr(worker,'_proc',None);holder=getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)
                if not process or not holder or getattr(bridge,'_managed_provider_generation',None)!=holder:raise ValueError('chat unavailable')
                binding=await bridge._handle_session_binding_read({'sessionID':native})
                if binding.get('owner')!=owner or binding.get('stableChatID')!=body.stableChatID or binding.get('authorityWorkspaceID')!=project.identity['workspaceID']:raise ValueError('chat unavailable')
                payload={'stableChatID':body.stableChatID,**({'cursor':body.cursor} if body.cursor else {})}
                page=await pool.session_native_analysis_candidates(body.stableChatID,native,workspace,payload,owner=owner)
                if page.status_code!=200 or len(page.content)>65536:raise ValueError('candidates unavailable')
                value=page.json()
                if (type(value) is not dict or set(value)-{'items','partial','cursor'} or type(value.get('items')) is not list or len(value['items'])>20 or type(value.get('partial')) is not bool
                    or (value['partial'] and (not isinstance(value.get('cursor'),str) or not 0<len(value['cursor'])<=1024))
                    or (not value['partial'] and 'cursor' in value)):raise ValueError('candidates unavailable')
                items=[]
                for item in value['items']:
                    try:
                        if type(item) is not dict or set(item)!={'messageID','callID','operation','provisional','actorID','rootOperationID','innerTurnID','executionRevision','inputSHA256'}:continue
                        facts=open_receipt(item['provisional'],kind='provisional')
                        if (facts['account']!=account or facts['workspace']!=project.identity['workspaceID'] or facts['chat']!=body.stableChatID or facts['nativeSession']!=native or facts['holder']!=holder or facts['sourceID']!=source.stable_id or facts['message']!=item['messageID'] or facts['call']!=item['callID'] or facts['actor']!=item['actorID'] or facts['root']!=item['rootOperationID'] or facts['turn']!=item['innerTurnID'] or facts['executionRevision']!=item['executionRevision'] or facts['inputSHA']!=item['inputSHA256'] or facts['operation']!=item['operation']):continue
                        items.append(item)
                    except (ValueError,TypeError,KeyError):continue
                await project._check()
                if (pool.worker_for_owner(owner) is not worker or bridge.mapped_session_id(body.stableChatID)!=native or bridge.mapped_session_workspace(body.stableChatID)!=workspace or await bridge._handle_session_binding_read({'sessionID':native})!=binding):raise ValueError('chat changed')
                latest=await project.host.stat_inspection(project.context,origin_id=source.origin_id)
                if latest.origin_id!=entry.origin_id or latest.kind!=entry.kind or latest.size!=entry.size or latest.revision!=entry.revision:raise ValueError('source changed')
                pool._assert_owner_lifecycle(key,epoch)
                if (pool._states.get(key) is not runtime or runtime.active is not worker or runtime.status!='ready' or runtime.generation!=generation or worker.installed_generation!=generation or worker._proc is not process or not worker.is_alive() or getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)!=holder):raise ValueError('holder changed')
                await project._check()
                _analysis_original(request,account,owner,project)
                return {'items':items,'partial':value['partial'],**({'cursor':value['cursor']} if isinstance(value.get('cursor'),str) else {})}
        except Exception:
            raise HTTPException(409,'Analysis candidates are unavailable')

    @router.post('/analysis/results/functions/read')
    async def read_analysis_function_export(request: Request, response: Response):
        response.headers['Cache-Control']='private, no-store';account,owner=_media_human(request)
        body=await _analysis_request_body(request,AnalysisFunctionExportReadRequest)
        try:
            project=_analysis_projects(request,body.workspaceID,read_only=True);_analysis_original(request,account,owner,project)
            result=await project.read_function_export(body.exportID);_analysis_original(request,account,owner,project)
            return result
        except FilesFacadeError as error:_raise(error)
        except (ValueError,UnicodeError):raise HTTPException(409,'Function export is unavailable')

    @router.post('/analysis/results/functions/export')
    async def export_analysis_functions(request: Request, response: Response):
        response.headers['Cache-Control']='private, no-store';account,owner=_media_human(request)
        body=await _analysis_request_body(request,AnalysisFunctionExportRequest)
        project=_analysis_projects(request,body.workspaceID);barrier=getattr(request.app.state,'account_request_barrier',None)
        if barrier is None:raise HTTPException(503,'Analysis authority unavailable')
        async def owned():
            async with barrier.admitted(owner):
                _analysis_original(request,account,owner,project)
                return await project.export_provider_functions(body.model_dump(exclude_unset=True))
        task=asyncio.create_task(owned())
        try:return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(lambda done:done.exception() if not done.cancelled() else None)
            raise
        except FilesFacadeError as error:_raise(error)
        except (ValueError,UnicodeError):raise HTTPException(409,'Function export is unavailable or stale')

    @router.post('/analysis/results/dossier/static-overlay')
    async def analysis_static_overlay(request: Request, response: Response):
        response.headers['Cache-Control']='private, no-store';account,owner=_media_human(request)
        body=await _analysis_request_body(request,AnalysisStaticOverlayRequest)
        try:
            project=_analysis_projects(request,body.workspaceID,read_only=True);_analysis_original(request,account,owner,project)
            result=await project.static_functions_overlay(body.dossierID,body.expectedDossierRevision,body.sourceRef)
            _analysis_original(request,account,owner,project)
            return result
        except FilesFacadeError as error:_raise(error)
        except (ValueError,UnicodeError):raise HTTPException(409,'Static function overlay is unavailable or stale')

    @router.post('/analysis/ingest/save')
    async def save_analysis_ingest(request: Request, response: Response):
        response.headers['Cache-Control']='private, no-store';account,owner=_media_human(request)
        body=await _analysis_request_body(request,AnalysisIngestSaveRequest,limit=1_572_864)
        service=_analysis_projects(request,body.workspaceID);barrier=getattr(request.app.state,'account_request_barrier',None)
        if barrier is None:raise HTTPException(503,'Analysis authority unavailable')
        payload=body.model_dump(exclude_unset=True);payload['data']=_analysis_ingest_bytes(payload.pop('data_b64'))
        async def owned():
            async with barrier.admitted(owner):
                _analysis_original(request,account,owner,service)
                return await service.save_ingest(payload)
        task=asyncio.create_task(owned())
        try:return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(lambda done:done.exception() if not done.cancelled() else None)
            raise
        except FilesFacadeError as error:_raise(error)
        except (ValueError,UnicodeError):raise HTTPException(409,'Analysis ingest is unavailable or stale')

    @router.post('/analysis/ingest/overlay')
    async def analysis_ingest_overlay(request: Request, response: Response):
        from src.openclank.analysis_ingest import trace_overlay
        response.headers['Cache-Control']='private, no-store';account,owner=_media_human(request)
        body=await _analysis_request_body(request,AnalysisIngestOverlayRequest)
        try:
            project=_analysis_projects(request,body.workspaceID,read_only=True);_analysis_original(request,account,owner,project)
            result=await project.read_ingest(body.ingestID,body.sourceRef)
            _analysis_original(request,account,owner,project)
            return trace_overlay(result['result'])  # carries read_ingest's sourceState
        except FilesFacadeError as error:_raise(error)
        except (ValueError,UnicodeError):raise HTTPException(409,'Analysis ingest is unavailable or unverifiable')

    @router.post('/analysis/results/dossier/function')
    async def inspect_analysis_function(request: Request, response: Response):
        response.headers['Cache-Control']='private, no-store'
        account,owner=_media_human(request)
        body=await _analysis_request_body(request,AnalysisFunctionRequest)
        try:
            project=_analysis_projects(request,body.workspaceID,read_only=True);_analysis_original(request,account,owner,project)
            result=await project.inspect_provider_function(body.dossierID,body.expectedDossierRevision,body.selector,body.sourceRef)
            _analysis_original(request,account,owner,project)
            return result
        except FilesFacadeError as error:_raise(error)
        except (ValueError,UnicodeError):raise HTTPException(409,'Analysis function is unavailable or ambiguous')

    @router.post('/analysis/results/dossier/read')
    async def read_analysis_dossier(request: Request, response: Response):
        response.headers['Cache-Control']='private, no-store'
        account,owner=_media_human(request)
        body=await _analysis_request_body(request,AnalysisDossierReadRequest)
        try:
            project=_analysis_projects(request,body.workspaceID,read_only=True);_analysis_original(request,account,owner,project)
            result=await project.read_provider_dossier(body.dossierID,body.sourceRef)
            _analysis_original(request,account,owner,project)
            return result
        except FilesFacadeError as error:_raise(error)
        except (ValueError,UnicodeError):raise HTTPException(409,'Analysis dossier is unavailable or unverifiable')

    @router.post('/analysis/results/dossier/elf')
    async def save_analysis_elf_dossier(request: Request, response: Response):
        from src.openclank.analysis_projects import AnalysisPreDispatchRefusal
        response.headers['Cache-Control']='private, no-store'
        account,owner=_media_human(request)
        body=await _analysis_request_body(request,AnalysisElfSaveRequest)
        service=_analysis_projects(request,body.workspaceID)
        barrier=getattr(request.app.state,'account_request_barrier',None)
        if barrier is None: raise HTTPException(503,'Analysis dossier authority unavailable')
        async def owned():
            async with barrier.admitted(owner):
                _analysis_original(request,account,owner,service)
                return await service.save_host_elf_dossier(body.model_dump(exclude_unset=True))
        task=asyncio.create_task(owned())
        try:return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(lambda done:done.exception() if not done.cancelled() else None)
            raise
        except FilesFacadeError as error:_raise(error)
        except AnalysisPreDispatchRefusal: raise HTTPException(409,{'code':'analysis_pre_dispatch_refused','message':'ELF inspection was refused before dispatch'})
        except (ValueError,UnicodeError):raise HTTPException(409,'ELF dossier is unavailable or stale')

    @router.post('/analysis/results/dossier/save')
    async def save_analysis_dossier(request: Request, response: Response):
        response.headers['Cache-Control']='private, no-store'
        account,owner=_media_human(request)
        body=await _analysis_request_body(request,AnalysisDossierSaveRequest)
        service=_analysis_projects(request,body.workspaceID)
        barrier=getattr(request.app.state,'account_request_barrier',None)
        store=getattr(getattr(request.app.state,'account_owner_lifecycle',None),'artifact_store',None)
        if barrier is None or store is None:raise HTTPException(503,'Analysis dossier authority unavailable')
        async def owned():
            async with barrier.admitted(owner):
                _analysis_original(request,account,owner,service)
                return await service.save_provider_dossier(body.model_dump(exclude_unset=True),store)
        task=asyncio.create_task(owned())
        try:return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(lambda done:done.exception() if not done.cancelled() else None)
            raise
        except FilesFacadeError as error:_raise(error)
        except (ValueError,UnicodeError):raise HTTPException(409,'Analysis dossier is unavailable or stale')

    @router.post('/analysis/results/complete')
    async def complete_analysis_result(request: Request, response: Response):
        from src.openclank.analysis_result_receipts import open_receipt, complete_provisional
        from src.openclank.analysis_adapters.base import capture_source, source_current
        from src.openclank.analysis_projects import resolve_project
        response.headers['Cache-Control'] = 'private, no-store'
        account, owner = _media_human(request)
        body = await _analysis_request_body(request, AnalysisCompletedResultRequest)
        try:
            descriptor=open_receipt(body.provisional,kind='provisional')
            if descriptor['account'] != account: raise ValueError('account changed')
            state=request.app.state
            auth=getattr(state,'auth_manager',None); barrier=getattr(state,'account_request_barrier',None)
            supervisor=getattr(state,'mimo_supervisor',None)
            artifacts=getattr(getattr(state,'account_owner_lifecycle',None),'artifact_store',None)
            if not all((auth,barrier,supervisor,artifacts)): raise ValueError('authority unavailable')
            binding, project=resolve_project(repository,workspace_id=descriptor['workspace'],owner_username=owner,account_id=account,auth_manager=auth)
            if project['workspaceID'] != descriptor['workspace']: raise ValueError('workspace changed')
            ctx=context(request,workspace=descriptor['workspace']); files=facade(request,copal_workspace=descriptor['workspace'])
            _analysis_original(request,account,owner)
            if ctx.owner_subject_id!=account: raise ValueError('account changed')
            pool=supervisor.owner_lifecycle_pool(); owner_key=pool._key(owner)
            runtime_state=pool._states.get(owner_key); worker=pool.worker_for_owner(owner)
            owner_epoch=pool._owner_lifecycle_epoch(owner_key)
            generation=getattr(runtime_state,'generation',None); process=getattr(worker,'_proc',None)
            bridge=getattr(worker,'_bridge',None) if worker else None
            physical=bridge.mapped_session_workspace(descriptor['chat']) if bridge else None
            if not physical: raise ValueError('worker unavailable')
            captured_once=False
            async def current(value):
                try:
                    fresh,_=resolve_project(repository,workspace_id=value['workspace'],owner_username=owner,account_id=account,auth_manager=auth)
                    pool._assert_owner_lifecycle(owner_key,owner_epoch)
                    if (auth.account_id(owner)!=account or auth.username_for_account_id(account)!=owner
                        or fresh!=binding or repository.generation()!=ctx.policy_generation or pool.worker_for_owner(owner) is not worker
                        or pool._states.get(owner_key) is not runtime_state or runtime_state.active is not worker
                        or runtime_state.status!='ready' or runtime_state.generation!=generation
                        or worker.installed_generation!=generation or worker._proc is not process or process is None
                        or not worker.is_alive() or worker._bridge is not bridge
                        or getattr(getattr(worker,'_managed_callbacks',None),'holder_id',None)!=value['holder']
                        or bridge.mapped_session_id(value['chat'])!=value['nativeSession']
                        or bridge.mapped_session_workspace(value['chat'])!=physical): return False
                    mapped=await bridge._handle_session_binding_read({'sessionID':value['nativeSession']})
                    return (mapped.get('owner')==owner and mapped.get('stableChatID')==value['chat']
                        and mapped.get('authorityWorkspaceID')==value['workspace'])
                except Exception:return False
            async def source_valid(value):
                nonlocal captured_once
                try:
                    _, ref=files._provider_for_ref(ctx,body.sourceRef,capability='download')
                    if ref.provider!='host' or ref.stable_id!=value['sourceID']:return False
                    if not captured_once:
                        captured=await capture_source(files,ctx,body.sourceRef,value['sourceRevision'])
                        if captured.sha256!=value['sourceSHA']:return False
                        captured_once=True
                        return True
                    return await source_current(files,ctx,body.sourceRef,value['sourceRevision'])
                except Exception:return False
            async def part_valid(value, token):
                try:
                    payload={'stableChatID':value['chat'],'messageID':value['message'],'actorID':value['actor'],
                        'rootOperationID':value['root'],'executionRevision':value['executionRevision'],'innerTurnID':value['turn'],
                        'callID':value['call'],'inputSHA256':value['inputSHA'],'phase':'completed',
                        'outputSHA256':value['outputSHA'],'outputSize':value['size'],'provisional':token}
                    proof=await pool.session_native_analysis_proof(value['chat'],value['nativeSession'],physical,payload,owner=owner)
                    return proof.status_code==200 and proof.json()=={'state':'current'}
                except Exception:return False
            async def owned():
                async with barrier.admitted(owner):
                    _analysis_original(request,account,owner)
                    if ctx.owner_subject_id!=account:raise ValueError('account changed')
                    return await complete_provisional(token=body.provisional,account=account,store=artifacts,
                        current=current,source_current=source_valid,part_current=part_valid)
            task=asyncio.create_task(owned())
            try:return await asyncio.shield(task)
            except asyncio.CancelledError:
                task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
                raise
        except Exception:
            raise HTTPException(409, 'Completed analysis result is unavailable')

    @router.post("/workspace")
    async def workspace_from_resource(body: WorkspaceResourceRequest, request: Request):
        """Bind an opaque Host resource to the one canonical Workspace model."""

        owner = context(request)
        try:
            target = await facade(request).workspace_target(
                owner,
                resource_ref=body.resource_ref,
            )
            binding = bind_workspace_path(
                repository,
                actor_subject_id=owner.owner_subject_id,
                owner_subject_id=owner.owner_subject_id,
                is_admin=owner.is_admin,
                path=target.directory_path,
                purpose=body.purpose,
                name=target.name,
                media_adoption=lambda *, workspace_root, workspace_id, owner_subject_id: adopt_loose_media_for_workspace(
                    operation_store=repository,
                    owner_subject_id=owner_subject_id,
                    workspace_root=workspace_root,
                    workspace_id=workspace_id,
                    history_context=trusted_tool_context(
                        actor_id=owner_subject_id,
                        account_id=owner_subject_id,
                        workspace_id=workspace_id,
                        workspace_root=workspace_root,
                    ),
                ),
                media_receipt=getattr(getattr(request, "state", None), "history_capture", None),
            )
        except FilesFacadeError as error:
            _raise(error)
        except WorkspacePolicyServiceError as error:
            status = 403 if error.code == "workspace_denied" else 404 if error.code == "workspace_missing" else 400
            raise HTTPException(
                status,
                detail={"code": error.code, "message": str(error)},
            ) from error
        return {
            "version": 1,
            "generation": repository.generation(),
            # Deliberately omit the host path. Code/composer resolve this ID
            # through the existing purpose-bound Workspace endpoint.
            "workspace": workspace_view(binding, include_path=False),
            "open_relative": target.open_relative,
        }

    @router.post("/code-graph-target")
    async def code_graph_target(body: CodeGraphTargetRequest, request: Request):
        """Resolve explicit Graph navigation without adopting media or indexing.

        A captured project is checked rather than guessed. Otherwise a folder
        is its own root and a file uses its containing directory; the caller
        must present that proposed scope before submitting the file handoff.
        Only the normal app-folder catalog binding can be created here, never
        a chat workspace, People/Agent grant or derived index.
        """
        if getattr(request.state, "internal_tool_authenticated", False):
            raise HTTPException(403, "Code Graph navigation is app-only")
        owner = context(request)
        try:
            target = await facade(request).workspace_target(owner, resource_ref=body.resource_ref)
            if repository.generation() != owner.policy_generation:
                raise HTTPException(409, detail={"code": "resource_ref_stale", "message": "File access changed; select the resource again"})
            if body.workspace_id is not None:
                workspace = repository.get_workspace(body.workspace_id)
                if workspace.owner_subject_id != owner.owner_subject_id or workspace.archived:
                    raise HTTPException(404, "Workspace was not found")
                if workspace.revision != body.expected_workspace_revision:
                    raise HTTPException(409, detail={"code": "revision_conflict", "message": "Workspace changed; select it again"})
                binding = resolve_workspace_binding(
                    repository, workspace, subject_id=owner.owner_subject_id,
                    is_admin=owner.is_admin, purpose="app_folder",
                )
                root = Path(binding.path).resolve(strict=True)
                selected = Path(target.directory_path).joinpath(target.open_relative).resolve(strict=True)
                relative = selected.relative_to(root)
                # A directory selection cannot silently widen to an ancestor.
                if not target.open_relative and selected != root:
                    raise ValueError("Folder is not the captured Workspace root")
                open_relative = relative.as_posix() if target.open_relative else ""
                scope_kind = "captured_workspace"
            else:
                binding = bind_workspace_path(
                    repository, actor_subject_id=owner.owner_subject_id,
                    owner_subject_id=owner.owner_subject_id, is_admin=owner.is_admin,
                    path=target.directory_path, purpose="app_folder", name=target.name,
                    # Deliberately no media_adoption callback/history receipt.
                )
                open_relative = target.open_relative
                scope_kind = "parent_folder" if open_relative else "selected_folder"
        except FilesFacadeError as error:
            _raise(error)
        except WorkspacePolicyServiceError as error:
            raise HTTPException(403 if error.code == "workspace_denied" else 404,
                                detail={"code": error.code, "message": "Code Graph scope is unavailable"}) from error
        except (FilePolicyError, OSError, ValueError) as error:
            raise HTTPException(404, detail={"code": "resource_unavailable", "message": "Resource is not available in that Code Graph scope"}) from error
        return {
            "version": 1, "generation": repository.generation(),
            "workspace": workspace_view(binding, include_path=False),
            "open_relative": open_relative, "scope_kind": scope_kind,
        }

    def workspace_public(workspace) -> dict[str, Any]:
        return {
            "id": workspace.id,
            "name": workspace.name,
            "location_id": workspace.location_id,
            "relative_folder": workspace.relative_folder,
            "archived": workspace.archived,
            "generation": workspace.generation,
            "revision": workspace.revision,
        }

    @router.get("/workspaces")
    async def workspaces(request: Request, include_archived: bool = False):
        owner = context(request)
        nonlocal_facade = facade(request)
        rows = []
        for workspace in repository.list_workspaces(
            owner_subject_id=owner.owner_subject_id,
            include_archived=bool(include_archived),
        ):
            if workspace.archived:
                resource, availability = None, "archived"
            else:
                try:
                    binding = resolve_workspace_binding(
                        repository,
                        workspace,
                        subject_id=owner.owner_subject_id,
                        is_admin=owner.is_admin,
                        purpose="app_folder",
                    )
                    resource = await nonlocal_facade.host_resource_for_path(owner, path=binding.path)
                    availability = "available"
                except (WorkspacePolicyServiceError, FilesFacadeError) as error:
                    resource = None
                    availability = getattr(error, "code", "workspace_unavailable")
            rows.append({
                "workspace": workspace_public(workspace),
                "availability": availability,
                "resource": resource,
            })
        return {"version": 1, "generation": repository.generation(), "entries": rows}

    @router.patch("/workspaces/{workspace_id}")
    async def update_workspace(workspace_id: str, body: WorkspaceUpdateRequest, request: Request):
        if body.name is None and body.archived is None:
            raise HTTPException(422, "Workspace update is empty")
        owner = context(request)
        try:
            current = repository.get_workspace(workspace_id)
            if current.owner_subject_id != owner.owner_subject_id:
                raise HTTPException(404, "Workspace was not found")
            updated = repository.update_workspace(
                workspace_id,
                actor_subject_id=owner.owner_subject_id,
                name=body.name,
                archived=body.archived,
                expected_revision=body.expected_revision,
            )
        except FilePolicyError as error:
            status = 409 if error.code == "revision_conflict" else 404 if error.code.endswith("_not_found") else 400
            raise HTTPException(status, detail={"code": error.code, "message": str(error)}) from error
        close_all_clients()
        resource = None
        availability = "archived" if updated.archived else "available"
        if not updated.archived:
            try:
                binding = resolve_workspace_binding(
                    repository,
                    updated,
                    subject_id=owner.owner_subject_id,
                    is_admin=owner.is_admin,
                    purpose="app_folder",
                )
                resource = await facade(request).host_resource_for_path(owner, path=binding.path)
            except (WorkspacePolicyServiceError, FilesFacadeError) as error:
                availability = getattr(error, "code", "workspace_unavailable")
        return {
            "version": 1,
            "generation": repository.generation(),
            "workspace": workspace_public(updated),
            "availability": availability,
            "resource": resource,
        }

    @router.post("/workspace-resource")
    async def workspace_resource(body: WorkspacePathResourceRequest, request: Request):
        """Resolve a Workspace-relative target into opaque Host resources.

        Code and other Workspace-aware apps may know a compatibility path while
        they edit it, but the Files handoff carries only the stable Workspace ID
        and a relative name.  This endpoint rechecks current App authority,
        resolves symlinks before containment, and returns no host path.
        """

        owner = context(request)
        if body.capture_layout is not None:
            from src.openclank.capture_layout import capture_layout_directory
            state = request.app.state
            auth = getattr(state, 'auth_manager', None)
            barrier = getattr(state, 'account_request_barrier', None)
            token = request.cookies.get('open_clank_session')
            def current():
                try:
                    key = str(owner.owner_username).strip().lower()
                    return bool(auth and barrier and token and state.auth_manager is auth
                        and state.account_request_barrier is barrier
                        and request.cookies.get('open_clank_session') == token
                        and auth.get_username_for_token(token) == owner.owner_username
                        and auth.account_id(owner.owner_username) == owner.owner_subject_id
                        and auth.username_for_account_id(owner.owner_subject_id) == owner.owner_username
                        and not barrier._fenced(key)
                        and not (barrier._absolute_fence and barrier._absolute_fence(key))
                        and context(request) == owner and repository.generation() == owner.policy_generation)
                except Exception:
                    return False
            if not current(): raise HTTPException(403, 'Workspace authority unavailable')
            async with barrier.admitted(owner.owner_username):
                if not current(): raise HTTPException(409, 'Workspace authority changed')
                try:
                    workspace = repository.get_workspace(body.workspace_id)
                    if workspace.owner_subject_id != owner.owner_subject_id or workspace.archived:
                        return {"version": 1, "generation": owner.policy_generation, "resource": None}
                    binding = resolve_workspace_binding(repository, workspace,
                        subject_id=owner.owner_subject_id, is_admin=owner.is_admin, purpose="app_folder")
                    location = repository.get_location(workspace.location_id)
                    with capture_layout_directory(binding.path, body.capture_layout) as (path, still_pinned):
                        resource = await facade(request).host_resource_for_path(owner, path=path)
                        if not current(): raise HTTPException(409, 'Workspace authority changed')
                        current_workspace = repository.get_workspace(body.workspace_id)
                        current_location = repository.get_location(workspace.location_id)
                        current_binding = resolve_workspace_binding(repository, current_workspace,
                            subject_id=owner.owner_subject_id, is_admin=owner.is_admin, purpose="app_folder")
                        if current_workspace != workspace or current_location != location or current_binding.path != binding.path:
                            raise HTTPException(409, 'Workspace authority changed')
                        if not still_pinned(): resource = {}
                        caps = resource.get('capabilities') or []
                        if (resource.get('provider') != 'host' or resource.get('kind') != 'folder'
                                or not resource.get('ref') or not resource.get('revision')
                                or 'write' not in caps or 'children' not in caps):
                            resource = None
                except (OSError, FilePolicyError, WorkspacePolicyServiceError, FilesFacadeError):
                    if not current(): raise HTTPException(409, 'Workspace authority changed')
                    resource = None
                if not current(): raise HTTPException(409, 'Workspace authority changed')
                return {"version": 1, "generation": owner.policy_generation, "resource": resource}

        try:
            workspace = repository.get_workspace(body.workspace_id)
            if workspace.owner_subject_id != owner.owner_subject_id or workspace.archived:
                raise HTTPException(404, "Workspace was not found")
            binding = resolve_workspace_binding(repository, workspace,
                subject_id=owner.owner_subject_id, is_admin=owner.is_admin, purpose="app_folder")
        except FilePolicyError as error:
            raise HTTPException(404, "Workspace was not found") from error
        except WorkspacePolicyServiceError as error:
            status = 403 if error.code == "workspace_denied" else 404
            raise HTTPException(status, detail={"code": error.code, "message": "Workspace resource is unavailable"}) from error

        raw_relative = str(body.relative_path or "").replace("\\", "/")
        if "\x00" in raw_relative or raw_relative.startswith("/") or re.match(r"^[A-Za-z]:", raw_relative):
            raise HTTPException(422, "Workspace-relative path is invalid")
        if raw_relative in {"", "."}:
            parts: tuple[str, ...] = ()
        else:
            parts = tuple(PurePosixPath(raw_relative).parts)
            if (
                not parts
                or any(part in {"", ".", ".."} for part in parts)
                or "//" in raw_relative
                or raw_relative.endswith("/")
            ):
                raise HTTPException(422, "Workspace-relative path is invalid")

        try:
            base = Path(binding.path).resolve(strict=True)
            target_path = base.joinpath(*parts).resolve(strict=True)
            target_path.relative_to(base)
            target = await facade(request).host_resource_for_path(owner, path=str(target_path))
            parent_path = target_path if target.get("kind") == "folder" else target_path.parent
            parent_path.relative_to(base)
            parent = target if parent_path == target_path else await facade(request).host_resource_for_path(
                owner,
                path=str(parent_path),
            )
        except (FileNotFoundError, PermissionError, OSError, ValueError, FilesFacadeError) as error:
            raise HTTPException(404, "Workspace resource is unavailable") from error

        return {
            "version": 1,
            "generation": repository.generation(),
            "workspace": workspace_public(workspace),
            "parent": parent,
            "resource": target,
        }

    def preview_revision(value: str | None) -> Mapping[str, str] | None:
        if value is None:
            return None
        if len(value) > 1024:
            raise HTTPException(400, "Preview revision is invalid")
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "Preview revision is invalid") from exc
        if (not isinstance(parsed, dict) or set(parsed) != {"kind", "value"}
            or not all(isinstance(parsed[k], str) and parsed[k] for k in ("kind", "value"))):
            raise HTTPException(400, "Preview revision is invalid")
        return parsed

    @router.api_route("/content/{resource_ref}", methods=["GET", "HEAD"])
    async def content(
        resource_ref: str,
        request: Request,
        purpose: Literal["download", "preview"] = "download",
        expected_revision: str | None = None,
    ):
        return await content_response(resource_ref, request, purpose, expected_revision=preview_revision(expected_revision))

    async def content_response(resource_ref, request, purpose="download", *, source_context=None, expected_revision=None, access_guard=None):
        """Download one provider resource through its opaque canonical ref.

        This compatibility data plane opens provider-owned files once and keeps
        the descriptor for the response. Host files will use the admitted Rust/
        Tonic handle transport instead; no raw provider ID or path is accepted.
        """
        descriptor = None
        try:
            ctx = source_context or context_for_ref(request, resource_ref)
            files = facade(request, copal_workspace=ctx.workspace_id)
            source = await files.content(
                ctx,
                resource_ref=resource_ref,
                capability=purpose,
                **({"expected_revision": expected_revision} if expected_revision is not None else {}),
            )
            policy_guard = _generation_guard(ctx.policy_generation)
            guard = lambda: policy_guard() and (access_guard is None or access_guard())
            if expected_revision is not None:
                current_resource = await files.stat(ctx, resource_ref=resource_ref)
                if current_resource.get("revision") != expected_revision:
                    raise FilesFacadeError("Submitted source changed; its saved evidence remains preserved", code="resource_changed")
            if not guard():
                raise FilesFacadeError("Submitted evidence access was revoked", code="permission_denied")
            if source.path is not None:
                descriptor, current = _open_path_content(source)
                size = int(current.st_size)
                modified_ms = int(current.st_mtime_ns // 1_000_000)
            elif source.stream is not None:
                if source.size is None or int(source.size) < 0:
                    raise FilesFacadeError("stream content size is unavailable", code="provider_unavailable")
                size = int(source.size)
                modified_ms = source.modified_unix_ms
            else:
                size = len(source.data or b"")
                modified_ms = source.modified_unix_ms

            etag_value = str(source.etag or "").strip().strip('"')
            if not etag_value:
                identity = source.expected_identity or (0, 0, size, int((modified_ms or 0) * 1_000_000))
                etag_value = "-".join(str(value) for value in identity)
            etag = f'"{etag_value}"'
            range_header = request.headers.get("range")
            if_range = request.headers.get("if-range")
            try:
                selected = None if (if_range and if_range.strip() != etag) else _range(range_header, size)
            except FilesFacadeError as error:
                if descriptor is not None:
                    os.close(descriptor)
                    descriptor = None
                raise HTTPException(
                    416,
                    detail={"code": error.code, "message": str(error)},
                    headers={"Accept-Ranges": "bytes", "Content-Range": f"bytes */{size}"},
                ) from error
            start, end = selected if selected is not None else (0, max(0, size - 1))
            response_size = 0 if size == 0 else end - start + 1
            ascii_name, encoded_name = _download_filename(source.filename)
            declared_type = str(source.media_type or "application/octet-stream").split(";", 1)[0].strip().lower()
            # SVG is inert in an image destination. Permit this one preview
            # presentation after opaque-ref authorization; downloads and active
            # document/frame/fetch destinations retain attachment treatment.
            image_svg_preview = (
                purpose == "preview"
                and declared_type == "image/svg+xml"
                and request.headers.get("sec-fetch-dest", "").lower() == "image"
            )
            active_type = declared_type in _ACTIVE_DOWNLOAD_TYPES and not image_svg_preview
            media_type = "application/octet-stream" if active_type else source.media_type
            disposition = "inline" if purpose == "preview" and not active_type else "attachment"
            headers = {
                "Content-Disposition": f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded_name}",
                "Content-Length": str(response_size),
                "Accept-Ranges": "bytes",
                "ETag": etag,
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, no-store",
                "Content-Security-Policy": "default-src 'none'; sandbox",
            }
            evidence = source.preview_evidence
            if evidence is not None:
                for field, header in (("source_sha256", "X-OpenClank-Source-SHA256"), ("decoder", "X-OpenClank-Decoder"),
                                      ("variant", "X-OpenClank-Variant"), ("revision_kind", "X-OpenClank-Revision-Kind"),
                                      ("revision_value", "X-OpenClank-Revision-Value")):
                    value = evidence.get(field)
                    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9:._-]{1,256}", value):
                        raise FilesFacadeError("Invalid derived preview evidence", code="provider_unavailable")
                    headers[header] = value
            if modified_ms is not None:
                headers["Last-Modified"] = email.utils.formatdate(modified_ms / 1000, usegmt=True)
            status_code = 206 if selected is not None else 200
            if selected is not None:
                headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            if request.method == "HEAD":
                if descriptor is not None:
                    os.close(descriptor)
                return Response(status_code=status_code, media_type=media_type, headers=headers)

            if source.stream is not None:
                async def guarded_stream():
                    async for chunk in source.stream(start, response_size):
                        if not guard():
                            raise _policy_aborted()
                        yield chunk

                body = guarded_stream()
            elif descriptor is not None:
                def path_chunks():
                    remaining = response_size
                    try:
                        with os.fdopen(descriptor, "rb", closefd=True) as handle:
                            handle.seek(start)
                            while remaining > 0:
                                if not guard():
                                    raise _policy_aborted()
                                chunk = handle.read(min(256 * 1024, remaining))
                                if not chunk:
                                    break
                                remaining -= len(chunk)
                                yield chunk
                    finally:
                        try:
                            os.close(descriptor)
                        except OSError:
                            pass

                body = path_chunks()
            else:
                data = source.data or b""

                async def data_chunks():
                    for offset in range(start, start + response_size, 256 * 1024):
                        if not guard():
                            raise _policy_aborted()
                        yield data[offset:min(start + response_size, offset + 256 * 1024)]

                body = data_chunks()
            return StreamingResponse(body, status_code=status_code, media_type=media_type, headers=headers)
        except FilesFacadeError as error:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if error.code == "invalid_range":
                # Do not disclose a resource's size unless its owner-bound ref
                # already resolved; the generic 416 body remains machine readable.
                raise HTTPException(416, detail={"code": error.code, "message": str(error)}) from error
            _raise(error)

    @router.get("/thumbnail/{resource_ref}")
    async def thumbnail(
        resource_ref: str,
        request: Request,
        width: int = Query(default=160, ge=1, le=1024),
        height: int = Query(default=160, ge=1, le=1024),
        scale: float = Query(default=1.0, ge=1.0, le=3.0),
        icon: bool = Query(default=False),
        expected_revision: str | None = None,
    ):
        try:
            ctx = context_for_ref(request, resource_ref)
            revision = preview_revision(expected_revision)
            files = facade(request)
            png = await files.thumbnail(
                ctx,
                resource_ref=resource_ref,
                width=width,
                height=height,
                scale=scale,
                **({"icon": True} if icon else {}),
                **({"expected_revision": revision} if revision is not None else {}),
            )
            if not _generation_guard(ctx.policy_generation)():
                raise FilesFacadeError("Preview access changed", code="permission_denied")
            if revision is not None and (await files.stat(ctx, resource_ref=resource_ref)).get("revision") != revision:
                raise FilesFacadeError("Preview source changed", code="resource_changed")
            return Response(
                png,
                media_type="image/png",
                headers={
                    "Cache-Control": "private, no-store",
                    "X-Content-Type-Options": "nosniff",
                    "Cross-Origin-Resource-Policy": "same-origin",
                    "Content-Security-Policy": "default-src 'none'; sandbox",
                },
            )
        except FilesFacadeError as error:
            _raise(error)

    @router.post("/watch")
    async def watch(body: WatchResourceRequest, request: Request):
        """Stream path-free change hints for one visible Host directory.

        The ResourceRef is owner/policy bound before response headers. Native
        events are advisory only; consumers re-list through the facade after a
        debounce, and gaps explicitly demand a full reconciliation.
        """
        try:
            events = await facade(request).watch(
                context_for_ref(request, body.resource_ref),
                resource_ref=body.resource_ref,
            )
        except FilesFacadeError as error:
            _raise(error)

        async def event_stream():
            iterator = events.__aiter__()
            pending = asyncio.create_task(anext(iterator))
            try:
                while True:
                    done, _ = await asyncio.wait({pending}, timeout=15.0)
                    if not done:
                        yield b": keepalive\n\n"
                        continue
                    try:
                        event = pending.result()
                    except StopAsyncIteration:
                        return
                    except FilesFacadeError as error:
                        # Headers are already committed. Preserve the typed failure for
                        # the browser so permanent denials are not retried as outages.
                        failure = json.dumps({"code": error.code}, separators=(",", ":"))
                        yield f"event: files-watch-error\ndata: {failure}\n\n".encode("utf-8")
                        return
                    safe = {
                        "sequence": int(event.get("sequence") or 0),
                        "kind": str(event.get("kind") or "rescan_required"),
                        "rescan_required": bool(event.get("rescan_required")),
                        "observed_unix_ms": int(event.get("observed_unix_ms") or 0),
                    }
                    yield (
                        "event: files-change\n"
                        f"data: {json.dumps(safe, separators=(',', ':'))}\n\n"
                    ).encode("utf-8")
                    pending = asyncio.create_task(anext(iterator))
            finally:
                if not pending.done():
                    pending.cancel()
                    try:
                        await pending
                    except (asyncio.CancelledError, StopAsyncIteration):
                        pass
                    except Exception:
                        pass
                close = getattr(iterator, "aclose", None)
                if callable(close):
                    await close()

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "private, no-store",
                "X-Accel-Buffering": "no",
                "X-Content-Type-Options": "nosniff",
            },
        )

    def generic_files_facade_factory(request: Request, context: ProviderContext) -> FilesFacade:
        return facade(request, copal_workspace=str(context.workspace_id or "default"))

    if application_state is not None:
        application_state.files_facade_factory = generic_files_facade_factory
        application_state.files_facade_state_factory = lambda workspace: facade_for_state(application_state,copal_workspace=workspace)
        def media_agent_service_factory(*, workspace, session_current, before_dispatch):
            if not isinstance(workspace,str) or not workspace or not callable(session_current) or not callable(before_dispatch): raise ValueError('media authority unavailable')
            return _media_service_for_state(application_state,workspace,session_current,before_dispatch)
        application_state.media_agent_service_factory = media_agent_service_factory
        def sprite_agent_service_factory(*, workspace, session_current, before_dispatch):
            if not isinstance(workspace,str) or not workspace or not callable(session_current) or not callable(before_dispatch): raise ValueError('sprite authority unavailable')
            return _sprite_sheet_service_for_state(application_state,workspace,session_current,before_dispatch)
        application_state.sprite_agent_service_factory = sprite_agent_service_factory
        def tripo_agent_service_factory(*, workspace, session_current, before_dispatch):
            if not isinstance(workspace,str) or not workspace or not callable(session_current) or not callable(before_dispatch): raise ValueError('media authority unavailable')
            return _tripo_service_for_state(application_state,workspace,session_current,before_dispatch)
        application_state.tripo_agent_service_factory = tripo_agent_service_factory
        def fal_agent_service_factory(*,workspace,session_current,before_dispatch):
            return _fal_service_for_state(application_state,workspace,session_current,before_dispatch)
        application_state.fal_agent_service_factory=fal_agent_service_factory
        def voidless_agent_service_factory(*,workspace,session_current,before_dispatch):
            return _voidless_service_for_state(application_state,workspace,session_current,before_dispatch)
        application_state.voidless_agent_service_factory=voidless_agent_service_factory
        def trellis_agent_service_factory(*,workspace,session_current,before_dispatch):
            return _trellis_service_for_state(application_state,workspace,session_current,before_dispatch)
        application_state.trellis_agent_service_factory=trellis_agent_service_factory
        def hunyuan_agent_service_factory(*,workspace,session_current,before_dispatch):
            return _hunyuan_service_for_state(application_state,workspace,session_current,before_dispatch)
        application_state.hunyuan_agent_service_factory=hunyuan_agent_service_factory
        application_state.hunyuan_human_service_factory=_hunyuan_human_service
        def fal_mesh_agent_service_factory(*,workspace,session_current,before_dispatch):
            return _fal_mesh_service_for_state(application_state,workspace,session_current,before_dispatch)
        application_state.fal_mesh_agent_service_factory=fal_mesh_agent_service_factory
        def material_agent_service_factory(*,workspace,session_current,before_dispatch):
            return _material_service_for_state(application_state,workspace,session_current,before_dispatch)
        application_state.material_agent_service_factory=material_agent_service_factory
        application_state.treehouse_files_facade = facade
        application_state.treehouse_files_context = context
        application_state.treehouse_files_content_response = content_response
    return router


__all__ = ["setup_files_facade_routes"]
