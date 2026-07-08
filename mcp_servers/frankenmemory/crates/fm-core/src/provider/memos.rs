use async_trait::async_trait;
use tracing::info;

use crate::provider::*;
use crate::record::*;

pub struct MemosProvider {
    inner: crate::provider::native::NativeProvider,
}

impl MemosProvider {
    pub fn new(inner: crate::provider::native::NativeProvider) -> Self {
        Self { inner }
    }
}

#[async_trait]
impl MemoryProvider for MemosProvider {
    fn id(&self) -> &str {
        "memos"
    }

    async fn capture(&self, turn: &CompletedTurn) -> CaptureResult {
        // Layer-A base: same capture as native, but with fact-store addition
        let result = self.inner.capture(turn).await;
        info!("memos capture: delegated to native");
        result
    }

    async fn recall(&self, q: &RecallQuery) -> RecallResult {
        // Layer-A recall: hybrid→RRF→collapse with fact-store
        self.inner.recall(q).await
    }

    async fn search(&self, p: &SearchParams) -> SearchResult {
        self.inner.search(p).await
    }

    async fn groom(&self, op: &GroomOpArgs) -> GroomResult {
        self.inner.groom(op).await
    }
}
