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
    BYTES_PER_FRAME,
    FRAME_DURATION_MS,
    SAMPLE_RATE,
)
from tools.audio_simulator.jitter_buffer import JitterBuffer


class TerminalController:
    """Handles non-blocking single-key or line input across platforms."""

    def __init__(self):
        self.is_tty = sys.stdin.isatty()
        self.old_settings = None

    def __enter__(self):
        if self.is_tty:
            try:
                import termios
                import tty

                self.old_settings = termios.tcgetattr(sys.stdin)
                tty.setcbreak(sys.stdin.fileno())
            except Exception:
                self.is_tty = False
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.is_tty and self.old_settings:
            try:
                import termios

                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self.old_settings)
            except Exception:
                pass

    def get_key(self, timeout: float = 0.05) -> str:
        """Returns a single character or line if available, or None."""
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

        self.jitter_buffer = JitterBuffer(target_delay_frames=2, max_frames=20)
        self.auto_ptt_interval = auto_ptt_interval

        # State flags
        self.ptt_active = False
        self.burst_active_until = 0.0
        self.running = False

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

    def _on_roip_data(self, packet: RoipPacket):
        self.rx_audio_frames += 1
        self.last_rx_remote_ptt = packet.ptt
        self.last_rx_remote_cos = packet.cos
        self.last_rx_db = calculate_rms_db(packet.payload)

        # Push audio payload into jitter buffer for playback
        self.jitter_buffer.push(packet.sequence, packet.payload, packet.timestamp)

    def _on_roip_heartbeat(self, packet: RoipPacket):
        self.rx_heartbeats += 1
        self.last_rx_remote_ptt = packet.ptt
        self.last_rx_remote_cos = packet.cos

    @property
    def is_transmitting(self) -> bool:
        """Returns True if PTT is keyed either manually or during a burst test."""
        return self.ptt_active or (time.time() < self.burst_active_until)

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

        while self.running and self.udp_manager.connected:
            now = time.time()
            cos_state = self.jitter_buffer.is_receiving

            if self.is_transmitting:
                # Capture 320 samples (640 bytes) of PCM audio
                pcm_data = self.audio_backend.read_frame()
                self.last_tx_db = calculate_rms_db(pcm_data)

                # Transmit 648-byte RoIP Data packet
                self.roip.send_data(payload=pcm_data, ptt=True, cos=cos_state)
                self.tx_audio_frames += 1

            else:
                # Idle: send 8-byte heartbeat at ~1 Hz keepalive
                if now - last_heartbeat_time >= 1.0:
                    self.roip.send_heartbeat(ptt=False, cos=cos_state)
                    self.tx_heartbeats += 1
                    last_heartbeat_time = now
                self.last_tx_db = -96.0

            next_deadline += frame_interval_s
            sleep_time = next_deadline - time.time()
            if sleep_time > 0:
                time.sleep(sleep_time)
            else:
                # Running behind; catch up deadline
                next_deadline = time.time()

    def _rx_playback_loop(self):
        """
        Pulls decoded PCM frames from the JitterBuffer at 50 Hz and streams to output.
        """
        frame_interval_s = FRAME_DURATION_MS / 1000.0
        next_deadline = time.time()

        while self.running and self.udp_manager.connected:
            frame = self.jitter_buffer.pop()
            self.audio_backend.write_frame(frame)

            next_deadline += frame_interval_s
            sleep_time = next_deadline - time.time()
            if sleep_time > 0:
                time.sleep(sleep_time)
            else:
                next_deadline = time.time()

    def trigger_tone_burst(self, duration_s: float = 1.0):
        """Triggers a temporary PTT transmission burst with a test tone."""
        self.burst_active_until = time.time() + duration_s

    def key_ptt(self):
        """Activates PTT (Push-to-Talk pressed)."""
        if not self.ptt_active:
            self.ptt_active = True
            # Send an immediate heartbeat with PTT=True
            self.roip.send_heartbeat(ptt=True, cos=self.jitter_buffer.is_receiving)

    def unkey_ptt(self):
        """Deactivates PTT (Push-to-Talk released)."""
        if self.ptt_active:
            self.ptt_active = False
            # Send an immediate heartbeat with PTT=False
            self.roip.send_heartbeat(ptt=False, cos=self.jitter_buffer.is_receiving)

    def toggle_ptt(self):
        """Toggles PTT state (for automated scripts or backwards compatibility)."""
        if self.ptt_active:
            self.unkey_ptt()
        else:
            self.key_ptt()


def print_dashboard(sim: RoipAudioSimulator):
    """Renders a clean real-time status dashboard."""
    tx_state = "KEYED (TX)" if sim.is_transmitting else "IDLE"
    rx_carrier = "CARRIER DETECTED (COS)" if sim.jitter_buffer.is_receiving else "SQUELCH CLOSED"
    remote_ptt = "KEYED" if sim.last_rx_remote_ptt else "OFF"

    backend_name = sim.audio_backend.__class__.__name__

    sys.stdout.write("\033[H\033[J")  # Clear screen and move cursor to top-left
    sys.stdout.write("======================================================================\n")
    sys.stdout.write("                 DXoIP ROIP-UDP AUDIO SIMULATOR                       \n")
    sys.stdout.write("======================================================================\n")
    sys.stdout.write(f" Backend       : {backend_name}\n")
    sys.stdout.write(f" Peer Endpoint : {sim.udp_manager.peer_addr[0]}:{sim.udp_manager.peer_addr[1]}\n")
    sys.stdout.write(f" Local PTT     : [ {tx_state:<10} ] | Remote PTT: [ {remote_ptt:<6} ]\n")
    sys.stdout.write(f" Local COS     : [ {rx_carrier:<22} ]\n")
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
    parser.add_argument("--gui", action="store_true", help="Launch graphical Push-to-Talk UI window")
    parser.add_argument("--ptt-hold-timeout", type=float, default=0.5, help="Hold timeout in seconds for terminal Push-to-Talk (default: 0.5s)")
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

    import platform

    def has_gui_display():
        if platform.system() in ("Darwin", "Windows"):
            return True
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))

    # 3. Initialize Audio Simulator
    sim = RoipAudioSimulator(
        udp_manager=udp_mgr,
        endianness=endian_char,
        audio_mode=args.mode,
        tone_freq=args.tone_freq,
        wav_play_path=args.wav_play,
        wav_record_path=args.wav_record,
        auto_ptt_interval=args.auto_ptt,
    )
    sim.start()

    # Launch GUI immediately if requested and available
    if args.gui:
        try:
            from tools.audio_simulator.gui import launch_gui, TKINTER_AVAILABLE
            if not TKINTER_AVAILABLE or not has_gui_display():
                print("[!] GUI requested but Tkinter or desktop display is not available.")
                print("[*] Falling back to interactive Terminal Push-to-Talk mode.\n")
            else:
                print("[*] Launching graphical Push-to-Talk window...")
                launch_gui(sim)
                return
        except Exception as e:
            print(f"[!] Could not launch GUI ({e}). Falling back to terminal Push-to-Talk mode.\n")

    # In terminal mode, wait for P2P connection before opening dashboard
    print("[*] Waiting for P2P connection to establish...")
    while not udp_mgr.connected and connect_thread.is_alive():
        time.sleep(0.1)

    if not udp_mgr.connected:
        print("[!] Connection failed or timed out.")
        sim.stop()
        sys.exit(1)

    print(f"\n[+] CONNECTED to peer: {udp_mgr.peer_addr[0]}:{udp_mgr.peer_addr[1]}\n")

    # Automated PTT thread if requested
    if args.auto_ptt > 0:
        def auto_ptt_worker():
            while sim.running:
                time.sleep(args.auto_ptt)
                sim.toggle_ptt()

        threading.Thread(target=auto_ptt_worker, daemon=True, name="AutoPTT").start()

    # 4. Interactive Loop (Push-to-Talk: Hold SPACE/P to talk, release to unkey)
    PTT_HOLD_TIMEOUT = args.ptt_hold_timeout
    last_ptt_press_time = 0.0

    try:
        with TerminalController() as term:
            last_dashboard_update = 0.0
            while sim.running and udp_mgr.connected:
                now = time.time()

                # Push-to-Talk: Automatically unkey when key release timeout is reached
                if sim.ptt_active and (now - last_ptt_press_time > PTT_HOLD_TIMEOUT):
                    sim.unkey_ptt()

                # Update dashboard ~5 times/second
                if now - last_dashboard_update >= 0.2:
                    if term.is_tty:
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
