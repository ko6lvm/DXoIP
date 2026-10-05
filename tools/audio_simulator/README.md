# DXoIP Part 97 Auxiliary Simplex Station Controller & Audio Simulator

An authentic **FCC Part 97 Auxiliary Simplex Station Controller** and test application for real-time **16 kHz 16-bit linear PCM RoIP audio streaming**, **continuous PTT/COS signaling**, **25 WPM CW station identification**, and **Time-Out Timer (TOT) safety enforcement** over peer-to-peer UDP connections.

> [!NOTE]
> **Clean Isolation:** This station controller is strictly isolated in `tools/audio_simulator/`. The core DXoIP codebase (`roip_udp.py`, `udp_manager.py`) and minimal headless entry point (`peer.py`) remain 100% zero-dependency and compatible with MicroPython on the Raspberry Pi Pico 2 W.

---

## Part 97 Station Features

* **FCC Part 97 Station Identification (§97.119):**
  * **Station Callsign:** Default station ID set to **`KO6LVM`** (configurable via `--callsign`).
  * **25 WPM CW Morse IDer:** Generates standard PARIS-timed 25 WPM Morse code audio (800 Hz) with 5ms raised-cosine keying envelope to eliminate key clicks.
  * **10-Minute Legal ID Timer:** Displays a real-time countdown timer (`ID Due In: MM:SS`). When the 10-minute timer expires, the station automatically transmits its CW ID beacon once the channel is clear.
  * **Local Sidetone:** Automatically routes CW Morse audio to the local speaker during transmission so the operator hears their own identifier.
  * **Manual ID Key (`[I]`):** Instantly key and transmit the station CW ID beacon at any time.

* **Transmitter Time-Out Timer (TOT) (§97.213 / §97.109):**
  * Regulatory safety cut-off (default `180.0`s / 3 minutes, configurable via `--tot`).
  * Real-time visual progress bar during transmission: `TOT: [████████░░] 24s/180s`.
  * **Automatic Cutoff & Anti-Hangup Lockout:** If transmission exceeds the TOT limit, the transmitter forcibly unkeys, plays an audible warning boop, and locks out transmission until the operator physically releases the PTT key.

* **Radio Interface & Controller Lines:**
  * Simulates direct interfacing to the two-way radio hardware:
    * **`PTT Line`:** Reflects transmitter hardware state (`ON (VOICE)`, `ON (CW ID)`, `ON (TONE)`, `CUTOFF`, `OFF`).
    * **`COS Line`:** Reflects receiver Carrier Operated Squelch state (`OPEN (CARRIER DETECT)`, `CLOSED (SQUELCHED)`, `MUTED (TX)`).
  * **Simplex Half-Duplex Rule:** Transmitting RF squelches local receiver playback to eliminate acoustic feedback.
  * **Busy Channel Lockout (BCLO):** Inhibits local transmission while remote carrier (`COS`) is detected to prevent doubling on the simplex frequency (can be bypassed with `--allow-doubling`).
  * **Squelch Tail & Courtesy Chime:** 100ms carrier hang-time delay followed by a dual-tone auxiliary link courtesy chime (880 Hz / 1046 Hz) when the remote station drops carrier.

* **Dual-Mode Audio Engine:**
  * **Live Hardware Audio (`mode=live`):** Streams from your physical microphone and plays out to your speakers using `sounddevice` (when hardware and `libportaudio2` are available).
  * **Synthetic Audio / File Engine (`mode=synth`):** Built-in zero-dependency generator producing 1 kHz sine wave test tones, CW Morse code, courtesy chimes, or playing from 16 kHz WAV files. Can also record received audio to `.wav`.

* **Tactical Terminal Dashboard:**
  * Full Part 97 Auxiliary Link console showing station ID, legal ID timer, TOT status bar, hardware controller lines, operating state, dBFS VU meters, and network telemetry (RTT, jitter queue, packet stats).

---

## Setup & Virtual Environment

You can run this station controller using standard Python (zero external dependencies required for synthetic audio / CW) or install `sounddevice` for live hardware audio:

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
*(If PortAudio is not installed, the controller will automatically use its built-in synthetic audio engine without failing!)*

---

## Usage

### Test 1: Local Loopback Test (Two Terminals on Same Machine)

**Terminal 1 (Station KO6LVM):**
```bash
python3 tools/audio_simulator/audio_app.py --room 9999 --port 15001 --callsign KO6LVM
```

**Terminal 2 (Station W6ABC):**
```bash
python3 tools/audio_simulator/audio_app.py --room 9999 --port 15002 --callsign W6ABC
```

Once connected:
* Press and hold `[SPACE]` or `[P]` in Terminal 1: Transmits audio with PTT active; observe Terminal 2 squelch open (`COS Line: CARRIER DETECT`).
* Release `[SPACE]`: Terminal 1 unkeys; Terminal 2 plays the dual-tone courtesy chime.
* Press `[I]` in Terminal 1: Sends 25 WPM CW Morse ID for `KO6LVM`; hear sidetone in Terminal 1 and Morse code received in Terminal 2.

---

### Test 2: Connecting Remote Peers Across NAT (Matchmaker)

**Device A:**
```bash
python3 tools/audio_simulator/audio_app.py --room 5432 --callsign KO6LVM
```

**Device B:**
```bash
python3 tools/audio_simulator/audio_app.py --room 5432 --callsign N0CALL
```

---

## Interactive Controls

| Key | Action |
| :--- | :--- |
| `[HOLD SPACE]` / `[P]` | **Push-to-Talk (PTT):** Transmit voice or test audio. Release to unkey. |
| `[I]` | **Station CW ID:** Transmit station identification (`KO6LVM`) in 25 WPM Morse code. |
| `[T]` | **Tone Burst:** Transmit 1-second 1 kHz sine wave test tone. |
| `[H]` | **Heartbeat:** Transmit 8-byte RoIP keepalive packet. |
| `[Q]` / `[Ctrl+C]` | **Power Down:** Disconnect and cleanly shut down the station. |

---

## Command-Line Options

```text
usage: audio_app.py [-h] [--room ROOM] [--server SERVER] [--peer PEER]
                    [--port PORT] [--stun STUN] [--stun-port STUN_PORT]
                    [--endian {big,little}] [--mode {auto,synth,live}]
                    [--tone-freq TONE_FREQ] [--wav-play WAV_PLAY]
                    [--wav-record WAV_RECORD] [--auto-ptt AUTO_PTT]
                    [--ptt-hold-timeout PTT_HOLD_TIMEOUT] [--allow-doubling]
                    [--no-roger-beep] [--callsign CALLSIGN] [--cw-wpm CW_WPM]
                    [--cw-freq CW_FREQ] [--tot TOT] [--id-interval ID_INTERVAL]
                    [--no-auto-id] [--courtesy-tone {chime,single,quindar,none}]
                    [--hang-time HANG_TIME]

Part 97 Auxiliary Station Options:
  --callsign CALLSIGN   Station Callsign (default: KO6LVM)
  --cw-wpm CW_WPM       Morse code ID speed in WPM (default: 25)
  --cw-freq CW_FREQ     Morse code tone pitch in Hz (default: 800.0)
  --tot TOT             Time-Out Timer duration in seconds (default: 180s, 0=disabled)
  --id-interval ID_INT  FCC Part 97 Legal ID interval in minutes (default: 10.0)
  --no-auto-id          Disable automatic CW ID beacon on 10-minute expiry
  --courtesy-tone STYLE Courtesy tone style: chime, single, quindar, none (default: chime)
  --hang-time HANG_TIME Squelch tail hang-time in seconds (default: 0.1)

Transport & Audio Options:
  --room ROOM           Matchmaker Room key (e.g. 1234)
  --server SERVER       HTTP Matchmaker URL (default: https://udp-matchmaker.lvmlabs.org)
  --peer PEER           Direct remote endpoint (<IP>:<Port>)
  --port PORT           Local UDP port to bind (default: random OS port)
  --endian {big,little} Header endianness (default: big)
  --mode {auto,synth,live} Audio backend mode (default: auto)
  --tone-freq TONE_FREQ Sine wave test tone frequency in Hz (default: 1000.0)
  --wav-play WAV_PLAY   Path to 16kHz mono WAV file to transmit on PTT
  --wav-record WAV_REC  Path to record received PCM audio as WAV
  --auto-ptt AUTO_PTT   Automated PTT toggle interval in seconds (0 = disabled)
  --allow-doubling      Disable Busy Channel Lockout (allow simultaneous transmitting)
  --no-roger-beep       Disable courtesy tone / roger beep on remote unkey
```
