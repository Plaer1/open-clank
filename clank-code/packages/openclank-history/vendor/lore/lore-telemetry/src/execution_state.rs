// SPDX-FileCopyrightText: 2026 Epic Games, Inc.
// SPDX-License-Identifier: MIT
/// Server-side state carried through `ExecutionContext` for span propagation
/// via `#[lore_instrument]`.
pub struct ServerExecutionState {
    pub span: ::tracing::Span,
    pub context_label: &'static str,
}
