//! Pure source for the optional GCF codec; no runtime integration is registered.
pub mod parser;
pub mod codec;
pub mod decoder;
pub mod stream;
pub mod stream_decoder;
pub mod graph_stream;
#[cfg(feature = "node-addon")]
mod napi_bridge;
