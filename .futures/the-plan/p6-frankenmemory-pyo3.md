# P6 — Frankenmemory PyO3 binding (Odysseus in-process)

**Goal:** compile fm-core as a Python extension module so Odysseus calls frankenmemory directly — no subprocess, no pipe, no JSON serialization, no MCP protocol overhead.

## Why

Current Odysseus → frankenmemory path:
```
Python → JSON serialize → pipe write → Rust pipe read → JSON deserialize
→ Rust does work
→ JSON serialize → pipe write → Python pipe read → JSON deserialize
```
Four serialization passes per call. Plus a separate subprocess eating memory. Plus MCP protocol framing on top.

PyO3 path:
```
Python → direct Rust function call → SQLite → return
```
Zero IPC. Zero JSON. Zero MCP. The SQLite connection lives in-process.

## Scope

**New crate:** `crates/fm-pyo3/` in the frankenmemory workspace.

**Python API:**
```python
import frankenmemory

fm = frankenmemory.Frankenmemory(
    db_path="/path/to/frankenmemory.db",
    workspace_id="global",
    embedding_api_base="https://api.openai.com/v1",
    embedding_model="text-embedding-3-small",
)

# capture a turn
result = await fm.capture(
    content="User's favorite color is blue",
    source="mimo",
    session_id="ses_abc",
    workspace_id="global",
)

# recall
hits = await fm.recall(
    query="what is the user's favorite color?",
    top_k=5,
    workspace_id="global",
)

# search
results = await fm.search(query="color preferences", limit=10)

# groom
await fm.groom(op="decay", workspace_id="global")
```

## Crate structure

```
crates/fm-pyo3/
├── Cargo.toml
├── pyproject.toml          # maturin build config
└── src/
    └── lib.rs              # PyO3 bindings (~150 lines)
```

### Cargo.toml
```toml
[package]
name = "fm-pyo3"
version = "0.1.0"
edition = "2021"

[lib]
name = "frankenmemory"
crate-type = ["cdylib"]

[dependencies]
fm-core = { path = "../fm-core" }
pyo3 = { version = "0.22", features = ["extension-module", "async"] }
pyo3-asyncio = "0.22"
tokio = { workspace = true }
serde_json = { workspace = true }
```

### pyproject.toml
```toml
[build-system]
requires = ["maturin>=1.5,<2.0"]
build-backend = "maturin"

[project]
name = "frankenmemory"
version = "0.1.0"
requires-python = ">=3.9"
```

### lib.rs — binding surface

Wrap `NativeProvider` as a Python class. Four async methods mapped to the `MemoryProvider` trait:

```rust
#[pyo3::pyclass]
struct Frankenmemory {
    provider: Arc<NativeProvider>,
    runtime: tokio::runtime::Runtime,
}

#[pyo3::pymethods]
impl Frankenmemory {
    #[new]
    #[pyo3(signature = (db_path, workspace_id="global", embedding_api_base=None, embedding_model=None))]
    fn new(db_path: String, workspace_id: &str, ...) -> PyResult<Self>;

    fn capture(&self, content: String, source: Option<String>, ...) -> PyResult<PyObject>;
    fn recall(&self, query: String, top_k: Option<usize>, ...) -> PyResult<PyObject>;
    fn search(&self, query: String, limit: Option<usize>) -> PyResult<PyObject>;
    fn groom(&self, op: String, workspace_id: Option<String>, dry_run: Option<bool>) -> PyResult<PyObject>;
}
```

**Type mapping:**
- `CompletedTurn` → Python kwargs (content, source, session_id, etc.)
- `RecallResult` → Python dict with `memories` list
- `SearchResult` → Python dict with `results` list
- `CaptureResult` → Python dict with `records_captured`, etc.
- Embeddings: `NoopEmbeddingClient` by default, `HttpEmbeddingClient` when `embedding_api_base` provided

## Python integration

### New file: `src/frankenmemory_pyo3_provider.py`

Drop-in replacement for `FrankenmemoryProvider`. Same `MemoryProvider` interface. Uses the PyO3 module instead of MCP subprocess.

```python
class FrankenmemoryPyO3Provider(MemoryProvider):
    provider_id = "frankenmemory"
    display_name = "Frankenmemory (Rust/PyO3)"

    def __init__(self, db_path=None, workspace_id="global", **kwargs):
        import frankenmemory
        self._fm = frankenmemory.Frankenmemory(
            db_path=db_path or os.environ["FM_DB_PATH"],
            workspace_id=workspace_id,
            ...
        )

    async def remember(self, text, **kwargs) -> MemoryRecord:
        result = await self._fm.capture(content=text, ...)
        return MemoryRecord(...)

    async def recall(self, query, **kwargs) -> List[MemorySearchHit]:
        result = await self._fm.recall(query=query, ...)
        return [MemorySearchHit(...) for m in result["memories"]]

    # ... list_memories, delete, same pattern
```

### `app_initializer.py` — provider selection

```python
memory_provider = os.environ.get("MEMORY_PROVIDER", "frankenmemory")
if memory_provider == "frankenmemory":
    try:
        import frankenmemory  # PyO3 available?
        fm = FrankenmemoryPyO3Provider(db_path=os.environ["FM_DB_PATH"], ...)
        logger.info("Memory provider: frankenmemory (PyO3 in-process)")
    except ImportError:
        fm = FrankenmemoryProvider(command=fm_command, workspace_id=fm_workspace)
        logger.info("Memory provider: frankenmemory (MCP subprocess fallback)")
    native = NativeMemoryProvider(memory_manager, memory_vector)
    native.enabled = False
    memory_provider_registry = MemoryProviderRegistry([fm, native])
```

## What changes

| Component | Before | After |
|-----------|--------|-------|
| Odysseus → fm | MCP subprocess (pipe + JSON) | PyO3 in-process (direct call) |
| mimo → fm | MCP subprocess (pipe + JSON) | No change (TypeScript) |
| fm-mcp binary | Required for both | Required for mimo only |
| Odysseus memory calls | ~0.1-1ms latency | ~0.001ms (function call) |
| Odysseus memory processes | 1 extra subprocess | 0 extra (in-process) |
| Serialization per call | JSON ×4 | zero |

## What doesn't change

- Browser ↔ Odysseus: HTTP/S + SSE (remote access preserved)
- Odysseus ↔ mimo: ACP over stdio (already optimal)
- fm-mcp binary: still built, still used by mimo
- SQLite schema, DB path, data format: identical
- `FrankenmemoryProvider` (MCP): kept as fallback
- Workspace filtering, recall, capture semantics: identical

## Build & install

```bash
cd mcp_servers/frankenmemory
maturin develop --release  # installs into current venv as `frankenmemory`
```

Or add to `requirements.txt`:
```
frankenmemory @ file:///home/e/sauce/ai/open-clank/mcp_servers/frankenmemory
```

## Risks

1. **tokio runtime in Python process** — PyO3 + tokio needs careful lifecycle management. The tokio runtime must be created once and reused. `pyo3-asyncio` handles this but adds a dependency. Mitigation: use `tokio::runtime::Runtime::block_on` inside GIL-released sections for simplicity (no async Python needed if we block).

2. **SQLite thread safety** — `rusqlite` connection is `Mutex<Connection>`, not `Send` across threads by default. In-process means the Python GIL and Rust's Mutex must coexist. Mitigation: `r2d2` connection pool or `Send` wrapper. Already handled — `SqliteStore` wraps connection in `Mutex<Connection>` which is `Send`.

3. **Embedding HTTP calls** — `HttpEmbeddingClient` uses reqwest with tokio. In-process this works but the tokio runtime must be running. Mitigation: create a single runtime in `#[new]` and reuse for all calls.

## Dependencies

- `pyo3 = "0.22"` (extension-module + async features)
- `pyo3-asyncio = "0.22"` (or handle manually with `block_on`)
- `maturin >= 1.5` (build tool)
- Python 3.9+ (for match statements if used in Python side)

## Verification

1. `maturin develop --release` succeeds
2. `python -c "import frankenmemory; fm = frankenmemory.Frankenmemory(db_path='/tmp/test.db')"` works
3. `await fm.capture(content="test")` writes to SQLite
4. `await fm.recall(query="test")` returns the capture
5. Odysseus boots with PyO3 provider, logs "frankenmemory (PyO3 in-process)"
6. Memory round-trip works end-to-end through Odysseus
