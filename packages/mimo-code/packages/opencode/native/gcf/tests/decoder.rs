#[path="../src/parser.rs"] mod parser;
#[path="../src/codec.rs"] mod codec;
#[path="../src/decoder.rs"] mod decoder;
#[test]
fn actual_encoder_roundtrip_and_refusals(){
 let inputs=[
  format!("{{\"rows\":[{{\"x\":null,\"text\":\"{}\"}},{{\"text\":\"{}\",\"flag\":true}}]}}","hello".repeat(20),"world".repeat(20)),
  format!("{{\"nested\":{{\"a\":[1,2,3],\"b\":\"{}\"}},\"seq\":[[1,2],[3,4]]}}","xy".repeat(100)),
  format!("[{{\"a\":\"{}\"}},{{\"a\":\"{}\"}}]","abc".repeat(50),"xyz".repeat(50))
 ];
 for input in inputs {let encoded=codec::encode_value(&parser::parse(input.as_bytes()).unwrap()).unwrap();let decoded=decoder::decode(&encoded).unwrap();assert_eq!(parser::parse(input.as_bytes()),parser::parse(&decoded));}
 let input=format!("[{}]", (0..40).map(|i|format!("{{\"number\":{},\"name\":\"item\"}}",i)).collect::<Vec<_>>().join(","));
 let encoded=match codec::encode(input.as_bytes()){codec::Outcome::Encoded(v)=>v,x=>panic!("not encoded: {x:?}")};assert_eq!(parser::parse(input.as_bytes()),parser::parse(&decoder::decode(&encoded).unwrap()));
 for invalid in ["GCF profile=generic\na=1\na=2\n","GCF profile=generic\na=01\n","GCF profile=generic\n## [2]{a}\n~\n", "GCF profile=generic\n=9007199254740992\n","GCF profile=generic\na=~\n","GCF profile=generic\na=-\ntrailing"] {assert!(decoder::decode(invalid.as_bytes()).is_err(),"{invalid:?}")}
 let mut deep=parser::Value::Integer(1);
 for _ in 0..17 {deep=parser::Value::Object(vec![("a".into(),parser::Value::Array(vec![deep]))]);}
 let deep_frame=codec::encode_value(&deep).unwrap();
 assert_eq!(decoder::decode(&deep_frame),Err(decoder::DecodeError::TooDeep));
 let expansion=format!("GCF profile=generic\n## [5000]{{{}}}\n{}", "k".repeat(30000), "1\n".repeat(5000));
 assert!(expansion.len()<parser::MAX_BYTES);assert_eq!(decoder::decode(expansion.as_bytes()),Err(decoder::DecodeError::TooLarge));
 assert!(decoder::decode(&vec![b'x';parser::MAX_BYTES+1]).is_err());
}
