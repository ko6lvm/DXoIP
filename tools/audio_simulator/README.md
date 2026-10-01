# DXoIP Desktop Audio Simulator

A standalone test application for testing real-time **16 kHz 16-bit linear PCM audio streaming** and **continuous PTT/COS signaling** over peer-to-peer RoIP UDP connections.

> [!NOTE]
> **Clean Isolation:** This tool is strictly isolated in `tools/audio_simulator/`. The core DXoIP codebase (`roip_udp.py`, `udp_manager.py`) remains 100% zero-dependency and compatible with MicroPython on the Raspberry Pi Pico 2 W.

---

## Features

* **Strict Protocol Compliance:**
  * Streams 648-byte RoIP Data packets (8-byte header + 640-byte 16-bit mono PCM @ 16 kHz) at 50 packets/sec (20ms frames).
  * Evaluates and transmits continuous PTT and COS flags on every frame.
  * Sends 8-byte Heartbeat packets during radio idle / unkeyed state.
* **Dual-Mode Audio Engine:**
  * **Live Hardware Audio (`mode=live`):** Streams from your physical microphone and plays out to your speakers using `sounddevice` (when hardware and `libportaudio2` are available).
  * **Synthetic Audio / File Engine (`mode=synth`):** Built-in zero-dependency generator producing 1 kHz sine wave test tones, roger beeps, or playing from 16 kHz WAV files. Can also record received audio to `.wav`.
* **Real-time Terminal Dashboard:**
  * Visual ASCII VU meter for Tx/Rx audio levels (dBFS).
  * Instant PTT status (KEYED vs IDLE) and remote COS status.
  * Jitter buffer queue depth and packet counters.
* **Interactive Hotkeys:**
  * `[SPACE]` or `[P]`: Toggle PTT (Key / Unkey transmitter).
  * `[T]`: Trigger 1-second 1 kHz test tone burst (useful for rapid link and audio path checks).
  * `[H]`: Send 8-byte Heartbeat packet.
  * `[Q]`: Quit cleanly.

---

## Setup & Virtual Environment

You can run this simulator using your system Python or in an isolated virtual environment:

### 1. Create and Activate Virtual Environment
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Optional: Live Hardware Audio
If you want live microphone and speaker streaming:
```bash
# On Debian / Ubuntu:
sudo apt-get install -y libportaudio2

# Install sounddevice in your venv:
pip install -r tools/audio_simulator/requirements.txt
```
*(If PortAudio is not installed, the simulator will automatically use its built-in synthetic audio engine without failing!)*

### 3. Cleanup
If you wish to remove the virtual environment at any time:
```bash
rm -rf .venv
```

---

## Usage

### Test 1: Local Loopback Test (Two Terminals on Same Machine)

**Terminal 1:**
```bash
python3 tools/audio_simulator/audio_app.py --room 9999 --port 15001
```

**Terminal 2:**
```bash
python3 tools/audio_simulator/audio_app.py --room 9999 --port 15002
```

Once connected:
* Press `T` in Terminal 1: Sends a 1-second 1 kHz tone burst (50 RoIP data frames).
* Terminal 2 will immediately register incoming RoIP Data frames, light up the Rx VU meter, and report remote PTT active!
* Press `SPACE` in Terminal 1 to latch PTT continuously.

---

### Test 2: Connecting Remote Peers Across NAT (Matchmaker)

**Device A:**
```bash
python3 tools/audio_simulator/audio_app.py --room 5432
```

**Device B:**
```bash
python3 tools/audio_simulator/audio_app.py --room 5432
```

Both peers query STUN, exchange reflexive WAN candidates via the matchmaker room, punch UDP holes, and start streaming RoIP audio.

---

### Command-Line Options

```text
usage: audio_app.py [-h] [--room ROOM] [--server SERVER] [--peer PEER]
                    [--port PORT] [--stun STUN] [--stun-port STUN_PORT]
                    [--endian {big,little}] [--mode {auto,synth,live}]
                    [--tone-freq TONE_FREQ] [--wav-play WAV_PLAY]
                    [--wav-record WAV_RECORD] [--auto-ptt AUTO_PTT]

options:
  --room ROOM           Matchmaker Room key (e.g. 1234)
  --server SERVER       HTTP Matchmaker URL (default: https://udp-matchmaker.lvmlabs.org)
  --peer PEER           Direct remote endpoint (<IP>:<Port>)
  --port PORT           Local UDP port to bind (default: random OS port)
  --endian {big,little} Header endianness (default: big)
  --mode {auto,synth,live} Audio backend mode (default: auto)
  --tone-freq TONE_FREQ Sine wave test tone frequency in Hz (default: 1000.0)
  --wav-play WAV_PLAY   Path to 16kHz mono WAV file to transmit on PTT
  --wav-record WAV_RECORD Path to record received PCM audio as WAV
  --auto-ptt AUTO_PTT   Automated PTT toggle interval in seconds (0 = disabled)
```
