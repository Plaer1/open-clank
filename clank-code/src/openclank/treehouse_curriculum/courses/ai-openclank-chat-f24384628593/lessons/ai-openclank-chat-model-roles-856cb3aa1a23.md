# Match the model to the input and output

- Inspect the selected model's current route and declared input/output capabilities.
- Text generation, image understanding and speech synthesis are different jobs. A registry entry alone does not prove that this browser can use it.

**Offline card:** A accepts text and images, returns text, and its route is available. B accepts text and returns audio. C's route is unavailable. Task 1: describe supplied pixels. Task 2: speak supplied text.

Choose A for task 1 and B for task 2, subject to the actual attachment/audio controls. An unavailable C cannot be made available by a prompt.

**Additional checks:** none of the supplied cards declares audio-to-text transcription. If a fallback fails, inspect its actual route, directional capability, attachment handling and result; selecting a fallback does not prove success.
