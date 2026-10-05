"""
Unit and Integration Tests for RoIP Audio Simulator (tests/test_audio_simulator.py)
Tests:
1. Synthetic audio generation (16 kHz, 16-bit signed PCM mono, 640 bytes/20ms).
2. Jitter buffer queueing, squelch detection, and underruns.
3. Full P2P loopback RoIP-UDP audio frame transmission (648B) and heartbeats (8B).
"""

import math
import os
import struct
import tempfile
import threading
import time
import unittest
import wave

from roip_udp import (
    ROIPUDP,
    RoipPacket,
    ROIP_MAGIC,
    ROIP_DATA_SIZE,
    ROIP_HEARTBEAT_SIZE,
    AUDIO_PAYLOAD_SIZE,
    FLAG_PTT,
    FLAG_COS,
)
from udp_manager import UDPManager
from tools.audio_simulator.audio_backend import (
    SyntheticAudioBackend,
    calculate_rms_db,
    format_vu_meter,
    generate_tone_frames,
    generate_morse_frames,
    generate_courtesy_tone_frames,
    BYTES_PER_FRAME,
    SAMPLES_PER_FRAME,
    SAMPLE_RATE,
    CHANNELS,
    SAMPLE_WIDTH,
)
from tools.audio_simulator.jitter_buffer import JitterBuffer


class TestAudioBackend(unittest.TestCase):
    """Verifies audio generation, math, and WAV file recording."""

    def setUp(self):
        self.backend = SyntheticAudioBackend(tone_freq=1000.0)
        self.backend.start()

    def tearDown(self):
        self.backend.stop()

    def test_frame_dimensions(self):
        """Frame must be exactly 640 bytes representing 320 samples of 16-bit PCM."""
        frame = self.backend.read_frame()
        self.assertEqual(len(frame), BYTES_PER_FRAME)
        self.assertEqual(len(frame), 640)

        # Unpack as signed 16-bit integers
        samples = struct.unpack(f"<{SAMPLES_PER_FRAME}h", frame)
        self.assertEqual(len(samples), 320)
        for s in samples:
            self.assertGreaterEqual(s, -32768)
            self.assertLessEqual(s, 32767)

    def test_silence_mode(self):
        """Silence mode should produce 640 null bytes."""
        self.backend.tone_mode = "silence"
        frame = self.backend.read_frame()
        self.assertEqual(frame, b"\x00" * 640)
        rms = calculate_rms_db(frame)
        self.assertEqual(rms, -96.0)

    def test_rms_calculation(self):
        """Verify RMS calculation and VU meter string formatting."""
        self.backend.tone_mode = "tone"
        frame = self.backend.read_frame()
        db = calculate_rms_db(frame)
        # Expected around -6 to -7 dBFS for amplitude 16000
        self.assertGreater(db, -10.0)
        self.assertLess(db, 0.0)

        vu_str = format_vu_meter(db)
        self.assertIn("[", vu_str)
        self.assertIn("dB", vu_str)

    def test_wav_recording(self):
        """Verify incoming frames can be recorded into a valid WAV file."""
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            wav_path = tf.name

        try:
            rec_backend = SyntheticAudioBackend(tone_freq=1000.0, record_path=wav_path)
            rec_backend.start()

            # Write 5 frames of test audio
            for _ in range(5):
                frame = rec_backend.read_frame()
                rec_backend.write_frame(frame)

            rec_backend.stop()

            # Verify the WAV file on disk
            with wave.open(wav_path, "rb") as wf:
                self.assertEqual(wf.getnchannels(), 1)
                self.assertEqual(wf.getsampwidth(), 2)
                self.assertEqual(wf.getframerate(), 16000)
                self.assertEqual(wf.getnframes(), 320 * 5)
        finally:
            if os.path.exists(wav_path):
                os.remove(wav_path)


class TestJitterBuffer(unittest.TestCase):
    """Verifies jitter buffer queueing, delay thresholds, and squelch tracking."""

    def test_queueing_and_playback(self):
        jb = JitterBuffer(target_delay_frames=2, max_frames=10, squelch_timeout_s=0.2)

        # Initially empty and buffering
        self.assertEqual(jb.pop(), b"\x00" * 640)
        self.assertFalse(jb.is_receiving)

        # Push frame 1
        frame1 = b"\x01\x00" * 320
        jb.push(seq=0, pcm_data=frame1)
        self.assertTrue(jb.is_receiving)
        # Still buffering (target delay is 2)
        self.assertEqual(jb.pop(), b"\x00" * 640)

        # Push frame 2
        frame2 = b"\x02\x00" * 320
        jb.push(seq=1, pcm_data=frame2)

        # Now buffer has reached target depth of 2, so popping releases frames
        out1 = jb.pop()
        self.assertEqual(out1, frame1)
        out2 = jb.pop()
        self.assertEqual(out2, frame2)

        # Now empty: underrun occurs
        out3 = jb.pop()
        self.assertEqual(out3, b"\x00" * 640)
        self.assertGreater(jb.underrun_count, 0)

    def test_squelch_timeout(self):
        jb = JitterBuffer(target_delay_frames=1, squelch_timeout_s=0.05)
        jb.push(seq=0, pcm_data=b"\x00" * 640)
        self.assertTrue(jb.is_receiving)

        time.sleep(0.08)
        self.assertFalse(jb.is_receiving)


class TestRoipUdpLoopback(unittest.TestCase):
    """Verifies real loopback transmission of RoIP 648B data and 8B heartbeat frames over UDP."""

    def setUp(self):
        self.udp_a = UDPManager(local_port=0, stun_host="127.0.0.1", stun_port=9999)
        self.udp_b = UDPManager(local_port=0, stun_host="127.0.0.1", stun_port=9999)

        self.roip_a = ROIPUDP(udp_manager=self.udp_a)
        self.roip_b = ROIPUDP(udp_manager=self.udp_b)

        # Connect directly to local loopback ports
        self.udp_a.peer_addr = ("127.0.0.1", self.udp_b.local_port)
        self.udp_a.candidate_addrs = [("127.0.0.1", self.udp_b.local_port)]
        self.udp_a.connected = True
        self.udp_a.running = True
        self.udp_a._rx_thread = threading.Thread(target=self.udp_a._receive_loop, daemon=True)
        self.udp_a._rx_thread.start()

        self.udp_b.peer_addr = ("127.0.0.1", self.udp_a.local_port)
        self.udp_b.candidate_addrs = [("127.0.0.1", self.udp_a.local_port)]
        self.udp_b.connected = True
        self.udp_b.running = True
        self.udp_b._rx_thread = threading.Thread(target=self.udp_b._receive_loop, daemon=True)
        self.udp_b._rx_thread.start()

    def tearDown(self):
        self.udp_a.stop()
        self.udp_b.stop()

    def test_roip_audio_data_packet_transfer(self):
        received_packets = []
        event = threading.Event()

        def on_data_received(pkt: RoipPacket):
            received_packets.append(pkt)
            if len(received_packets) >= 10:
                event.set()

        self.roip_b.on_data_received = on_data_received

        # Generate audio payload
        backend = SyntheticAudioBackend(tone_freq=1000.0)
        backend.start()

        # Send 10 RoIP audio frames (each 648B = 8B header + 640B PCM)
        for i in range(10):
            pcm = backend.read_frame()
            self.roip_a.send_data(payload=pcm, ptt=True, cos=False)
            time.sleep(0.005)

        backend.stop()
        event.wait(timeout=2.0)

        self.assertEqual(len(received_packets), 10)

        # Validate packet fields
        for idx, pkt in enumerate(received_packets):
            self.assertEqual(pkt.magic, ROIP_MAGIC)
            self.assertTrue(pkt.ptt)
            self.assertFalse(pkt.cos)
            self.assertEqual(pkt.sequence, idx)
            self.assertEqual(len(pkt.payload), AUDIO_PAYLOAD_SIZE)
            self.assertTrue(pkt.is_data)
            self.assertFalse(pkt.is_heartbeat)

    def test_roip_heartbeat_packet_transfer(self):
        received_heartbeats = []
        event = threading.Event()

        def on_heartbeat_received(pkt: RoipPacket):
            received_heartbeats.append(pkt)
            if len(received_heartbeats) >= 3:
                event.set()

        self.roip_b.on_heartbeat_received = on_heartbeat_received

        # Send 3 heartbeats (each 8 bytes)
        for _ in range(3):
            self.roip_a.send_heartbeat(ptt=False, cos=True)
            time.sleep(0.005)

        event.wait(timeout=2.0)

        self.assertEqual(len(received_heartbeats), 3)
        for idx, pkt in enumerate(received_heartbeats):
            self.assertEqual(pkt.magic, ROIP_MAGIC)
            self.assertFalse(pkt.ptt)
            self.assertTrue(pkt.cos)
            self.assertEqual(len(pkt.payload), 0)
            self.assertTrue(pkt.is_heartbeat)
            self.assertFalse(pkt.is_data)


class TestSimulatorIntegration(unittest.TestCase):
    """End-to-end integration test of the full RoipAudioSimulator engine."""

    def test_simulator_burst_streaming(self):
        from tools.audio_simulator.audio_app import RoipAudioSimulator

        udp_a = UDPManager(local_port=0, stun_host="127.0.0.1", stun_port=9999)
        udp_b = UDPManager(local_port=0, stun_host="127.0.0.1", stun_port=9999)

        # Connect loopback
        udp_a.peer_addr = ("127.0.0.1", udp_b.local_port)
        udp_a.candidate_addrs = [("127.0.0.1", udp_b.local_port)]
        udp_a.connected = True
        udp_a.running = True
        udp_a._rx_thread = threading.Thread(target=udp_a._receive_loop, daemon=True)
        udp_a._rx_thread.start()

        udp_b.peer_addr = ("127.0.0.1", udp_a.local_port)
        udp_b.candidate_addrs = [("127.0.0.1", udp_a.local_port)]
        udp_b.connected = True
        udp_b.running = True
        udp_b._rx_thread = threading.Thread(target=udp_b._receive_loop, daemon=True)
        udp_b._rx_thread.start()

        sim_a = RoipAudioSimulator(udp_manager=udp_a, audio_mode="synth", tone_freq=1000.0)
        sim_b = RoipAudioSimulator(udp_manager=udp_b, audio_mode="synth", tone_freq=1000.0)

        sim_a.start()
        sim_b.start()

        try:
            # Trigger 500ms tone burst (~25 audio frames @ 50 fps)
            sim_a.trigger_tone_burst(duration_s=0.5)
            time.sleep(0.2)

            # sim_b should have received audio frames and updated COS / PTT state
            self.assertGreaterEqual(sim_b.rx_audio_frames, 5)
            self.assertTrue(sim_b.last_rx_remote_ptt)
            self.assertGreater(sim_b.last_rx_db, -20.0)
        finally:
            sim_a.stop()
            sim_b.stop()

    def test_simulator_push_to_talk_key_unkey(self):
        from tools.audio_simulator.audio_app import RoipAudioSimulator

        udp_a = UDPManager(local_port=0, stun_host="127.0.0.1", stun_port=9999)
        udp_b = UDPManager(local_port=0, stun_host="127.0.0.1", stun_port=9999)

        udp_a.peer_addr = ("127.0.0.1", udp_b.local_port)
        udp_a.candidate_addrs = [("127.0.0.1", udp_b.local_port)]
        udp_a.connected = True
        udp_a.running = True
        udp_a._rx_thread = threading.Thread(target=udp_a._receive_loop, daemon=True)
        udp_a._rx_thread.start()

        udp_b.peer_addr = ("127.0.0.1", udp_a.local_port)
        udp_b.candidate_addrs = [("127.0.0.1", udp_a.local_port)]
        udp_b.connected = True
        udp_b.running = True
        udp_b._rx_thread = threading.Thread(target=udp_b._receive_loop, daemon=True)
        udp_b._rx_thread.start()

        sim_a = RoipAudioSimulator(udp_manager=udp_a, audio_mode="synth", tone_freq=1000.0)
        sim_b = RoipAudioSimulator(udp_manager=udp_b, audio_mode="synth", tone_freq=1000.0)

        sim_a.start()
        sim_b.start()

        try:
            # Press PTT
            self.assertTrue(sim_a.key_ptt())
            self.assertTrue(sim_a.is_transmitting)
            time.sleep(0.2)
            self.assertTrue(sim_b.last_rx_remote_ptt)
            self.assertGreaterEqual(sim_b.rx_audio_frames, 4)

            # Release PTT
            sim_a.unkey_ptt()
            self.assertFalse(sim_a.is_transmitting)
            time.sleep(0.15)
            self.assertFalse(sim_b.last_rx_remote_ptt)
        finally:
            sim_a.stop()
            sim_b.stop()

    def test_simplex_busy_channel_lockout(self):
        """Simplex radio: local PTT must be locked out when channel is busy."""
        from tools.audio_simulator.audio_app import RoipAudioSimulator

        udp_a = UDPManager(local_port=0, stun_host="127.0.0.1", stun_port=9999)
        udp_b = UDPManager(local_port=0, stun_host="127.0.0.1", stun_port=9999)

        udp_a.peer_addr = ("127.0.0.1", udp_b.local_port)
        udp_a.candidate_addrs = [("127.0.0.1", udp_b.local_port)]
        udp_a.connected = True
        udp_a.running = True
        udp_a._rx_thread = threading.Thread(target=udp_a._receive_loop, daemon=True)
        udp_a._rx_thread.start()

        udp_b.peer_addr = ("127.0.0.1", udp_a.local_port)
        udp_b.candidate_addrs = [("127.0.0.1", udp_a.local_port)]
        udp_b.connected = True
        udp_b.running = True
        udp_b._rx_thread = threading.Thread(target=udp_b._receive_loop, daemon=True)
        udp_b._rx_thread.start()

        sim_a = RoipAudioSimulator(udp_manager=udp_a, audio_mode="synth", busy_channel_lockout=True)
        sim_b = RoipAudioSimulator(udp_manager=udp_b, audio_mode="synth", busy_channel_lockout=True)

        sim_a.start()
        sim_b.start()

        try:
            # Station A keys PTT (channel is clear, so keys successfully)
            self.assertTrue(sim_a.key_ptt())
            time.sleep(0.15)

            # Station B detects active channel
            self.assertTrue(sim_b.is_channel_busy)

            # Station B attempts to key PTT -> Must be LOCKED OUT!
            keyed = sim_b.key_ptt()
            self.assertFalse(keyed)
            self.assertFalse(sim_b.ptt_active)

            # Station A unkeys
            sim_a.unkey_ptt()
            time.sleep(0.2)

            # Channel clears -> Station B can now key PTT
            self.assertTrue(sim_b.key_ptt())
            self.assertTrue(sim_b.ptt_active)
            sim_b.unkey_ptt()
        finally:
            sim_a.stop()
            sim_b.stop()

    def test_part97_cw_id_transmission(self):
        """Station sends 25 WPM CW ID; peer receives audio and station sidetone queues."""
        from tools.audio_simulator.audio_app import RoipAudioSimulator

        udp_a = UDPManager(local_port=0, stun_host="127.0.0.1", stun_port=9999)
        udp_b = UDPManager(local_port=0, stun_host="127.0.0.1", stun_port=9999)

        udp_a.peer_addr = ("127.0.0.1", udp_b.local_port)
        udp_a.candidate_addrs = [("127.0.0.1", udp_b.local_port)]
        udp_a.connected = True
        udp_a.running = True
        udp_a._rx_thread = threading.Thread(target=udp_a._receive_loop, daemon=True)
        udp_a._rx_thread.start()

        udp_b.peer_addr = ("127.0.0.1", udp_a.local_port)
        udp_b.candidate_addrs = [("127.0.0.1", udp_a.local_port)]
        udp_b.connected = True
        udp_b.running = True
        udp_b._rx_thread = threading.Thread(target=udp_b._receive_loop, daemon=True)
        udp_b._rx_thread.start()

        sim_a = RoipAudioSimulator(udp_manager=udp_a, audio_mode="synth", callsign="KO6LVM", cw_wpm=25)
        sim_b = RoipAudioSimulator(udp_manager=udp_b, audio_mode="synth", callsign="W6ABC", cw_wpm=25)

        sim_a.start()
        sim_b.start()

        try:
            # Trigger CW ID
            self.assertTrue(sim_a.trigger_cw_id(force=True))
            self.assertTrue(sim_a.is_cw_iding)
            self.assertTrue(sim_a.is_transmitting)
            self.assertTrue(sim_a.ptt_line)

            # Wait for transmission frames to stream
            time.sleep(0.3)
            self.assertGreater(sim_b.rx_audio_frames, 3)
            self.assertTrue(sim_b.last_rx_remote_ptt)
            self.assertTrue(sim_b.is_channel_busy)
        finally:
            sim_a.stop()
            sim_b.stop()

    def test_part97_cw_id_squelch_closure_with_mic_backend(self):
        """Verifies that CW ID finishes and squelch closes cleanly even with an active microphone capture queue."""
        import queue
        from tools.audio_simulator.audio_app import RoipAudioSimulator
        from tools.audio_simulator.audio_backend import AudioBackend, BYTES_PER_FRAME

        class MockLiveCaptureBackend(AudioBackend):
            def __init__(self):
                self._in_queue = queue.Queue(maxsize=3)
                self.running = True
                self.th = threading.Thread(target=self._capture_worker, daemon=True)
            def start(self):
                self.th.start()
            def stop(self):
                self.running = False
            def flush_input(self):
                while not self._in_queue.empty():
                    try:
                        self._in_queue.get_nowait()
                    except Exception:
                        break
            def has_buffered_input(self):
                return not self._in_queue.empty()
            def read_frame(self):
                try:
                    return self._in_queue.get(timeout=0.03)
                except Exception:
                    return b"\x00" * BYTES_PER_FRAME
            def write_frame(self, data):
                pass
            def _capture_worker(self):
                while self.running:
                    time.sleep(0.02)
                    try:
                        self._in_queue.put_nowait(b"\x01\x00" * 320)
                    except queue.Full:
                        try:
                            self._in_queue.get_nowait()
                            self._in_queue.put_nowait(b"\x01\x00" * 320)
                        except Exception:
                            pass

        udp_a = UDPManager(local_port=0, stun_host="127.0.0.1", stun_port=9999)
        udp_b = UDPManager(local_port=0, stun_host="127.0.0.1", stun_port=9999)

        udp_a.peer_addr = ("127.0.0.1", udp_b.local_port)
        udp_a.candidate_addrs = [("127.0.0.1", udp_b.local_port)]
        udp_a.connected = True
        udp_a.running = True
        udp_a._rx_thread = threading.Thread(target=udp_a._receive_loop, daemon=True)
        udp_a._rx_thread.start()

        udp_b.peer_addr = ("127.0.0.1", udp_a.local_port)
        udp_b.candidate_addrs = [("127.0.0.1", udp_a.local_port)]
        udp_b.connected = True
        udp_b.running = True
        udp_b._rx_thread = threading.Thread(target=udp_b._receive_loop, daemon=True)
        udp_b._rx_thread.start()

        sim_a = RoipAudioSimulator(udp_manager=udp_a, audio_mode="synth", callsign="E", cw_wpm=60)
        sim_a.audio_backend = MockLiveCaptureBackend()

        sim_b = RoipAudioSimulator(udp_manager=udp_b, audio_mode="synth", callsign="W", cw_wpm=60)

        sim_a.start()
        sim_b.start()

        try:
            self.assertTrue(sim_a.trigger_cw_id(force=True))
            # Wait for CW ID to complete (at 60 WPM, callsign 'E' is ~4 frames / 80ms)
            timeout = time.time() + 2.0
            while sim_a.is_cw_iding and time.time() < timeout:
                time.sleep(0.05)

            self.assertFalse(sim_a.is_cw_iding)
            # Allow time for unkey heartbeat, hang-time (100ms), and jitter buffer squelch timeout (150ms)
            time.sleep(0.35)

            # Squelch must be closed on Station B and TX must be inactive on Station A
            self.assertFalse(sim_a.is_transmitting)
            self.assertFalse(sim_b.is_channel_busy)
            self.assertFalse(sim_b.last_rx_remote_ptt)
            self.assertFalse(sim_b.jitter_buffer.is_receiving)

            # Ensure Station A is not continuously transmitting mic audio
            frames_before = sim_a.tx_audio_frames
            time.sleep(0.2)
            self.assertEqual(sim_a.tx_audio_frames, frames_before)
        finally:
            sim_a.stop()
            sim_b.stop()

    def test_part97_timeout_timer_enforcement(self):
        """TOT cut-off: continuous PTT exceeding limit forcibly cuts off TX and locks out."""
        from tools.audio_simulator.audio_app import RoipAudioSimulator

        udp_a = UDPManager(local_port=0, stun_host="127.0.0.1", stun_port=9999)
        udp_b = UDPManager(local_port=0, stun_host="127.0.0.1", stun_port=9999)

        udp_a.peer_addr = ("127.0.0.1", udp_b.local_port)
        udp_a.candidate_addrs = [("127.0.0.1", udp_b.local_port)]
        udp_a.connected = True
        udp_a.running = True
        udp_a._rx_thread = threading.Thread(target=udp_a._receive_loop, daemon=True)
        udp_a._rx_thread.start()

        udp_b.peer_addr = ("127.0.0.1", udp_a.local_port)
        udp_b.candidate_addrs = [("127.0.0.1", udp_a.local_port)]
        udp_b.connected = True
        udp_b.running = True
        udp_b._rx_thread = threading.Thread(target=udp_b._receive_loop, daemon=True)
        udp_b._rx_thread.start()

        # Set TOT to 0.15 seconds (150ms)
        sim_a = RoipAudioSimulator(udp_manager=udp_a, audio_mode="synth", tot_limit_s=0.15)
        sim_b = RoipAudioSimulator(udp_manager=udp_b, audio_mode="synth")

        sim_a.start()
        sim_b.start()

        try:
            self.assertTrue(sim_a.key_ptt())
            self.assertTrue(sim_a.is_transmitting)

            # Wait for TOT cutoff to fire (>150ms)
            time.sleep(0.25)

            # Transmitter must be forcibly cut off
            self.assertTrue(sim_a.tot_cutoff)
            self.assertTrue(sim_a.tot_lockout)
            self.assertFalse(sim_a.is_transmitting)
            self.assertFalse(sim_a.ptt_line)

            # Attempting to key PTT while in lockout must fail
            self.assertFalse(sim_a.key_ptt())

            # Releasing PTT clears lockout
            sim_a.unkey_ptt()
            self.assertFalse(sim_a.tot_lockout)
            self.assertFalse(sim_a.tot_cutoff)

            # Can key again
            self.assertTrue(sim_a.key_ptt())
            sim_a.unkey_ptt()
        finally:
            sim_a.stop()
            sim_b.stop()

    def test_part97_controller_lines(self):
        """Verify ptt_line and cos_line reflect hardware states accurately."""
        from tools.audio_simulator.audio_app import RoipAudioSimulator

        udp_a = UDPManager(local_port=0, stun_host="127.0.0.1", stun_port=9999)
        udp_b = UDPManager(local_port=0, stun_host="127.0.0.1", stun_port=9999)

        udp_a.peer_addr = ("127.0.0.1", udp_b.local_port)
        udp_a.candidate_addrs = [("127.0.0.1", udp_b.local_port)]
        udp_a.connected = True
        udp_a.running = True
        udp_a._rx_thread = threading.Thread(target=udp_a._receive_loop, daemon=True)
        udp_a._rx_thread.start()

        udp_b.peer_addr = ("127.0.0.1", udp_a.local_port)
        udp_b.candidate_addrs = [("127.0.0.1", udp_a.local_port)]
        udp_b.connected = True
        udp_b.running = True
        udp_b._rx_thread = threading.Thread(target=udp_b._receive_loop, daemon=True)
        udp_b._rx_thread.start()

        sim_a = RoipAudioSimulator(udp_manager=udp_a, audio_mode="synth")
        sim_b = RoipAudioSimulator(udp_manager=udp_b, audio_mode="synth")

        sim_a.start()
        sim_b.start()

        try:
            # Standby: both lines inactive
            self.assertFalse(sim_a.ptt_line)
            self.assertFalse(sim_a.cos_line)
            self.assertFalse(sim_b.ptt_line)
            self.assertFalse(sim_b.cos_line)

            # Station A keys PTT
            sim_a.key_ptt()
            self.assertTrue(sim_a.ptt_line)
            self.assertFalse(sim_a.cos_line)  # TX mutes RX COS

            time.sleep(0.15)
            # Station B should detect carrier (COS line active, PTT line inactive)
            self.assertTrue(sim_b.cos_line)
            self.assertFalse(sim_b.ptt_line)

            sim_a.unkey_ptt()
        finally:
            sim_a.stop()
            sim_b.stop()


class TestPart97Generators(unittest.TestCase):
    """Verifies FCC Part 97 CW Morse ID and courtesy tone synthesis."""

    def test_morse_generation_ko6lvm(self):
        """KO6LVM at 25 WPM CW Morse generation."""
        frames = generate_morse_frames("KO6LVM", wpm=25, freq=800.0)
        self.assertGreater(len(frames), 50)
        for frame in frames:
            self.assertEqual(len(frame), BYTES_PER_FRAME)
            samples = struct.unpack(f"<{SAMPLES_PER_FRAME}h", frame)
            for s in samples:
                self.assertGreaterEqual(s, -32768)
                self.assertLessEqual(s, 32767)

        # Confirm non-silent tone frames exist
        max_rms = max(calculate_rms_db(f) for f in frames)
        self.assertGreater(max_rms, -20.0)

    def test_morse_wpm_timing(self):
        """Higher WPM must produce shorter transmission frame counts."""
        frames_15wpm = generate_morse_frames("KO6LVM", wpm=15)
        frames_25wpm = generate_morse_frames("KO6LVM", wpm=25)
        self.assertGreater(len(frames_15wpm), len(frames_25wpm))

    def test_courtesy_tones(self):
        """Verifies courtesy tone and alert styles."""
        for style in ["chime", "single", "quindar", "boop"]:
            frames = generate_courtesy_tone_frames(style)
            self.assertGreater(len(frames), 0)
            for f in frames:
                self.assertEqual(len(f), BYTES_PER_FRAME)
        self.assertEqual(generate_courtesy_tone_frames("none"), [])


if __name__ == "__main__":
    unittest.main()
