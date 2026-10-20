# Separate listening, transcription and speech output

- Microphone permission, audio-input support and speech synthesis are independent checks. Uploading a recording can have different requirements from live capture.

**Offline cards:** A1 accepts text and returns audio; microphone permission=true. A2 accepts audio and returns text; microphone permission=false, upload capability unspecified. A3 route unavailable. Supplied spoken-script text: `Meeting at fifty`; supplied transcript: `Meeting at fifteen`.

A1 is synthesis, not transcription. A2 cannot use blocked live capture; do not infer whether upload exists. The transcript changes the number and needs correction against the source.

A successful synthesis needs an observed audio result you can inspect/play, not only microphone permission. No card here establishes a live voice conversation.
