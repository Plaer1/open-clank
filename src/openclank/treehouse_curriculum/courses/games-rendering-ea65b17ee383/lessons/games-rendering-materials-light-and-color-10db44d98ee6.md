# A material is more than a color

- A material describes how a surface responds to light. Metallic/roughness models separate base color, roughness and metallic behavior; a normal map changes the shading normal, not the mesh silhouette.
- Direct light arrives from a source; indirect illumination has interacted elsewhere. A shadow tests visibility to light; a dark texture painted onto a surface is different.
- Color textures encode appearance; roughness and normal textures encode data. Applying an appearance transfer curve to data changes its meaning.
- Linear-light arithmetic, display encoding, gamut and tone mapping are separate concerns. HDR values may exceed display range; tone mapping chooses how to show them.

**Self-check:** A rock has a detailed normal map but a square silhouette and overly shiny surface. Name which inputs you would inspect for each issue. Explain why roughness should not be treated like base-color image data.
