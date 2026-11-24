"""Closed table of accepted native-analysis provider profiles (single source for Host validators).

Lookups use equality only, so untrusted JSON values (lists, dicts) are never hashed.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class AnalysisProfile:
    provider: str
    version: str
    producer_evidence: str        # receipt providerEvidence, dossier executableEvidence, snapshot provider_evidence
    version_evidence: str         # receipt/dossier/snapshot version evidence
    result_version_evidence: str  # result providerVersionEvidence


PROFILES = (
    AnalysisProfile(provider='rizin', version='0.8.1', producer_evidence='native_producer_attested',
                    version_evidence='operator_configured_unverified', result_version_evidence='operator_configured_unverified'),
)
_active = PROFILES


@contextmanager
def using_profiles(table):
    """Test hook: swap the table for the duration of the block. Not a registration API."""
    global _active
    previous, _active = _active, tuple(table)
    try: yield
    finally: _active = previous


def profile_for(provider, version, profiles=None):
    for profile in (_active if profiles is None else profiles):
        if provider == profile.provider and version == profile.version: return profile


def profile_for_version(version, profiles=None):
    for profile in (_active if profiles is None else profiles):
        if version == profile.version: return profile


def profile_for_provider(provider, profiles=None):
    for profile in (_active if profiles is None else profiles):
        if provider == profile.provider: return profile


def default_legacy_profile(profiles=None):
    """For stored documents written before a provider field existed: the first (Rizin) profile."""
    return (_active if profiles is None else profiles)[0]
