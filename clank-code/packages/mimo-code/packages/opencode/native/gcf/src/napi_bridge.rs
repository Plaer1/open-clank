//! Private synchronous ABI. The sole first-party caller is the admitted JS
//! loader, which rejects shared backing and passes a fresh private Buffer.
use napi::bindgen_prelude::{BufferSlice, Env, Object, Result};
use napi_derive::napi;
use std::panic::{catch_unwind, AssertUnwindSafe};
use crate::codec::{self, Outcome, Reason};
use crate::decoder::{self, DecodeError};

const POLICY: &str = "gcf-generic-v3-safe-integers-v1";
const CODEC_VERSION: &str = "0.1.0";
const MAX_BYTES: usize = 50 * 1024;

#[napi(js_name = "codecInfoV1")]
pub fn codec_info_v1(env: &Env) -> Result<Object<'_>> {
    let mut result = Object::new(env)?;
    result.set("abi", 1u32)?;
    result.set("codecVersion", CODEC_VERSION)?;
    result.set("policyIDs", vec![POLICY.to_owned()])?;
    result.set("decodePolicyIDs", vec![POLICY.to_owned()])?;
    result.set("previewPolicyIDs", vec![POLICY.to_owned()])?;
    Ok(result)
}

fn reason(value: Reason) -> &'static str {
    match value {
        Reason::TooSmall => "too_small", Reason::TooLarge => "too_large",
        Reason::Invalid => "invalid", Reason::UnsupportedNumber => "unsupported_number",
        Reason::TooDeep => "too_deep", Reason::TooManyValues => "too_many_values",
        Reason::DuplicateKey => "duplicate_key", Reason::NotSmaller => "not_smaller",
    }
}

#[napi(js_name = "encodeV1")]
pub fn encode_v1<'env>(env: &'env Env, input: BufferSlice<'env>, policy_id: String) -> Result<Object<'env>> {
    encode_result(env, input, policy_id, false)
}

#[napi(js_name = "encodePreviewV1")]
pub fn encode_preview_v1<'env>(env: &'env Env, input: BufferSlice<'env>, policy_id: String) -> Result<Object<'env>> {
    encode_result(env, input, policy_id, true)
}

fn encode_result<'env>(env: &'env Env, input: BufferSlice<'env>, policy_id: String, preview: bool) -> Result<Object<'env>> {
    let len = input.len();
    // This copy happens synchronously before parsing; no borrowed JS bytes
    // are retained or sent to a worker. The private caller guarantees that
    // the backing store cannot be shared or concurrently mutated.
    let outcome = catch_unwind(AssertUnwindSafe(|| {
        if policy_id != POLICY { return Err("codec_failure"); }
        if len > MAX_BYTES { return Err("too_large"); }
        let mut owned = Vec::new();
        owned.try_reserve_exact(len).map_err(|_| "codec_failure")?;
        owned.extend_from_slice(&input);
        Ok(if preview { codec::encode_preview(&owned) } else { codec::encode(&owned) })
    }));
    let mut result = Object::new(env)?;
    result.set("inputBytes", len as u32)?;
    match outcome {
        Ok(Ok(Outcome::Encoded(output))) => {
            result.set("state", "encoded")?;
            result.set("outputBytes", output.len() as u32)?;
            result.set("output", BufferSlice::copy_from(env, &output)?)?;
        }
        other => {
            let why = match other {
                Ok(Ok(Outcome::Bypass(value))) => reason(value),
                Ok(Err(value)) => value,
                Err(_) => "codec_failure",
                _ => "codec_failure",
            };
            result.set("state", "bypass")?;
            result.set("reason", why)?;
            result.set("outputBytes", len as u32)?;
        }
    }
    Ok(result)
}

/// Additive synchronous decode; only the verified private caller supplies nonshared input.
#[napi(js_name = "decodeV1")]
pub fn decode_v1<'env>(env: &'env Env, input: BufferSlice<'env>, policy_id: String) -> Result<Object<'env>> {
    let len = input.len();
    let outcome = catch_unwind(AssertUnwindSafe(|| {
        if policy_id != POLICY { return Err("codec_failure"); }
        if len > MAX_BYTES { return Err("too_large"); }
        let mut owned = Vec::new();
        owned.try_reserve_exact(len).map_err(|_| "codec_failure")?;
        owned.extend_from_slice(&input);
        decoder::decode(&owned).map_err(|e| match e {
            DecodeError::Invalid => "invalid", DecodeError::TooLarge => "too_large",
            DecodeError::TooDeep => "too_deep", DecodeError::TooManyValues => "too_many_values",
        })
    }));
    let mut result = Object::new(env)?;
    result.set("inputBytes", len as u32)?;
    match outcome {
        Ok(Ok(output)) => {
            result.set("state", "decoded")?;
            result.set("outputBytes", output.len() as u32)?;
            result.set("output", BufferSlice::copy_from(env, &output)?)?;
        }
        other => {
            let why = match other { Ok(Err(value)) => value, Err(_) => "codec_failure", _ => "codec_failure" };
            result.set("state", "refused")?;
            result.set("reason", why)?;
        }
    }
    Ok(result)
}

/// Optional additive stream capability; codecInfoV1 remains unchanged.
#[napi(js_name = "streamInfoV1")]
pub fn stream_info_v1(env: &Env) -> Result<Object<'_>> {
    let mut result=Object::new(env)?;
    result.set("version",1u32)?;
    result.set("policyIDs",vec![crate::stream::POLICY.to_owned()])?;
    Ok(result)
}

#[napi]
pub struct GenericStreamV1 { inner: crate::stream::GenericStream }

#[napi]
impl GenericStreamV1 {
    #[napi]
    pub fn push<'env>(&mut self, env:&'env Env, input:BufferSlice<'env>)->Result<Object<'env>> {
        let result=catch_unwind(AssertUnwindSafe(|| {
            if input.len()>MAX_BYTES {return Err(())}
            let owned=input.to_vec(); self.inner.push(&owned)
        }));
        let chunk=match result {Ok(Ok(chunk))=>chunk,_=>{self.inner.abort();return Err(napi::Error::from_reason("stream unavailable"))}};
        let mut out=Object::new(env)?;out.set("state","provisional")?;out.set("chunk",BufferSlice::copy_from(env,&chunk)?)?;Ok(out)
    }
    #[napi]
    pub fn close<'env>(&mut self,env:&'env Env)->Result<Object<'env>> {
        let result=catch_unwind(AssertUnwindSafe(||self.inner.close()));
        let chunk=match result {Ok(Ok(chunk))=>chunk,_=>{self.inner.abort();return Err(napi::Error::from_reason("stream unavailable"))}};
        let mut out=Object::new(env)?;out.set("state","complete")?;out.set("chunk",BufferSlice::copy_from(env,&chunk)?)?;Ok(out)
    }
    #[napi]
    pub fn abort(&mut self) {self.inner.abort()}
}

#[napi(js_name = "createGenericStreamV1")]
pub fn create_generic_stream_v1(policy_id:String)->Result<GenericStreamV1> {
    if policy_id!=crate::stream::POLICY {return Err(napi::Error::from_reason("stream unavailable"))}
    Ok(GenericStreamV1{inner:crate::stream::GenericStream::new()})
}

#[napi(js_name = "decodeGenericStreamV1")]
pub fn decode_generic_stream_v1<'env>(env:&'env Env,input:BufferSlice<'env>,policy_id:String)->Result<Object<'env>> {
    let result=catch_unwind(AssertUnwindSafe(||{
        if policy_id!=crate::stream::POLICY || input.len()>MAX_BYTES {return Err(())}
        let owned=input.to_vec();crate::stream_decoder::decode_stream(&owned).map_err(|_|())
    }));
    let output=match result {Ok(Ok(output))=>output,_=>return Err(napi::Error::from_reason("stream unavailable"))};
    let mut out=Object::new(env)?;out.set("state","decoded")?;out.set("output",BufferSlice::copy_from(env,&output)?)?;Ok(out)
}

/// Optional additive typed Symbol/Edge stream capability; separate from the generic stream.
#[napi(js_name = "graphStreamInfoV1")]
pub fn graph_stream_info_v1(env: &Env) -> Result<Object<'_>> {
    let mut result=Object::new(env)?;
    result.set("version",1u32)?;
    result.set("policyIDs",vec![crate::graph_stream::POLICY.to_owned()])?;
    Ok(result)
}

#[napi]
pub struct GraphStreamV1 { inner: crate::graph_stream::GraphStream }

#[napi]
impl GraphStreamV1 {
    #[napi]
    pub fn push<'env>(&mut self, env:&'env Env, input:BufferSlice<'env>)->Result<Object<'env>> {
        let result=catch_unwind(AssertUnwindSafe(|| {
            if input.len()>MAX_BYTES {return Err(())}
            let owned=input.to_vec(); self.inner.push(&owned)
        }));
        let chunk=match result {Ok(Ok(chunk))=>chunk,_=>{self.inner.abort();return Err(napi::Error::from_reason("stream unavailable"))}};
        let mut out=Object::new(env)?;out.set("state","provisional")?;out.set("chunk",BufferSlice::copy_from(env,&chunk)?)?;Ok(out)
    }
    #[napi]
    pub fn end<'env>(&mut self,env:&'env Env)->Result<Object<'env>> {
        let result=catch_unwind(AssertUnwindSafe(||self.inner.end()));
        let chunk=match result {Ok(Ok(chunk))=>chunk,_=>{self.inner.abort();return Err(napi::Error::from_reason("stream unavailable"))}};
        let mut out=Object::new(env)?;out.set("state","complete")?;out.set("chunk",BufferSlice::copy_from(env,&chunk)?)?;Ok(out)
    }
    #[napi]
    pub fn abort(&mut self) {self.inner.abort()}
}

#[napi(js_name = "createGraphStreamV1")]
pub fn create_graph_stream_v1(policy_id:String)->Result<GraphStreamV1> {
    if policy_id!=crate::graph_stream::POLICY {return Err(napi::Error::from_reason("stream unavailable"))}
    Ok(GraphStreamV1{inner:crate::graph_stream::GraphStream::new()})
}

/// Verifies a complete graph stream and returns its canonical `{symbols,edges}` JSON.
#[napi(js_name = "decodeGraphStreamV1")]
pub fn decode_graph_stream_v1<'env>(env:&'env Env,input:BufferSlice<'env>,policy_id:String)->Result<Object<'env>> {
    let result=catch_unwind(AssertUnwindSafe(||{
        if policy_id!=crate::graph_stream::POLICY || input.len()>MAX_BYTES {return Err(())}
        let owned=input.to_vec();crate::graph_stream::decode(&owned)?;crate::stream_decoder::decode_stream(&owned).map_err(|_|())
    }));
    let output=match result {Ok(Ok(output))=>output,_=>return Err(napi::Error::from_reason("stream unavailable"))};
    let mut out=Object::new(env)?;out.set("state","decoded")?;out.set("output",BufferSlice::copy_from(env,&output)?)?;Ok(out)
}
