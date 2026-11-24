# What a reconstructed frame can know

Optional practice.

- Antialiasing estimates pixel coverage to reduce jagged sampling; upscaling reconstructs a higher-resolution image from fewer rendered samples. Denoising estimates a less noisy image from noisy measurements.
- Temporal methods reuse previous samples with motion/depth checks. Generated frames estimate intermediate images; they do not add authoritative simulation or input steps.
- Case: at t0 a moving foreground square covers pixel P. At t1 it moves right and exposes P's background. Old P contains the square, not the newly exposed surface.
- Visible buffer contains a wall and the near side of a character. A reflective surface would need an object behind the camera that is absent from that buffer.

**Self-check:** Match the five techniques to inputs/outputs. Explain the disocclusion and off-screen limits; assess '120 displayed frames means 120 authoritative input/simulation steps.'
