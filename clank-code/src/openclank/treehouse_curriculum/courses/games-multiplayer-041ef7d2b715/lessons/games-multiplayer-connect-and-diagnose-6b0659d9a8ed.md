# Separate connection from session quality

- DNS locates named services; ports and transports distinguish endpoints. General protocol details live in Programming.
- STUN helps discover addressing, TURN relays traffic and ICE checks candidate paths. Discovery alone does not guarantee a usable peer connection.
- Matchmaking selects compatible players; a lobby organizes them; placement selects a host/region; the game server runs the session. Capacity and geography constrain different things.
- HTTP commonly carries service requests; WebSocket supports persistent messages; real-time voice has media-specific timing. A game can use several protocols.
- Diagnose in stages: resolution → connection/authentication → compatible protocol → session join → delay/loss/jitter under load. Transport security protects communication, while game rules still validate actions.

**Self-check:** A session joins successfully but players warp during packet loss. Name two useful measurements and one wrong first intervention. Then distinguish that failure from an unreachable peer behind NAT.
