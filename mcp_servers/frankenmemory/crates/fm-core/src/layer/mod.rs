pub mod layer_a;
pub mod layer_b;

use async_trait::async_trait;

use crate::embed::EmbeddingClient;
use crate::record::*;
use crate::store::MemoryStore;

#[async_trait]
pub trait RecallLayer: Send + Sync {
    async fn recall(
        &self,
        query: &RecallQuery,
        store: &dyn MemoryStore,
        embed: &dyn EmbeddingClient,
    ) -> RecallResult;
}
