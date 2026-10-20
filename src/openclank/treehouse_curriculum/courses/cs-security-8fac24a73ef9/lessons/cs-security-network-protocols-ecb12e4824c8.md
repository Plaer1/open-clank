# Layers, framing, and secure channels

- IP routes packets; TCP provides an ordered reliable byte stream. UDP provides datagrams with different delivery guarantees.
- A byte stream does not preserve application message boundaries. Use a defined length, delimiter/escaping, or other framing rule.
- DNS resolves names; HTTP defines application messages; TLS protects a channel under authenticated-key assumptions. Application authorization remains separate.

**Try:** A TCP read returns half a message. Explain how a length-prefixed receiver handles it and why TLS does not grant access to every server document.
