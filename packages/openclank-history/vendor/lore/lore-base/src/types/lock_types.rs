// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
use super::BranchId;
use super::Hash;

#[derive(Clone, Debug, Default, Eq, Hash, Ord, PartialEq, PartialOrd)]
/// Descriptor of a resource that can be locked
pub struct LockResource {
    /// Branch ID
    pub branch: BranchId,

    /// Hash identifier for the resource
    pub hash: Hash,

    /// Human readable description of the resource (i.e. file path, property name, etc)
    pub description: String,
}

#[derive(Debug, Clone, PartialEq, Default)]
/// Represents the lock on a resource
pub struct LockData {
    /// Resource
    pub resource: LockResource,

    /// Identifier of the user holding the lock
    pub owner: String,

    /// Lock timestamp
    pub locked_at: u64,
}
