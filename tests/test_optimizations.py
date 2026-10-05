"""
Unit Tests for DXoIP Optimizations (tests/test_optimizations.py)
Tests:
1. RoipPacket & ROIPUDP precompiled structs, slots, pack_into, and timer caching.
2. JitterBuffer out-of-order reordering, uint16 wrap-around, and late-drop handling.
3. UDPManager fast-path packet routing, RTT echo/calculation, and prompt thread shutdown.
"""

import struct
import threading
import time
import unittest
from unittest.mock import MagicMock

from roip_udp import (
    ROIPUDP,
    RoipPacket,
    ROIP_MAGIC,
    ROIP_DATA_SIZE,
    ROIP_HEARTBEAT_SIZE,
    AUDIO_PAYLOAD_SIZE,
    FLAG_PTT,
    FLAG_COS,
    get_current_timestamp_us,
)
from udp_manager import (
    UDPManager,
    PREFIX_PING,
    PREFIX_PONG,
    get_local_ips,
)
from tools.audio_simulator.jitter_buffer import JitterBuffer, seq_diff, SILENCE_FRAME
from tools.audio_simulator.audio_backend import calculate_rms_db


class TestRoipOptimizations(unittest.TestCase):
    """Verifies RoIP framing layer optimizations."""

    def test_timestamp_resolution(self):
        """Microsecond timestamp must return a 32-bit unsigned integer."""
        t1 = get_current_timestamp_us()
        self.assertGreaterEqual(t1, 0)
        self.assertLessEqual(t1, 0xFFFFFFFF)

        time.sleep(0.005)
        t2 = get_current_timestamp_us()
        self.assertGreaterEqual(t2, 0)
        self.assertLessEqual(t2, 0xFFFFFFFF)
        # Verify elapsed time is roughly within expected bounds (~5ms = 5000us)
        diff = (t2 - t1) & 0xFFFFFFFF
        self.assertGreater(diff, 1000)
        self.assertLess(diff, 100000)

    def test_slots_defined(self):
        """RoipPacket must use __slots__ to eliminate __dict__ allocation."""
        pkt = RoipPacket(flags=FLAG_PTT, sequence=42, timestamp=12345, payload=b"\x00" * 640)
        self.assertFalse(hasattr(pkt, "__dict__"))
        self.assertTrue(hasattr(pkt, "__slots__"))

    def test_precompiled_struct_endianness(self):
        """Endianness serialization must work for both Network ('!') and Little-Endian ('<')."""
        payload = b"\x12\x34" * 320
        pkt = RoipPacket(flags=FLAG_PTT | FLAG_COS, sequence=0x1234, timestamp=0x56789ABC, payload=payload)

        # Big-Endian
        be_bytes = pkt.to_bytes(endianness="!")
        self.assertEqual(len(be_bytes), ROIP_DATA_SIZE)
        be_pkt = RoipPacket.from_bytes(be_bytes, endianness="!")
        self.assertEqual(be_pkt.flags, FLAG_PTT | FLAG_COS)
        self.assertEqual(be_pkt.sequence, 0x1234)
        self.assertEqual(be_pkt.timestamp, 0x56789ABC)
        self.assertEqual(be_pkt.payload, payload)

        # Little-Endian
        le_bytes = pkt.to_bytes(endianness="<")
        self.assertEqual(len(le_bytes), ROIP_DATA_SIZE)
        le_pkt = RoipPacket.from_bytes(le_bytes, endianness="<")
        self.assertEqual(le_pkt.flags, FLAG_PTT | FLAG_COS)
        self.assertEqual(le_pkt.sequence, 0x1234)
        self.assertEqual(le_pkt.timestamp, 0x56789ABC)
        self.assertEqual(le_pkt.payload, payload)

        # Headers should differ in byte layout
        self.assertNotEqual(be_bytes[:8], le_bytes[:8])

    def test_pack_into_zero_allocation(self):
        """pack_into must serialize identical binary into preallocated buffers."""
        payload = b"\xAB" * 640
        pkt = RoipPacket(flags=FLAG_PTT, sequence=100, timestamp=999999, payload=payload)

        # Data packet
        buf = bytearray(ROIP_DATA_SIZE)
        n = pkt.pack_into(buf)
        self.assertEqual(n, ROIP_DATA_SIZE)
        self.assertEqual(bytes(buf), pkt.to_bytes())

        # Heartbeat packet
        hb = RoipPacket(flags=FLAG_COS, sequence=101, timestamp=1000000, payload=b"")
        hb_buf = bytearray(ROIP_HEARTBEAT_SIZE)
        n_hb = hb.pack_into(hb_buf)
        self.assertEqual(n_hb, ROIP_HEARTBEAT_SIZE)
        self.assertEqual(bytes(hb_buf), hb.to_bytes())

    def test_roipudp_send_data_buffer_reuse(self):
        """ROIPUDP.send_data transmits valid packets using its internal preallocated buffer."""
        sent_packets = []

        class MockUDPManager:
            def send_packet(self, data):
                sent_packets.append(bytes(data))

        mock_mgr = MockUDPManager()
        roip = ROIPUDP(udp_manager=mock_mgr)

        payload = b"\x55" * 640
        seq = roip.send_data(payload=payload, ptt=True, cos=False)

        self.assertEqual(len(sent_packets), 1)
        self.assertEqual(len(sent_packets[0]), ROIP_DATA_SIZE)
        pkt = RoipPacket.from_bytes(sent_packets[0])
        self.assertEqual(pkt.sequence, seq)
        self.assertTrue(pkt.ptt)
        self.assertFalse(pkt.cos)
        self.assertEqual(pkt.payload, payload)


class TestJitterBufferOptimizations(unittest.TestCase):
    """Verifies JitterBuffer out-of-order reordering and wrap-around handling."""

    def test_seq_diff_math(self):
        """seq_diff must compute signed distance across uint16 boundaries."""
        self.assertEqual(seq_diff(10, 5), 5)
        self.assertEqual(seq_diff(5, 10), -5)
        self.assertEqual(seq_diff(0, 65535), 1)
        self.assertEqual(seq_diff(65535, 0), -1)
        self.assertEqual(seq_diff(2, 65534), 4)

    def test_out_of_order_packet_reordering(self):
        """Arriving out-of-order packets must be popped in sequential order."""
        jb = JitterBuffer(target_delay_frames=4, max_frames=8)

        f0 = b"\x00" * 640
        f1 = b"\x01" * 640
        f2 = b"\x02" * 640
        f3 = b"\x03" * 640

        # Push in scramble order: 0, 2, 1, 3
        jb.push(seq=0, pcm_data=f0)
        jb.push(seq=2, pcm_data=f2)
        jb.push(seq=1, pcm_data=f1)
        jb.push(seq=3, pcm_data=f3)

        # Buffer depth reached 4: pop in sequence
        self.assertEqual(jb.pop(), f0)
        self.assertEqual(jb.pop(), f1)
        self.assertEqual(jb.pop(), f2)
        self.assertEqual(jb.pop(), f3)

    def test_duplicate_packet_ignored(self):
        """Duplicate sequence numbers should be suppressed."""
        jb = JitterBuffer(target_delay_frames=1, max_frames=8)
        jb.push(seq=10, pcm_data=b"\x10" * 640)
        jb.push(seq=10, pcm_data=b"\x10" * 640)
        self.assertEqual(jb.queued_frames, 1)

    def test_late_packet_dropped(self):
        """Packets arriving after their playback point must be dropped."""
        jb = JitterBuffer(target_delay_frames=1, max_frames=4)
        jb.push(seq=10, pcm_data=b"\x10" * 640)
        p10 = jb.pop()
        self.assertEqual(p10, b"\x10" * 640)

        # Now sequence 9 arrives late
        jb.push(seq=9, pcm_data=b"\x09" * 640)
        self.assertEqual(jb.late_drop_count, 1)
        self.assertEqual(jb.queued_frames, 0)

    def test_uint16_sequence_wrap_around(self):
        """Packets crossing the 65535 -> 0 boundary must order correctly."""
        jb = JitterBuffer(target_delay_frames=3, max_frames=6)

        f_wrap_prev = b"\xFE" * 640  # seq 65534
        f_wrap_max  = b"\xFF" * 640  # seq 65535
        f_wrap_zero = b"\x00" * 640  # seq 0

        # Arrive scrambled: 65534, 0, 65535
        jb.push(seq=65534, pcm_data=f_wrap_prev)
        jb.push(seq=0, pcm_data=f_wrap_zero)
        jb.push(seq=65535, pcm_data=f_wrap_max)

        self.assertEqual(jb.pop(), f_wrap_prev)
        self.assertEqual(jb.pop(), f_wrap_max)
        self.assertEqual(jb.pop(), f_wrap_zero)

    def test_silence_frame_identity(self):
        """Underrun must return preallocated SILENCE_FRAME directly."""
        jb = JitterBuffer(target_delay_frames=1)
        out = jb.pop()
        self.assertIs(out, SILENCE_FRAME)


class TestUdpManagerOptimizations(unittest.TestCase):
    """Verifies UDPManager fast-path and keepalive RTT tracking."""

    def test_get_local_ips(self):
        """get_local_ips must return valid non-empty list of non-loopback addresses."""
        ips = get_local_ips()
        self.assertIsInstance(ips, list)
        self.assertGreater(len(ips), 0)
        for ip in ips:
            self.assertFalse(ip.startswith("127."))

    def test_rtt_measurement_ping_pong(self):
        """Keepalive PING with timestamp must return PONG and record RTT."""
        udp_a = UDPManager(local_port=0, stun_host="127.0.0.1", stun_port=9999)
        udp_b = UDPManager(local_port=0, stun_host="127.0.0.1", stun_port=9999)

        # Wire loopback
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

        try:
            # Send PING from A to B with current timestamp
            now_us = int(time.time() * 1_000_000) & 0xFFFFFFFF
            ping_pkt = PREFIX_PING + struct.pack("!I", now_us)
            udp_a.send_packet(ping_pkt)

            # Wait for B to echo PONG back to A
            time.sleep(0.05)
            self.assertGreater(udp_a.last_rtt_us, 0)
            self.assertGreater(udp_a.last_rtt_ms, 0.0)
            self.assertLess(udp_a.last_rtt_ms, 50.0)  # Local loopback RTT should be sub-millisecond to few ms
        finally:
            udp_a.stop()
            udp_b.stop()

    def test_fast_path_application_packet_dispatch(self):
        """Application data (e.g. RoIP 0x52) from locked peer must take fast path."""
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

        received = []
        udp_b.on_packet_received = lambda d: received.append(d)

        try:
            # Send packet starting with 'R' (0x52)
            app_data = b"R\x01\x00\x01\x00\x00\x00\x00" + (b"\x7F" * 640)
            udp_a.send_packet(app_data)
            time.sleep(0.05)

            self.assertEqual(len(received), 1)
            self.assertEqual(received[0], app_data)
        finally:
            udp_a.stop()
            udp_b.stop()

    def test_prompt_stop_shutdown(self):
        """Calling stop() should terminate threads without hanging."""
        udp = UDPManager(local_port=0, stun_host="127.0.0.1", stun_port=9999)
        udp.connected = True
        udp.running = True
        udp.peer_addr = ("127.0.0.1", 9999)
        udp._rx_thread = threading.Thread(target=udp._receive_loop, daemon=True)
        udp._watchdog_thread = threading.Thread(target=udp._watchdog_loop, daemon=True)
        udp._rx_thread.start()
        udp._watchdog_thread.start()

        start = time.time()
        udp.stop()
        udp._watchdog_thread.join(timeout=1.0)
        elapsed = time.time() - start

        self.assertFalse(udp._watchdog_thread.is_alive())
        self.assertLess(elapsed, 1.0)

    def test_join_matchmaker_room_409_recovery(self):
        """Verify automatic recovery when room returns HTTP 409 Conflict."""
        import urllib.error
        from unittest.mock import MagicMock

        udp = UDPManager(local_port=0)
        udp.public_ip = "1.2.3.4"
        udp.public_port = 5000

        calls = []
        def mock_http_request(url, timeout=10.0):
            calls.append(url)
            if '/join' in url and len(calls) == 1:
                fp = MagicMock()
                fp.read.return_value = b'{"error": "Room already matched or full"}'
                raise urllib.error.HTTPError(url, 409, "Conflict", {}, fp)
            elif '/poll' in url:
                return {"status": "matched", "peer_wan": "9.9.9.9:9999", "peer_lan": "10.0.0.9:9999"}
            elif '/join' in url:
                return {"status": "waiting", "room": "test409", "role": "p1"}
            return {}

        udp._http_request = mock_http_request

        # Should recover by polling and waiting, then timing out or polling peer
        # Mock poll loop returning peer
        def mock_poll_after_join(url, timeout=10.0):
            if '/join' in url and len(calls) == 0:
                fp = MagicMock()
                fp.read.return_value = b'{"error": "Room already matched"}'
                calls.append('join_409')
                raise urllib.error.HTTPError(url, 409, "Conflict", {}, fp)
            elif '/poll' in url:
                calls.append('poll_clean')
                return {"status": "matched", "peer_wan": "8.8.8.8:8888", "peer_lan": "10.0.0.8:8888"}
            elif '/join' in url:
                calls.append('join_ok')
                return {"status": "matched", "peer_wan": "8.8.8.8:8888", "peer_lan": "10.0.0.8:8888"}
            return {}

        calls.clear()
        udp._http_request = mock_poll_after_join
        wan, lan = udp.join_matchmaker_room("http://fake-server", "test409", poll_timeout=5.0)
        self.assertEqual(wan, "8.8.8.8:8888")
        self.assertEqual(lan, "10.0.0.8:8888")
        self.assertIn('join_409', calls)
        self.assertIn('poll_clean', calls)
        self.assertIn('join_ok', calls)
        udp.stop()

    def test_join_matchmaker_self_match_recovery(self):
        """Verify self-match is rejected and room is refreshed."""
        from unittest.mock import MagicMock

        udp = UDPManager(local_port=0)
        udp.public_ip = "1.2.3.4"
        udp.public_port = 5000
        my_wan = f"{udp.public_ip}:{udp.public_port}"
        wan_ep, lan_ep = udp.get_endpoints()
        my_lan = f"{lan_ep[0]}:{lan_ep[1]}"

        calls = []
        def mock_http_request(url, timeout=10.0):
            if '/join' in url and len(calls) == 0:
                calls.append('self_match')
                return {"status": "matched", "peer_wan": my_wan, "peer_lan": my_lan}
            elif '/poll' in url and len(calls) == 1:
                calls.append('poll_clear')
                return {"status": "matched"}
            elif '/join' in url and len(calls) == 2:
                calls.append('join_p1')
                return {"status": "matched", "peer_wan": "9.9.9.9:9999", "peer_lan": "10.0.0.9:9999"}
            return {}

        udp._http_request = mock_http_request
        wan, lan = udp.join_matchmaker_room("http://fake-server", "testself", poll_timeout=5.0)
        self.assertEqual(wan, "9.9.9.9:9999")
        self.assertEqual(lan, "10.0.0.9:9999")
        self.assertEqual(calls, ['self_match', 'poll_clear', 'join_p1'])
        udp.stop()

    def test_last_error_captured(self):
        """Verify last_error is set on connection failure."""
        udp = UDPManager(local_port=0)
        udp.public_ip = "1.2.3.4"
        udp.public_port = 5000
        udp.join_matchmaker_room = MagicMock(side_effect=RuntimeError("Custom matchmaker failure"))

        with self.assertRaises(RuntimeError):
            udp.connect_room("error_room")

        self.assertIsNotNone(udp.last_error)
        self.assertIn("Custom matchmaker failure", udp.last_error)
        udp.stop()


if __name__ == "__main__":
    unittest.main()
