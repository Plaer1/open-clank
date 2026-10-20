# A shader transforms declared inputs

- A vertex shader processes vertex data; a fragment shader computes per-fragment outputs. Uniforms are shared parameters; interpolated varyings carry values between stages; bindings connect resources.
- This original Godot 4.7 canvas shader makes a left-to-right ramp on a ColorRect. Running it is optional; the required exercise is a numerical prediction.

```glsl
shader_type canvas_item;
render_mode unshaded;
uniform float strength : hint_range(0.0, 1.0) = 1.0;
void fragment() {
    float level = mix(0.2, 0.8, UV.x) * strength;
    COLOR = vec4(level, level, level, 1.0);
}
```

- Shader source is compiled for a target; GLSL, HLSL, WGSL and Metal Shading Language are not interchangeable strings. SPIR-V is an intermediate representation used by Vulkan toolchains.

**Self-check:** Predict level at UV.x=0,0.5,1 with strength=0.5. Identify which input varies across the rectangle and which is shared.
