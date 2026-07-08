pub mod memos;
pub mod native;
pub mod tencent;

use async_trait::async_trait;
use serde::{Deserialize, Serialize};

use crate::record::*;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GroomOpArgs {
    pub op: GroomOp,
    pub workspace_id: Option<String>,
    pub dry_run: bool,
}

#[async_trait]
pub trait MemoryProvider: Send + Sync {
    fn id(&self) -> &str;
    async fn capture(&self, turn: &CompletedTurn) -> CaptureResult;
    async fn recall(&self, q: &RecallQuery) -> RecallResult;
    async fn search(&self, p: &SearchParams) -> SearchResult;
    async fn groom(&self, op: &GroomOpArgs) -> GroomResult;
}
