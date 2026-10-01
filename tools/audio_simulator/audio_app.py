#!/usr/bin/env python3
"""
DXoIP Desktop Audio Simulator (audio_app.py)
A standalone test application for streaming 16 kHz 16-bit PCM RoIP audio,
continuous PTT/COS signaling, and receiving audio with jitter buffering.

Cleanly isolated under tools/audio_simulator/ to preserve zero external
dependencies in the core DXoIP codebase.
"""

import argparse
import os
import select
import sys
import collections
import threading
import time
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from udp_manager import UDPManager, parse_endpoint
from roip_udp import ROIPUDP, RoipPacket, AUDIO_PAYLOAD_SIZE, get_current_timestamp_us
from tools.audio_simulator.audio_backend import (
    get_audio_backend,
    calculate_rms_db,
    format_vu_meter,
    generate_tone_frames,
    BYTES_PER_FRAME,
    FRAME_DURATION_MS,
    SAMPLE_RATE,
)
from tools.audio_simulator.jitter_buffer import JitterBuffer


class TerminalController:
    """Handles non-blocking single-key input across Linux, macOS, and Windows."""

    def __init__(self):
        self.is_windows = (os.name == "nt")
        self.is_tty = sys.stdin.isatty()
        self.interactive = self.is_tty
        self.old_settings = None

    def __enter__(self):
        if self.is_windows:
            self.interactive = self.is_tty
            return self

        if self.is_tty:
            try:
                import termios
                import tty

                self.old_settings = termios.tcgetattr(sys.stdin)
                tty.setcbreak(sys.stdin.fileno())
            except Exception:
                self.interactive = False
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if not self.is_windows and self.is_tty and self.old_settings:
            try:
                import termios

                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self.old_settings)
            except Exception:
                pass

    def get_key(self, timeout: float = 0.05) -> str:
        """Returns a single character or line if available, or None."""
        if self.is_windows:
            try:
                import msvcrt

                deadline = time.time() + timeout
                while time.time() < deadline:
                    if msvcrt.kbhit():
                        ch = msvcrt.getwch()
                        # If extended key prefix (\x00 or \xe0), consume the next scan code
                        if ch in ("\x00", "\xe0"):
                            if msvcrt.kbhit():
                                msvcrt.getwch()
                            return None
                        return ch
                    time.sleep(0.005)
                return None
            except Exception:
                return None

        # POSIX (Linux, macOS)
        try:
            r, _, _ = select.select([sys.stdin], [], [], timeout)
            if not r:
                return None
            if self.is_tty:
                return sys.stdin.read(1)
            else:
                line = sys.stdin.readline()
                return line.strip() if line else None
        except Exception:
            return None


class WindowsTimerResolution:
    """Ensures 1ms timer resolution on Windows for precise 50 Hz audio streaming."""

    def __enter__(self):
        if os.name == "nt":
            try:
                import ctypes

                self._winmm = ctypes.windll.winmm
                self._winmm.timeBeginPeriod(1)
            except Exception:
                self._winmm = None
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if os.name == "nt" and getattr(self, "_winmm", None):
            try:
                self._winmm.timeEndPeriod(1)
            except Exception:
                pass


class RoipAudioSimulator:
    """
    Desktop Audio Simulator managing the 50 Hz audio transmission loop,
    jitter-buffered reception, continuous PTT/COS signaling, and UI.
    """

    def __init__(
        self,
        udp_manager: UDPManager,
        endianness: str = "!",
        audio_mode: str = "auto",
        tone_freq: float = 1000.0,
        wav_play_path: str = None,
        wav_record_path: str = None,
        auto_ptt_interval: float = 0.0,
        busy_channel_lockout: bool = True,
        roger_beep: bool = True,
    ):
        self.udp_manager = udp_manager
        self.roip = ROIPUDP(udp_manager=self.udp_manager, endianness=endianness)
        self.audio_backend = get_audio_backend(
            mode=audio_mode,
            record_path=wav_record_path,
            wav_play_path=wav_play_path,
        )
        if hasattr(self.audio_backend, "tone_freq"):
            self.audio_backend.tone_freq = tone_freq

        self.jitter_buffer = JitterBuffer(target_delay_frames=1, max_frames=4)
        self.auto_ptt_interval = auto_ptt_interval
        self.busy_channel_lockout = busy_channel_lockout
        self.roger_beep = roger_beep

        # Simplex State flags
        self.ptt_active = False
        self.burst_active_until = 0.0
        self.running = False
        self.lockout_notice_until = 0.0
        self._alert_frames = collections.deque()

        # Stats & Metrics
        self.tx_audio_frames = 0
        self.tx_heartbeats = 0
        self.rx_audio_frames = 0
        self.rx_heartbeats = 0
        self.last_rx_remote_ptt = False
        self.last_rx_remote_cos = False
        self.last_tx_db = -96.0
        self.last_rx_db = -96.0
        self.last_rtt_us = 0

        # Wire RoIP callbacks
        self.roip.on_data_received = self._on_roip_data
        self.roip.on_heartbeat_received = self._on_roip_heartbeat

    @property
    def is_channel_busy(self) -> bool:
        """Returns True if the simplex channel is occupied (remote station transmitting)."""
        return self.jitter_buffer.is_receiving or self.last_rx_remote_ptt

    @property
    def is_transmitting(self) -> bool:
        """Returns True if local PTT is keyed or a test burst is actively transmitting."""
        return self.ptt_active or (time.time() < self.burst_active_until)

    def _on_roip_data(self, packet: RoipPacket):
        self.rx_audio_frames += 1
        remote_dropped_ptt = (self.last_rx_remote_ptt and not packet.ptt)
        self.last_rx_remote_ptt = packet.ptt
        self.last_rx_remote_cos = packet.cos

        # Simplex radio rule: if local station is actively transmitting, RX is blinded
        if not self.is_transmitting:
            self.last_rx_db = calculate_rms_db(packet.payload)
            self.jitter_buffer.push(packet.sequence, packet.payload, packet.timestamp)

        # Courtesy Roger Beep when remote station finishes transmitting
        if remote_dropped_ptt and self.roger_beep and not self.is_transmitting:
            for f in generate_tone_frames(freq=1200.0, duration_ms=80, amplitude=12000):
                self._alert_frames.append(f)

    def _on_roip_heartbeat(self, packet: RoipPacket):
        self.rx_heartbeats += 1
        remote_dropped_ptt = (self.last_rx_remote_ptt and not packet.ptt)
        self.last_rx_remote_ptt = packet.ptt
        self.last_rx_remote_cos = packet.cos

        # Courtesy Roger Beep when remote station releases PTT
        if remote_dropped_ptt and self.roger_beep and not self.is_transmitting:
            for f in generate_tone_frames(freq=1200.0, duration_ms=80, amplitude=12000):
                self._alert_frames.append(f)

    def start(self):
        self.running = True
        self.audio_backend.start()

        self._tx_thread = threading.Thread(target=self._tx_loop, daemon=True, name="AudioTxLoop")
        self._rx_thread = threading.Thread(target=self._rx_playback_loop, daemon=True, name="AudioRxPlayback")
        self._tx_thread.start()
        self._rx_thread.start()

    def stop(self):
        self.running = False
        self.audio_backend.stop()
        self.udp_manager.stop()

    def _tx_loop(self):
        """
        Precise 50 Hz (20ms) transmission loop.
        Transmits 648B RoIP Data frames when PTT is keyed,
        or 8B Heartbeats when idle to maintain NAT pinholes.
        """
        frame_interval_s = FRAME_DURATION_MS / 1000.0  # 0.020 s
        next_deadline = time.time()
        last_heartbeat_time = 0.0
        was_transmitting = False

        while self.running and self.udp_manager.connected:
            now = time.time()

            if self.is_transmitting:
                # Capture 320 samples (640 bytes) of PCM audio
                pcm_data = self.audio_backend.read_frame()
                self.last_tx_db = calculate_rms_db(pcm_data)

                # Simplex radio rule: transmitting RF squelches local receiver (COS=False)
                self.roip.send_data(payload=pcm_data, ptt=True, cos=False)
                self.tx_audio_frames += 1
                was_transmitting = True

            elif was_transmitting and self.audio_backend.has_buffered_input():
                # Drain trailing audio frames captured right before unkeying
                pcm_data = self.audio_backend.read_frame()
                self.last_tx_db = calculate_rms_db(pcm_data)
                self.roip.send_data(payload=pcm_data, ptt=True, cos=False)
                self.tx_audio_frames += 1

            else:
                if was_transmitting:
                    # Trailing speech frames fully drained: send clean unkey heartbeat
                    self.roip.send_heartbeat(ptt=False, cos=self.jitter_buffer.is_receiving)
                    self.tx_heartbeats += 1
                    last_heartbeat_time = now
                    was_transmitting = False
                elif now - last_heartbeat_time >= 1.0:
                    # Idle: send 8-byte heartbeat at ~1 Hz keepalive
                    self.roip.send_heartbeat(ptt=False, cos=self.jitter_buffer.is_receiving)
                    self.tx_heartbeats += 1
                    last_heartbeat_time = now

                self.last_tx_db = -96.0

            next_deadline += frame_interval_s
            sleep_time = next_deadline - time.time()
            if sleep_time > 0:
                time.sleep(sleep_time)
            elif sleep_time < -frame_interval_s:
                # Running behind; catch up deadline
                next_deadline = time.time()

    def _rx_playback_loop(self):
        """
        Pulls decoded PCM frames at 50 Hz and streams to output.
        Enforces simplex rule: local transmission mutes receiver playback.
        """
        frame_interval_s = FRAME_DURATION_MS / 1000.0
        next_deadline = time.time()

        while self.running and self.udp_manager.connected:
            if self.is_transmitting:
                # Simplex radio rule: transmitting mutes receiver speaker to prevent feedback
                _ = self.jitter_buffer.pop()
                self.audio_backend.write_frame(b"\x00" * BYTES_PER_FRAME)
                self.last_rx_db = -96.0
            else:
                # Play courtesy roger beep if queued
                if self._alert_frames:
                    frame = self._alert_frames.popleft()
                    self.audio_backend.write_frame(frame)
                    self.last_rx_db = calculate_rms_db(frame)
                else:
                    frame = self.jitter_buffer.pop()
                    self.audio_backend.write_frame(frame)
                    if self.jitter_buffer.is_receiving:
                        self.last_rx_db = calculate_rms_db(frame)
                    else:
                        self.last_rx_db = -96.0

            next_deadline += frame_interval_s
            sleep_time = next_deadline - time.time()
            if sleep_time > 0:
                time.sleep(sleep_time)
            elif sleep_time < -frame_interval_s:
                next_deadline = time.time()

    def trigger_tone_burst(self, duration_s: float = 1.0) -> bool:
        """Triggers a temporary PTT transmission burst with a test tone."""
        if self.busy_channel_lockout and self.is_channel_busy:
            self.lockout_notice_until = time.time() + 1.2
            return False
        self.burst_active_until = time.time() + duration_s
        return True

    def key_ptt(self) -> bool:
        """
        Activates PTT (Push-to-Talk pressed).
        Enforces Busy Channel Lockout (BCLO) in simplex radio mode.
        Returns True if PTT keyed, False if locked out by active channel.
        """
        if self.busy_channel_lockout and self.is_channel_busy:
            self.lockout_notice_until = time.time() + 1.2
            return False

        if not self.ptt_active:
            self.ptt_active = True
            # Flush mic input queue so stale ambient audio buffered while idle is discarded
            self.audio_backend.flush_input()
        return True

    def unkey_ptt(self):
        """Deactivates PTT (Push-to-Talk released)."""
        if self.ptt_active:
            self.ptt_active = False

    def toggle_ptt(self) -> bool:
        """Toggles PTT state."""
        if self.ptt_active:
            self.unkey_ptt()
            return False
        else:
            return self.key_ptt()


def print_dashboard(sim: RoipAudioSimulator):
    """Renders a clean real-time status dashboard reflecting simplex radio state."""
    now = time.time()
    if sim.is_transmitting:
        operating_state = "TX (TRANSMITTING)"
        channel_status = "TRANSMIT ACTIVE (Local RX Muted)"
    elif sim.is_channel_busy:
        if now < sim.lockout_notice_until:
            operating_state = "BUSY LOCKOUT"
            channel_status = "TRANSMIT INHIBITED (Channel Busy!)"
        else:
            operating_state = "RX (RECEIVING)"
            channel_status = "CARRIER DETECTED (Squelch Open)"
    else:
        operating_state = "STANDBY"
        channel_status = "SQUELCH CLOSED (Channel Clear)"

    remote_ptt = "KEYED" if sim.last_rx_remote_ptt else "OFF"
    bclo_mode = "ON" if sim.busy_channel_lockout else "OFF"
    roger_mode = "ON" if sim.roger_beep else "OFF"

    backend_name = sim.audio_backend.__class__.__name__

    sys.stdout.write("\033[H\033[J")  # Clear screen and move cursor to top-left
    sys.stdout.write("======================================================================\n")
    sys.stdout.write("                 DXoIP ROIP-UDP SIMPLEX RADIO SIMULATOR               \n")
    sys.stdout.write("======================================================================\n")
    sys.stdout.write(f" Backend       : {backend_name}\n")
    sys.stdout.write(f" Peer Endpoint : {sim.udp_manager.peer_addr[0]}:{sim.udp_manager.peer_addr[1]}\n")
    sys.stdout.write(f" Radio Mode    : [ {operating_state:<20} ]\n")
    sys.stdout.write(f" Channel Status: [ {channel_status:<38} ]\n")
    sys.stdout.write(f" Remote PTT    : [ {remote_ptt:<6} ] | BCLO: [{bclo_mode}] | Roger Beep: [{roger_mode}]\n")
    sys.stdout.write("----------------------------------------------------------------------\n")
    sys.stdout.write(f" Mic/Tx Audio  : {format_vu_meter(sim.last_tx_db)}\n")
    sys.stdout.write(f" Spk/Rx Audio  : {format_vu_meter(sim.last_rx_db)}\n")
    sys.stdout.write("----------------------------------------------------------------------\n")
    sys.stdout.write(
        f" Tx Audio Frames: {sim.tx_audio_frames:<6} | Tx Heartbeats: {sim.tx_heartbeats:<6}\n"
    )
    sys.stdout.write(
        f" Rx Audio Frames: {sim.rx_audio_frames:<6} | Rx Heartbeats: {sim.rx_heartbeats:<6}\n"
    )
    sys.stdout.write(
        f" Jitter Queued  : {sim.jitter_buffer.queued_frames:<6} | Underruns    : {sim.jitter_buffer.underrun_count:<6}\n"
    )
    sys.stdout.write("======================================================================\n")
    sys.stdout.write(" Controls: [HOLD SPACE/P] Push-to-Talk | [T] 1s Tone Burst | [H] Heartbeat | [Q] Quit\n")
    sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser(description="DXoIP Desktop Audio Simulator")
    parser.add_argument("--room", type=str, default=None, help="Matchmaker Room key (e.g. 1234)")
    parser.add_argument("--server", type=str, default="https://udp-matchmaker.lvmlabs.org", help="HTTP Matchmaker URL")
    parser.add_argument("--peer", type=str, default=None, help="Direct remote endpoint (<IP>:<Port>)")
    parser.add_argument("--port", type=int, default=0, help="Local UDP port to bind (default: random OS port)")
    parser.add_argument("--stun", type=str, default="stun.l.google.com", help="STUN server host")
    parser.add_argument("--stun-port", type=int, default=19302, help="STUN server port")
    parser.add_argument("--endian", choices=["big", "little"], default="big", help="Header endianness")
    parser.add_argument("--mode", choices=["auto", "synth", "live"], default="auto", help="Audio backend mode")
    parser.add_argument("--tone-freq", type=float, default=1000.0, help="Sine wave test tone frequency in Hz")
    parser.add_argument("--wav-play", type=str, default=None, help="Path to 16kHz mono WAV file to transmit on PTT")
    parser.add_argument("--wav-record", type=str, default=None, help="Path to record received PCM audio as WAV")
    parser.add_argument("--auto-ptt", type=float, default=0.0, help="Automated PTT toggle interval in seconds (0 = disabled)")
    parser.add_argument("--ptt-hold-timeout", type=float, default=0.35, help="Hold timeout in seconds for terminal Push-to-Talk (default: 0.35s)")
    parser.add_argument("--allow-doubling", action="store_true", help="Disable Busy Channel Lockout (allow simultaneous transmitting)")
    parser.add_argument("--no-roger-beep", action="store_true", help="Disable courtesy tone / roger beep on remote unkey")
    args = parser.parse_args()

    endian_char = "!" if args.endian == "big" else "<"

    # 1. Initialize UDP Transport
    udp_mgr = UDPManager(local_port=args.port, stun_host=args.stun, stun_port=args.stun_port)
    print(f"[*] Bound local UDP port {udp_mgr.local_port}")
    print(f"[*] Resolving STUN public endpoint ({args.stun}:{args.stun_port})...")

    try:
        wan_ep, lan_ep = udp_mgr.get_endpoints()
        print(f"\n==========================================")
        print(f" PUBLIC (WAN) ENDPOINT : {wan_ep[0]}:{wan_ep[1]}")
        print(f" LOCAL (LAN) ENDPOINT  : {lan_ep[0]}:{lan_ep[1]}")
        print(f"==========================================\n")
    except Exception as e:
        print(f"[!] STUN resolution failed: {e}")
        udp_mgr.stop()
        sys.exit(1)

    # 2. Setup connection
    if args.room:
        print(f"[*] Connecting via Room '{args.room}' on {args.server}...")
        connect_thread = threading.Thread(
            target=udp_mgr.connect_room,
            args=(args.room, args.server),
            daemon=True,
        )
    elif args.peer:
        peer_ip, peer_port = parse_endpoint(args.peer)
        print(f"[*] Punching directly towards {peer_ip}:{peer_port}...")
        connect_thread = threading.Thread(
            target=udp_mgr.connect_peer,
            args=(peer_ip, peer_port),
            daemon=True,
        )
    else:
        # Prompt
        print("Choose connection mode:")
        print(" [1] Room Key (Matchmaker)")
        print(" [2] Direct IP:Port")
        choice = input("Select [1/2] (default 1): ").strip()
        if choice == "2":
            p_ep = input("Enter peer endpoint (<IP>:<Port>): ").strip()
            peer_ip, peer_port = parse_endpoint(p_ep)
            connect_thread = threading.Thread(
                target=udp_mgr.connect_peer,
                args=(peer_ip, peer_port),
                daemon=True,
            )
        else:
            r_key = input("Enter Room Key (default 1234): ").strip() or "1234"
            connect_thread = threading.Thread(
                target=udp_mgr.connect_room,
                args=(r_key, args.server),
                daemon=True,
            )

    connect_thread.start()

    # Wait for connection
    print("[*] Waiting for P2P connection to establish...")
    while not udp_mgr.connected and connect_thread.is_alive():
        time.sleep(0.1)

    if not udp_mgr.connected:
        print("[!] Connection failed or timed out.")
        udp_mgr.stop()
        sys.exit(1)

    print(f"\n[+] CONNECTED to peer: {udp_mgr.peer_addr[0]}:{udp_mgr.peer_addr[1]}\n")

    # 3. Initialize Audio Simulator
    sim = RoipAudioSimulator(
        udp_manager=udp_mgr,
        endianness=endian_char,
        audio_mode=args.mode,
        tone_freq=args.tone_freq,
        wav_play_path=args.wav_play,
        wav_record_path=args.wav_record,
        auto_ptt_interval=args.auto_ptt,
        busy_channel_lockout=not args.allow_doubling,
        roger_beep=not args.no_roger_beep,
    )
    with WindowsTimerResolution():
        sim.start()

        # Automated PTT thread if requested
        if args.auto_ptt > 0:
            def auto_ptt_worker():
                while sim.running:
                    time.sleep(args.auto_ptt)
                    sim.toggle_ptt()

            threading.Thread(target=auto_ptt_worker, daemon=True, name="AutoPTT").start()

        if os.name == "nt":
            # Enable ANSI escape sequences on Windows console/PowerShell
            try:
                os.system("")
            except Exception:
                pass

        # 4. Interactive Loop (Push-to-Talk: Hold SPACE/P to talk, release to unkey)
        PTT_HOLD_TIMEOUT = args.ptt_hold_timeout
        last_ptt_press_time = 0.0

        try:
            with TerminalController() as term:
                # Immediate initial dashboard display
                if term.interactive:
                    print_dashboard(sim)

                last_dashboard_update = time.time()
                while sim.running and udp_mgr.connected:
                    now = time.time()

                    # Push-to-Talk: Automatically unkey when key release timeout is reached
                    if sim.ptt_active and (now - last_ptt_press_time > PTT_HOLD_TIMEOUT):
                        sim.unkey_ptt()

                    # Update dashboard ~5 times/second
                    if now - last_dashboard_update >= 0.2:
                        if term.interactive:
                            print_dashboard(sim)
                        last_dashboard_update = now

                    key = term.get_key(timeout=0.03)
                    if not key:
                        continue

                    key_lower = key.lower()
                    if key_lower in ("q", "\x03"):  # 'q' or Ctrl-C
                        break
                    elif key_lower in (" ", "p"):
                        last_ptt_press_time = time.time()
                        sim.key_ptt()
                    elif key_lower == "t":
                        sim.trigger_tone_burst(duration_s=1.0)
                    elif key_lower == "h":
                        sim.roip.send_heartbeat(ptt=sim.ptt_active, cos=sim.jitter_buffer.is_receiving)

        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            print("\n[*] Stopping Audio Simulator...")
            sim.stop()
            print("[*] Audio Simulator stopped.")


if __name__ == "__main__":
    main()
