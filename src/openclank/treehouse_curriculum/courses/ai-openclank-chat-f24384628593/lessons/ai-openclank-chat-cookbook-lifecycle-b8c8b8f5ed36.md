# Trace a local model from download to readiness

- Cookbook separates Launch, Download, Dependencies and Settings; Scan/Download belongs to its download workflow.
- A downloaded file is not a running server. A running process can still be loading. Inspect readiness and output.

**Offline machine:** Laptop A, macOS arm64, 8 GiB memory. Target folder `fictional/practice/models`; llama.cpp installed; MLX and vLLM unavailable. Candidate tiny: Q4 GGUF, 3 GiB, compatible with this llama.cpp setup. Candidate giant: safetensors, 18 GiB, requires the unavailable vLLM setup. Preset: Laptop A, llama.cpp, tiny, port 8100.

**State cards:** D1 download 50%, incomplete. D2 download done, file cached, no server. S1 process running, loading 45%, readiness pending. S2 readiness success, responding model tiny. S3 badge Stopped, exit 1, output `port 8100 occupied`.

Choose tiny for this fictional compatibility exercise. For S3 inspect the conflict and use an authorized unused port or deliberately stop the identified conflicting service. No installation or download is needed to answer.
