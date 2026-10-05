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
    generate_morse_frames,
    generate_courtesy_tone_frames,
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
        if self.interactive:
            try:
                sys.stdout.write("\033[?25l")  # Hide cursor to prevent cursor flicker
                sys.stdout.flush()
            except Exception:
                pass

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
        if self.interactive:
            try:
                sys.stdout.write("\033[?25h\n")  # Restore cursor
                sys.stdout.flush()
            except Exception:
                pass

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
    Part 97 Auxiliary Simplex Station Controller & Audio Simulator.
    Manages the 50 Hz audio transmission loop, jitter-buffered reception,
    continuous PTT/COS signaling, 25 WPM CW station IDer, FCC legal ID timer,
    Time-Out Timer (TOT), and hardware line telemetry.
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
        callsign: str = "KO6LVM",
        cw_wpm: int = 25,
        cw_freq: float = 800.0,
        tot_limit_s: float = 180.0,
        id_interval_s: float = 600.0,
        auto_id: bool = True,
        courtesy_tone_style: str = "chime",
        hang_time_s: float = 0.1,
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

        # Station Identification & CW Beacon (FCC Part 97.119)
        self.callsign = (callsign or "KO6LVM").upper()
        self.cw_wpm = int(cw_wpm)
        self.cw_freq = float(cw_freq)
        self.id_interval_s = float(id_interval_s)
        self.auto_id = auto_id
        self.last_id_time = time.time()
        self.is_cw_iding = False
        self._cw_tx_frames = collections.deque()

        # Transmitter Time-Out Timer (TOT) (§97.213 / §97.109)
        self.tot_limit_s = float(tot_limit_s)
        self.ptt_start_time = 0.0
        self.tot_cutoff = False
        self.tot_lockout = False

        # Squelch Tail & Courtesy Chime
        self.courtesy_tone_style = courtesy_tone_style
        self.hang_time_s = float(hang_time_s)
        self.squelch_hang_until = 0.0

        # Sidetone queue for local playback during CW ID transmission
        self._sidetone_frames = collections.deque()

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
        """Returns True if the simplex channel is occupied (remote station transmitting or squelch hang)."""
        now = time.time()
        return self.jitter_buffer.is_receiving or (now < self.squelch_hang_until)

    @property
    def is_transmitting(self) -> bool:
        """Returns True if local transmitter PTT is asserted (Voice, Burst, or CW ID)."""
        if self.tot_cutoff:
            return False
        return (
            self.ptt_active
            or (time.time() < self.burst_active_until)
            or bool(self._cw_tx_frames)
        )

    @property
    def ptt_line(self) -> bool:
        """Direct status of the radio hardware PTT control line."""
        return self.is_transmitting

    @property
    def cos_line(self) -> bool:
        """Direct status of the radio hardware Carrier Operated Squelch (COS) detection line."""
        if self.is_transmitting:
            return False
        return self.is_channel_busy

    @property
    def id_remaining_s(self) -> float:
        """Seconds remaining on the 10-minute FCC Part 97 legal ID timer."""
        return max(0.0, (self.last_id_time + self.id_interval_s) - time.time())

    @property
    def tot_elapsed_s(self) -> float:
        """Elapsed seconds of active continuous PTT transmission for Time-Out Timer."""
        if not self.ptt_active or self.ptt_start_time == 0.0:
            return 0.0
        return max(0.0, time.time() - self.ptt_start_time)

    def _on_roip_data(self, packet: RoipPacket):
        self.rx_audio_frames += 1
        remote_dropped_ptt = (self.last_rx_remote_ptt and not packet.ptt)
        self.last_rx_remote_ptt = packet.ptt
        self.last_rx_remote_cos = packet.cos

        # Simplex radio rule: if local station is actively transmitting, RX is blinded
        if not self.is_transmitting:
            self.last_rx_db = calculate_rms_db(packet.payload)
            self.jitter_buffer.push(packet.sequence, packet.payload, packet.timestamp)

        # Courtesy Roger Beep / Chime when remote station finishes transmitting
        if remote_dropped_ptt and not self.is_transmitting:
            now = time.time()
            self.squelch_hang_until = now + self.hang_time_s
            if self.roger_beep:
                for f in generate_courtesy_tone_frames(style=self.courtesy_tone_style):
                    self._alert_frames.append(f)

    def _on_roip_heartbeat(self, packet: RoipPacket):
        self.rx_heartbeats += 1
        remote_dropped_ptt = (self.last_rx_remote_ptt and not packet.ptt)
        self.last_rx_remote_ptt = packet.ptt
        self.last_rx_remote_cos = packet.cos

        # Courtesy Roger Beep / Chime when remote station releases PTT
        if remote_dropped_ptt and not self.is_transmitting:
            now = time.time()
            self.squelch_hang_until = now + self.hang_time_s
            if self.roger_beep:
                for f in generate_courtesy_tone_frames(style=self.courtesy_tone_style):
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
        Transmits 648B RoIP Data frames when PTT / CW ID is active,
        enforces Time-Out Timer (TOT), handles 10-minute auto ID, and
        transmits 8B Heartbeats when idle to maintain NAT pinholes.
        """
        frame_interval_s = FRAME_DURATION_MS / 1000.0  # 0.020 s
        next_deadline = time.time()
        last_heartbeat_time = 0.0
        was_transmitting = False
        was_voice_transmitting = False
        drain_voice_frames = 0

        while self.running and self.udp_manager.connected:
            now = time.time()

            # 1. Enforce Time-Out Timer (TOT) on active voice PTT
            if self.ptt_active and self.tot_limit_s > 0:
                if (now - self.ptt_start_time) >= self.tot_limit_s:
                    self.tot_cutoff = True
                    self.tot_lockout = True
                    self.ptt_active = False
                    self.ptt_start_time = 0.0
                    for f in generate_courtesy_tone_frames("boop"):
                        self._alert_frames.append(f)
                    for _ in range(2):
                        self.roip.send_heartbeat(ptt=False, cos=self.is_channel_busy)
                    self.tx_heartbeats += 2
                    was_transmitting = False
                    was_voice_transmitting = False
                    self.audio_backend.flush_input()

            # 2. Check Automated 10-Minute FCC Legal ID Timer
            if (
                self.auto_id
                and not self.is_transmitting
                and not self.is_channel_busy
                and self.id_remaining_s <= 0.0
            ):
                self.trigger_cw_id(force=False)

            # 3. Transmission Priority: CW ID -> Voice PTT / Burst
            if self._cw_tx_frames:
                pcm_data = self._cw_tx_frames.popleft()
                self.last_tx_db = calculate_rms_db(pcm_data)
                self.roip.send_data(payload=pcm_data, ptt=True, cos=False)
                self.tx_audio_frames += 1
                self._sidetone_frames.append(pcm_data)
                was_transmitting = True
                was_voice_transmitting = False
                if not self._cw_tx_frames:
                    self.is_cw_iding = False
                    self.audio_backend.flush_input()

            elif self.is_transmitting:
                # Capture 320 samples (640 bytes) of PCM audio
                pcm_data = self.audio_backend.read_frame()
                self.last_tx_db = calculate_rms_db(pcm_data)

                # Simplex radio rule: transmitting RF squelches local receiver (COS=False)
                self.roip.send_data(payload=pcm_data, ptt=True, cos=False)
                self.tx_audio_frames += 1
                was_transmitting = True
                was_voice_transmitting = True
                drain_voice_frames = 2

            elif was_voice_transmitting and drain_voice_frames > 0 and self.audio_backend.has_buffered_input():
                # Drain trailing audio frames captured right before unkeying (max 2 frames)
                pcm_data = self.audio_backend.read_frame()
                self.last_tx_db = calculate_rms_db(pcm_data)
                self.roip.send_data(payload=pcm_data, ptt=True, cos=False)
                self.tx_audio_frames += 1
                drain_voice_frames -= 1
                if drain_voice_frames == 0 or not self.audio_backend.has_buffered_input():
                    was_voice_transmitting = False
                    self.audio_backend.flush_input()

            else:
                was_voice_transmitting = False
                if was_transmitting:
                    # Trailing speech frames fully drained: send clean unkey heartbeat
                    for _ in range(2):
                        self.roip.send_heartbeat(ptt=False, cos=self.is_channel_busy)
                    self.tx_heartbeats += 2
                    last_heartbeat_time = now
                    was_transmitting = False
                    self.audio_backend.flush_input()
                elif now - last_heartbeat_time >= 1.0:
                    # Idle: send 8-byte heartbeat at ~1 Hz keepalive
                    self.roip.send_heartbeat(ptt=False, cos=self.is_channel_busy)
                    self.tx_heartbeats += 1
                    last_heartbeat_time = now

                self.last_tx_db = -96.0

            next_deadline += frame_interval_s
            sleep_time = next_deadline - time.time()
            if sleep_time > 0:
                time.sleep(sleep_time)
            elif sleep_time < -frame_interval_s:
                next_deadline = time.time()

    def _rx_playback_loop(self):
        """
        Pulls decoded PCM frames at 50 Hz and streams to output.
        Enforces simplex rule: local transmission mutes receiver playback,
        while routing local sidetone for CW ID transmission.
        """
        frame_interval_s = FRAME_DURATION_MS / 1000.0
        next_deadline = time.time()

        while self.running and self.udp_manager.connected:
            # Check for remote carrier drop if jitter buffer timed out without an explicit unkey heartbeat
            if self.last_rx_remote_ptt and not self.jitter_buffer.is_receiving:
                self.last_rx_remote_ptt = False
                now = time.time()
                self.squelch_hang_until = now + self.hang_time_s
                if self.roger_beep:
                    for f in generate_courtesy_tone_frames(style=self.courtesy_tone_style):
                        self._alert_frames.append(f)

            if self._sidetone_frames:
                # Local sidetone for CW ID transmission
                sidetone_frame = self._sidetone_frames.popleft()
                self.audio_backend.write_frame(sidetone_frame)
                self.last_rx_db = calculate_rms_db(sidetone_frame)
            elif self.is_transmitting:
                # Simplex radio rule: transmitting mutes receiver speaker to prevent feedback
                _ = self.jitter_buffer.pop()
                self.audio_backend.write_frame(b"\x00" * BYTES_PER_FRAME)
                self.last_rx_db = -96.0
            else:
                # Play courtesy roger beep / chime or alert if queued
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

    def trigger_cw_id(self, force: bool = False) -> bool:
        """
        Transmits station identification in 25 WPM CW Morse Code (FCC Part 97.119).
        Queues frames for transmission, provides local sidetone to speaker, and resets ID timer.
        """
        if not force and self.busy_channel_lockout and self.is_channel_busy:
            self.lockout_notice_until = time.time() + 1.2
            return False

        self.audio_backend.flush_input()
        frames = generate_morse_frames(
            text=self.callsign,
            wpm=self.cw_wpm,
            freq=self.cw_freq,
        )
        self._cw_tx_frames.clear()
        for f in frames:
            self._cw_tx_frames.append(f)
        self.is_cw_iding = True
        self.last_id_time = time.time()
        return True

    def key_ptt(self) -> bool:
        """
        Activates PTT (Push-to-Talk pressed).
        Enforces Busy Channel Lockout (BCLO) and Time-Out Timer anti-hangup lockout.
        Returns True if PTT keyed, False if locked out by active channel or TOT.
        """
        if self.tot_lockout:
            return False

        if self.busy_channel_lockout and self.is_channel_busy:
            self.lockout_notice_until = time.time() + 1.2
            return False

        if not self.ptt_active:
            self.ptt_active = True
            self.ptt_start_time = time.time()
            self.tot_cutoff = False
            # Flush mic input queue so stale ambient audio buffered while idle is discarded
            self.audio_backend.flush_input()
        return True

    def unkey_ptt(self):
        """Deactivates PTT (Push-to-Talk released) and clears TOT anti-hangup lockout."""
        if self.ptt_active:
            self.ptt_active = False
            self.ptt_start_time = 0.0
        self.tot_lockout = False
        self.tot_cutoff = False

    def toggle_ptt(self) -> bool:
        """Toggles PTT state."""
        if self.ptt_active:
            self.unkey_ptt()
            return False
        else:
            return self.key_ptt()


def print_dashboard(sim: RoipAudioSimulator):
    """Renders an authentic FCC Part 97 Auxiliary Simplex Station Controller dashboard."""
    now = time.time()

    # Operating state & channel description
    if sim.tot_cutoff:
        operating_state = "TOT CUTOFF"
        channel_status = "TRANSMITTER INHIBITED (Release PTT to reset)"
    elif sim.is_cw_iding:
        operating_state = "CW ID BEACON"
        channel_status = f"TRANSMITTING ID: {sim.callsign} @ {sim.cw_wpm} WPM"
    elif sim.is_transmitting:
        operating_state = "TX (TRANSMITTING)"
        channel_status = "TRANSMIT ACTIVE (PTT Asserted / Local RX Muted)"
    elif sim.is_channel_busy:
        if now < sim.lockout_notice_until:
            operating_state = "BUSY LOCKOUT"
            channel_status = "TRANSMIT INHIBITED (Channel Occupied - BCLO Active)"
        else:
            operating_state = "RX (RECEIVING)"
            channel_status = "CARRIER DETECTED (COS Open / Receiving Audio)"
    else:
        operating_state = "STANDBY"
        channel_status = "SQUELCH CLOSED (Simplex Channel Clear)"

    # Hardware line status indicators
    if sim.ptt_active:
        ptt_line_str = "ON (VOICE)"
    elif sim.is_cw_iding:
        ptt_line_str = "ON (CW ID)"
    elif time.time() < sim.burst_active_until:
        ptt_line_str = "ON (TONE)"
    elif sim.tot_cutoff:
        ptt_line_str = "CUTOFF"
    else:
        ptt_line_str = "OFF"

    if sim.is_transmitting:
        cos_line_str = "MUTED (TX)"
    elif sim.is_channel_busy:
        cos_line_str = "CARRIER DETECT"
    else:
        cos_line_str = "CLOSED"

    # Legal ID timer
    rem_s = int(sim.id_remaining_s)
    mm = rem_s // 60
    ss = rem_s % 60
    if sim.is_cw_iding:
        id_str = f"SENDING ({sim.cw_wpm} WPM)"
    elif rem_s == 0:
        id_str = "DUE NOW!"
    elif rem_s < 60:
        id_str = f"DUE SOON ({mm:02d}:{ss:02d})"
    else:
        id_str = f"{mm:02d}:{ss:02d} ({sim.cw_wpm} WPM)"

    # Time-Out Timer (TOT) display
    if sim.tot_cutoff:
        tot_str = "EXPIRED (RELEASE PTT)"
    elif sim.ptt_active and sim.tot_limit_s > 0:
        elapsed = int(sim.tot_elapsed_s)
        limit = int(sim.tot_limit_s)
        fraction = min(1.0, elapsed / limit)
        bars = int(fraction * 10)
        tot_bar = "█" * bars + "░" * (10 - bars)
        tot_str = f"[{tot_bar}] {elapsed}s/{limit}s"
    elif sim.tot_limit_s > 0:
        tot_str = f"STANDBY ({int(sim.tot_limit_s)}s)"
    else:
        tot_str = "DISABLED"

    bclo_mode = "ON" if sim.busy_channel_lockout else "OFF"
    courtesy_mode = sim.courtesy_tone_style.upper() if sim.roger_beep else "OFF"

    backend_name = sim.audio_backend.__class__.__name__
    rtt_val = f"{sim.udp_manager.last_rtt_ms:.1f} ms" if sim.udp_manager.last_rtt_ms > 0 else "measuring"

    lines = [
        "\033[H",  # Move cursor to top-left without wiping the display (eliminates Windows flicker)
        "======================================================================\033[K\n",
        "         DXoIP PART 97 AUXILIARY SIMPLEX STATION CONTROLLER           \033[K\n",
        "======================================================================\033[K\n",
        f" Station ID      : {sim.callsign:<18} Station Type: AUX LINK (SIMPLEX)\033[K\n",
        f" Legal ID Timer  : [ {id_str:<18} ] TOT Status  : [ {tot_str:<18} ]\033[K\n",
        f" Peer Endpoint   : {sim.udp_manager.peer_addr[0]}:{sim.udp_manager.peer_addr[1]:<14} Link RTT    : {rtt_val:<18}\033[K\n",
        "----------------------------------------------------------------------\033[K\n",
        f" Controller Lines: PTT [ {ptt_line_str:<10} ]    COS [ {cos_line_str:<16} ]\033[K\n",
        f" Operating State : [ {operating_state:<20} ]\033[K\n",
        f" Channel Status  : {channel_status}\033[K\n",
        f" Channel Rules   : BCLO: [{bclo_mode}] | Courtesy Tone: [{courtesy_mode}]\033[K\n",
        "----------------------------------------------------------------------\033[K\n",
        f" Mic/Tx Audio    : {format_vu_meter(sim.last_tx_db)}\033[K\n",
        f" Spk/Rx Audio    : {format_vu_meter(sim.last_rx_db)}\033[K\n",
        "----------------------------------------------------------------------\033[K\n",
        f" Tx Audio Frames : {sim.tx_audio_frames:<6} (Heartbeats: {sim.tx_heartbeats:<4}) | Rx Frames: {sim.rx_audio_frames:<6} (Heartbeats: {sim.rx_heartbeats:<4})\033[K\n",
        f" Jitter Buffer   : {sim.jitter_buffer.queued_frames} frms ({sim.jitter_buffer.underrun_count} underruns, {sim.jitter_buffer.late_drop_count} drops) | Audio: {backend_name}\033[K\n",
        "======================================================================\033[K\n",
        " Controls: [HOLD SPACE/P] Push-to-Talk | [I] CW ID\033[K\n",
        "           [T] 1s Tone Burst | [H] Heartbeat | [Q] Disconnect & Shutdown\033[K\n",
        "\033[J",  # Clear anything remaining below dashboard
    ]
    sys.stdout.write("".join(lines))
    sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser(description="DXoIP Part 97 Auxiliary Simplex Station Controller")
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
    # Part 97 Auxiliary Station parameters
    parser.add_argument("--callsign", type=str, default="KO6LVM", help="Station Callsign (default: KO6LVM)")
    parser.add_argument("--cw-wpm", type=int, default=25, help="Morse code ID speed in WPM (default: 25)")
    parser.add_argument("--cw-freq", type=float, default=800.0, help="Morse code tone pitch in Hz (default: 800.0)")
    parser.add_argument("--tot", type=float, default=180.0, help="Time-Out Timer duration in seconds (default: 180s, 0=disabled)")
    parser.add_argument("--id-interval", type=float, default=10.0, help="FCC Part 97 Legal ID interval in minutes (default: 10.0)")
    parser.add_argument("--no-auto-id", action="store_true", help="Disable automatic CW ID beacon on 10-minute expiry")
    parser.add_argument("--courtesy-tone", choices=["chime", "single", "quindar", "none"], default="chime", help="Courtesy tone style (default: chime)")
    parser.add_argument("--hang-time", type=float, default=0.1, help="Squelch tail hang-time in seconds (default: 0.1)")
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
        err_msg = udp_mgr.last_error or "Connection failed or timed out."
        print(f"[!] {err_msg}")
        udp_mgr.stop()
        sys.exit(1)

    print(f"\n[+] CONNECTED to peer: {udp_mgr.peer_addr[0]}:{udp_mgr.peer_addr[1]}\n")

    # 3. Initialize Station Simulator
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
        callsign=args.callsign,
        cw_wpm=args.cw_wpm,
        cw_freq=args.cw_freq,
        tot_limit_s=args.tot,
        id_interval_s=args.id_interval * 60.0,
        auto_id=not args.no_auto_id,
        courtesy_tone_style=args.courtesy_tone,
        hang_time_s=args.hang_time,
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
                # Immediate initial dashboard display (clear once on start)
                if term.interactive:
                    sys.stdout.write("\033[2J\033[H")
                    sys.stdout.flush()
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
                        if not sim.tot_lockout:
                            last_ptt_press_time = time.time()
                            sim.key_ptt()
                    elif key_lower == "i":
                        sim.trigger_cw_id(force=False)
                    elif key_lower == "t":
                        sim.trigger_tone_burst(duration_s=1.0)
                    elif key_lower == "h":
                        sim.roip.send_heartbeat(ptt=sim.ptt_active, cos=sim.is_channel_busy)

        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            print("\n[*] Stopping Audio Simulator...")
            sim.stop()
            print("[*] Audio Simulator stopped.")


if __name__ == "__main__":
    main()
