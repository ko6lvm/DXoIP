# DXoIP

A lightweight, zero-dependency **Radio over IP (RoIP)** protocol implementation and peer-to-peer UDP hole puncher in Python. Designed for both desktop Python 3 and MicroPython (Raspberry Pi Pico 2 W).

DXoIP connects any two devices directly across NATs and firewalls without port forwarding, central relay servers, or third-party libraries. It couples **RFC 5389 STUN discovery** and a serverless **Cloudflare Worker matchmaker** with a robust binary framing layer streaming **20ms 16-bit PCM audio** and real-time **PTT/COS signaling**.

---

## Architecture

DXoIP is architected into two decoupled layers:

```
+-----------------------------------------------------------+
|                       udp-manager                         |
|                                                           |
|  1. Open UDP socket & query STUN (RFC 5389)               |
|  2. Exchange endpoints via matchmaker (/join, /poll)      |
|  3. UDP hole punch across candidate endpoints             |
|  4. Maintain NAT pinhole (PING/PONG) + 10s watchdog       |
+-----------------------------------------------------------+
         |                                         ^
         | 8B / 648B Raw Bytes (in)                | 8B / 648B Raw Bytes (out)
         v                                         |
+-----------------------------------------------------------+
|                         roip-udp                          |
|                                                           |
|  - 648-byte RoIP Data Struct (20ms 16-bit PCM audio)      |
|  - 8-byte Heartbeat Struct (PTT/COS signaling keepalive)  |
|  - Sequence tracking & microsecond clock timestamps       |
|  - Continuous PTT/COS state evaluation                    |
+-----------------------------------------------------------+
         |                                         ^
         | Decoded Frames                          | Audio & PTT/Mic
         v                                         |
+-----------------------------------------------------------+
|                      radio-manager                        |
|       (Audio interface, PTT line, Kenwood radio / GPIO)   |
+-----------------------------------------------------------+
```

1. **Network Transport (`UDPManager` in `udp_manager.py`):**
   * Owns socket lifecycle, STUN reflexive endpoint discovery, and HTTP signaling.
   * Punches UDP holes across both WAN and LAN candidates simultaneously (prioritizing LAN when peers share a public IP).
   * Enforces a **10-second inactivity watchdog**: automatically transmits a `TIMEOUT` packet to the remote peer upon link loss before triggering `on_disconnected()`.
   * Agnostic to application payloads: sends and receives raw byte datagrams.

2. **Protocol & Framing Layer (`ROIPUDP` in `roip_udp.py`):**
   * Handles packing, unpacking, sequence counters (0–65535), and 32-bit microsecond clock timestamps (`time_us_32`).
   * Evaluates continuous PTT and COS bitflags on every frame.
   * Bridges directly with `UDPManager` via method calls (`send_packet`) and callbacks (`on_packet_received`).

---

## Packet Specifications

All datagrams share a strict **8-byte header**, cleanly distinguished by datagram size:

```
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|  Magic (0x52) |  Flags (PTT/COS)|       Sequence (uint16)     |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                      Timestamp (uint32)                       |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                                                               |
|        Payload: 640 bytes (320 samples 16-bit PCM Audio)      |
|               (Only present in RoIP Data packets)             |
|                                                               |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
```

### 1. RoIP Data Struct (648 bytes)
Transmits 20ms of audio (320 samples of 16-bit mono PCM @ 16 kHz):
* **Offset 0 (1B):** Magic byte `0x52` (ASCII `'R'`)
* **Offset 1 (1B):** Flags bitfield (`Bit 0 = PTT`, `Bit 1 = COS`)
* **Offset 2–3 (2B):** Sequence counter (`uint16`, 0–65535)
* **Offset 4–7 (4B):** Timestamp (`uint32` microsecond clock `time_us_32`)
* **Offset 8–647 (640B):** Audio payload (640 bytes of 16-bit PCM)

### 2. Heartbeat Data Struct (8 bytes)
Lightweight signaling datagram sent during radio idle:
* **Offset 0 (1B):** Magic byte `0x52` (ASCII `'R'`)
* **Offset 1 (1B):** Flags bitfield (`Bit 0 = PTT`, `Bit 1 = COS`)
* **Offset 2–3 (2B):** Sequence counter (`uint16`, 0–65535)
* **Offset 4–7 (4B):** Timestamp (`uint32` microsecond clock `time_us_32`)

### Continuous PTT & COS State Semantics
* **Never a Toggle Event:** PTT and COS flags are evaluated and transmitted on **every single frame**.
* **Failsafe Design:** If packet loss occurs over UDP, receiving radios will not get stuck in a "hot mic" transmit state. The remote station automatically unkeys if PTT frames stop arriving.

---

## Quickstart

Run on two different machines (across separate Wi-Fi, cellular, or NAT networks):

### Device 1:
```bash
python peer.py --room 1234
```

### Device 2:
```bash
python peer.py --room 1234
```

### Establishing P2P Connection
Once both peers run `peer.py --room 1234`, they query STUN, discover their public reflexive and local endpoints, exchange them via the matchmaker, and punch a direct UDP hole.

```text
==========================================
 PUBLIC (WAN) ENDPOINT : 198.51.100.1:45123
 LOCAL (LAN) ENDPOINT  : 192.168.1.50:45123
==========================================

[+] SUCCESS! Direct UDP Hole Punched and P2P Session Established!
[+] Active Peer Endpoint: 203.0.113.10:52341
[*] Connection active. Press Ctrl+C to disconnect.
```

To test full-duplex 16 kHz audio streaming and Push-to-Talk, use the dedicated [Audio Simulator](tools/audio_simulator/README.md):
```bash
python tools/audio_simulator/audio_app.py --room 1234
```

---

## CLI Reference (`peer.py`)

```text
usage: peer.py [-h] [--port PORT] [--server SERVER] [--room ROOM]
               [--peer PEER] [--stun STUN] [--stun-port STUN_PORT]

options:
  -h, --help            Show this help message and exit
  --port PORT           Local UDP port to bind (default: 0 / dynamic OS port)
  --server SERVER       HTTP Matchmaker URL (default: https://udp-matchmaker.lvmlabs.org)
  --room ROOM           Room key to pair with peer (e.g. 1234)
  --peer PEER           Direct remote endpoint (<IP>:<Port>) for manual punch without matchmaker
  --stun STUN           STUN server hostname (default: stun.l.google.com)
  --stun-port STUN_PORT STUN server port (default: 19302)
```

---

## Python API Usage

### 1. Using `ROIPUDP` with `UDPManager`

```python
from udp_manager import UDPManager
from roip_udp import ROIPUDP, RoipPacket

# Initialize transport and RoIP framing layer
udp_mgr = UDPManager()
roip = ROIPUDP(udp_manager=udp_mgr)

# Register callbacks
def on_audio(packet: RoipPacket):
    print(f"Audio received: {len(packet.payload)}B | PTT={packet.ptt} | Seq={packet.sequence}")

def on_heartbeat(packet: RoipPacket):
    print(f"Heartbeat: PTT={packet.ptt} | COS={packet.cos}")

def on_disconnect(reason):
    print(f"Disconnected: {reason}")

roip.on_data_received = on_audio
roip.on_heartbeat_received = on_heartbeat
udp_mgr.on_disconnected = on_disconnect

# Connect using matchmaker room
udp_mgr.connect_room(room_key="1234")

# Transmit a 20ms audio frame (648 bytes) with PTT active
pcm_samples = b"\x00" * 640
roip.send_data(payload=pcm_samples, ptt=True, cos=False)

# Transmit an idle heartbeat (8 bytes)
roip.send_heartbeat(ptt=False, cos=False)
```

### 2. Passing Custom Structs Directly to `UDPManager`

You can bypass the RoIP layer and pass custom binary structs directly to `UDPManager`:

```python
import struct
from udp_manager import UDPManager

udp_mgr = UDPManager()
udp_mgr.connect_peer("198.51.100.1", 5000)

# Pack your own 8-byte binary struct: Magic (0x52), Flags, Seq, Timestamp
custom_packet = struct.pack("!BBHI", 0x52, 0x01, 100, 12345678)

# Send raw bytes directly:
udp_mgr.send_packet(custom_packet)
```

---

## Repository Structure

```text
DXoIP/
├── udp_manager.py        # P2P UDP transport, STUN resolution, matchmaking & 10s watchdog
├── roip_udp.py           # 648B RoIP Data struct & 8B Heartbeat struct framing layer
├── peer.py               # CLI entry point wiring UDPManager & ROIPUDP
├── stun.py               # RFC 5389 STUN binary protocol implementation
├── README.md             # Documentation and usage guide
├── GEMINI.md             # Architecture rules and protocol invariants
└── cloudflare/           # Serverless matchmaker service
    ├── worker.js         # Matchmaker API (/join and /poll endpoints)
    └── wrangler.toml     # Cloudflare Workers configuration
```

---

## Self-Hosting the Matchmaker

A live public matchmaker is pre-configured at `https://udp-matchmaker.lvmlabs.org`.

To deploy your own free instance on Cloudflare Workers:

1. Install the Cloudflare Wrangler CLI:
   ```bash
   npm install -g wrangler
   ```
2. Authenticate:
   ```bash
   wrangler login
   ```
3. Deploy from the `cloudflare` folder:
   ```bash
   cd cloudflare
   wrangler deploy
   ```
4. Run `peer.py` pointing to your custom worker:
   ```bash
   python peer.py --server https://<your-worker>.<your-subdomain>.workers.dev --room 1234
   ```

---

## License

MIT
