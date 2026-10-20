# Schedule resources, not just draws

- Buffers and images hold data; commands reference them. A producer must finish and make writes visible before a dependent consumer reads. A barrier expresses a dependency; a fence can coordinate completion with the host.
- Compute dispatch performs parallel work outside a raster draw. Tessellation subdivides geometry; mesh/task pipelines offer another geometry-processing path where supported.
- Ray tracing queries intersections using acceleration structures. Path tracing estimates light transport through sampled paths; more samples trade work for lower noise rather than guaranteeing instant realism.
- Vulkan, Direct3D, Metal and WebGPU place different interfaces over GPU work. Do not assume every device/API exposes the same features. Query the actual target and profile its frame time.

**Self-check:** A compute pass writes positions that a draw reads. State the data dependency and two things to measure before adding a more advanced renderer.
