use async_trait::async_trait;
use tracing::info;

use crate::provider::*;
use crate::record::*;

pub struct TencentProvider {
    inner: crate::provider::native::NativeProvider,
}

impl TencentProvider {
    pub fn new(inner: crate::provider::native::NativeProvider) -> Self {
        Self { inner }
    }
}

#[async_trait]
impl MemoryProvider for TencentProvider {
    fn id(&self) -> &str {
        "tencent"
    }

    async fn capture(&self, turn: &CompletedTurn) -> CaptureResult {
        // Layer-B base: same capture, with L0/L1/L2/L3 distillation semantics
        let result = self.inner.capture(turn).await;
        info!("tencent capture: delegated to native");
        result
    }

    async fn recall(&self, q: &RecallQuery) -> RecallResult {
        // Layer-B recall: L1 strategy + persona/scene
        self.inner.recall(q).await
    }

    async fn search(&self, p: &SearchParams) -> SearchResult {
        self.inner.search(p).await
    }

    async fn groom(&self, op: &GroomOpArgs) -> GroomResult {
        self.inner.groom(op).await
    }
}
