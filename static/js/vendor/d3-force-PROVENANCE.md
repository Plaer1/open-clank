# D3 force layout dependency

Locally served ESM bundle acquired October 20, 2026 from https://esm.sh/d3-force@3.0.0/X-ZGQzLWRpc3BhdGNoQDMuMC4xLGQzLXF1YWR0cmVlQDMuMC4xLGQzLXRpbWVyQDMuMC4x/es2022/d3-force.bundle.mjs.
SHA256 `c5e97996f8ebcd379c045e1f6f1288136849dad40eb0ee19a3c403387e73a58a`. No runtime CDN imports.

Upstream: https://github.com/d3/d3-force/tree/v3.0.0 . Bundle produced by esm.sh; pinned package metadata:

```json
{
  "d3-force": {
    "version": "3.0.0",
    "dependencies": {
      "d3-dispatch": "1 - 3",
      "d3-quadtree": "1 - 3",
      "d3-timer": "1 - 3"
    }
  },
  "d3-quadtree": {
    "version": "3.0.1",
    "dependencies": {}
  },
  "d3-dispatch": {
    "version": "3.0.1",
    "dependencies": {}
  },
  "d3-timer": {
    "version": "3.0.1",
    "dependencies": {}
  }
}
```

Full upstream and dependency ISC notices are retained in d3-force-LICENSE.txt. Open Clank calls simulation.stop() immediately and advances finite ticks in its existing layout worker; it does not adopt D3 rendering or a live timer loop.

Dependency versions explicitly pinned in the acquisition request: `https://esm.sh/d3-force@3.0.0?bundle&deps=d3-dispatch@3.0.1,d3-quadtree@3.0.1,d3-timer@3.0.1`. Upstream downloaded bundle SHA256 `11af883da9dbacd4674448007693acc57971af2c58497abcedb17a198f0feef0`; shipped SHA above differs only by removal of the unavailable source-map comment.
