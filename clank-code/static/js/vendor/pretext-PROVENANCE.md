# Pretext for Matrix Rain

The local ESM bundle uses `@chenglou/pretext` 1.0.0. The release tarball’s SHA-512 integrity matched the npm registry’s published value. [Upstream repository](https://github.com/chenglou/pretext) · [Pinned tarball](https://registry.npmjs.org/@chenglou/pretext/-/pretext-1.0.0.tgz)

- Tarball integrity `sha512-BxKiDMxWJhyzL8asvcgg3T9YnUU7OUFsKO8oRaR2SYAPX4Sh2wM9gqTBjrBaZ4fnl84cEQ8czC0Q1M83z3chsA==`
- Bundle SHA-256 `ef46fdadda7b3bc8e2a8fd1493a5a2fced271227702e0935d114b34d03f0c7d2`
- Release license and all upstream third-party notices are reproduced in [pretext-LICENSE.txt](pretext-LICENSE.txt).

To regenerate, extract the pinned tarball alongside a temporary `matrix-entry.js` containing this re-export.

```js
export { clearCache, measureNaturalWidth, prepare } from './package/dist/layout.js';
```

Run Bun 1.3.13 with `bun build matrix-entry.js --target=browser --format=esm --minify --outfile=pretext-matrix.js` and prepend the copyright line from the shipped file. The Matrix renderer loads this module from the application’s static assets when Matrix Rain starts.
