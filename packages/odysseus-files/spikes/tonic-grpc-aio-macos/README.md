# Tonic + grpc.aio private macOS IPC admission spike

This directory is an isolated, non-production admission harness. It does not
replace `odysseus-files`, register a route, or provide a TCP fallback. The Rust
process accepts only an absolute Unix-domain socket path inside an owner-owned
`0700` directory, creates a `0600` socket, verifies the connecting peer UID on
macOS, and requires a random per-process session binding in gRPC metadata.

The harness exercises checked-in Python stubs with only runtime `grpcio` and
`protobuf` installed. Rust builds use a vendored `protoc`; installed machines do
not need `protoc` or `grpcio-tools`.

## Reproduce

```sh
python3 -m venv /tmp/openclank-tonic-admission-venv
/tmp/openclank-tonic-admission-venv/bin/pip install -r requirements-runtime.txt
cargo test --locked
cargo build --release --locked
/tmp/openclank-tonic-admission-venv/bin/python run_admission.py \
  --binary target/release/openclank-tonic-admission
```

The default run transfers 250 MiB in 256 KiB chunks through a deliberately slow
Python consumer while sampling concurrent health and 200-entry browse calls. It
also verifies explicit cancellation, a client deadline, bounded producer queue
depth, socket ownership/mode, server RSS, and the absence of a TCP listener. The
last stdout line is a machine-readable JSON report; any failed admission check
returns a nonzero exit status.

Python stubs are regenerated only during development:

```sh
/tmp/openclank-tonic-codegen-venv/bin/pip install -r requirements-codegen.txt
/tmp/openclank-tonic-codegen-venv/bin/python -m grpc_tools.protoc \
  -I proto --python_out=python --grpc_python_out=python proto/admission.proto
```

