#[path = "../src/parser.rs"] mod parser;
#[path = "../src/codec.rs"] mod codec;
use std::io::{Read,Write};
fn main() {
    let mut input = Vec::new(); std::io::stdin().read_to_end(&mut input).unwrap();
    match codec::encode(&input) {
        codec::Outcome::Encoded(bytes) => { std::io::stdout().write_all(b"encoded\n").unwrap(); std::io::stdout().write_all(&bytes).unwrap(); },
        codec::Outcome::Bypass(reason) => println!("bypass:{reason:?}"),
    }
}
