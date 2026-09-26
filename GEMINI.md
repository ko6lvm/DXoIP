# DXoIP Development Guidelines & Protocol Invariants

## Architecture & Separation of Concerns
1. **Network Transport (`UDPManager` in `udp_manager.py`):**
   - Owns UDP socket binding, STUN discovery (RFC 5389), HTTP matchmaker signaling (`/join`, `/poll`), and UDP hole punching.
   - Prioritizes LAN vs. WAN candidate endpoints (LAN first if public WAN IP matches).
   - Enforces a 10-second inactivity watchdog: transmits a `TIMEOUT` packet to the peer upon link loss before triggering `on_disconnected()`.
   - Never inspects or parses application payloads; treats data as raw byte blobs.

2. **Protocol & Framing Layer (`ROIPUDP` in `roip_udp.py`):**
   - Owns serialization, deserialization, sequence counters, and microsecond timestamps for RoIP packets.
   - Communicates with `UDPManager` via direct method calls (`send_packet`) and callbacks (`on_packet_received`).

## Binary Struct Specifications
All packets share a strict 8-byte header:
* Offset 0 (1B): Magic byte `0x52` (ASCII `'R'`)
* Offset 1 (1B): Flags (`Bit 0 = PTT`, `Bit 1 = COS`)
* Offset 2–3 (2B): Sequence counter (`uint16`, 0–65535)
* Offset 4–7 (4B): Timestamp (`uint32` microsecond clock `time_us_32`)

* **Heartbeat Struct:** Exactly **8 bytes** (`ROIP_HEARTBEAT_SIZE`).
* **RoIP Data Struct:** Exactly **648 bytes** (`ROIP_DATA_SIZE`), containing 640 bytes of 16-bit PCM (20ms audio @ 16 kHz).
* **Endianness:** Default to Network Byte Order (`!BBHI`), keeping configurable support for Little-Endian (`<BBHI`).

## Protocol Semantics & Rules
* **Continuous PTT/COS State:** PTT and COS flags must be evaluated and transmitted on **every single frame**. Never implement PTT as a toggle packet or state-transition event, which risks stuck transmitters upon packet loss.
* **MicroPython Compatibility:** Maintain zero external dependencies. Use standard library modules (`socket`, `struct`, `time`, `threading`) compatible with Raspberry Pi Pico 2 W / MicroPython.
