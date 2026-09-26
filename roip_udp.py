"""
RoIP UDP Protocol & Framing Layer (roip_udp.py)
Implements the 648-byte RoIP Data Struct and 8-byte Heartbeat Struct.
Compatible with standard Python 3 and MicroPython (Raspberry Pi Pico 2 W).
"""

import struct
import time

# Protocol Constants
ROIP_MAGIC = 0x52          # 'R' header validation
FLAG_PTT   = 0x01          # Bit 0: Push-to-Talk active
FLAG_COS   = 0x02          # Bit 1: Carrier-Operated Squelch active

HEADER_SIZE        = 8     # 8-byte standard header
AUDIO_PAYLOAD_SIZE = 640   # 320 samples 16-bit PCM (20ms audio)
ROIP_DATA_SIZE     = 648   # 8-byte header + 640-byte audio payload
ROIP_HEARTBEAT_SIZE = 8    # 8-byte header only


def get_current_timestamp_us():
    """
    Returns 32-bit microsecond clock timestamp (0 to 2^32 - 1).
    Matches Raspberry Pi Pico time_us_32() behavior.
    """
    try:
        import time as utime
        if hasattr(utime, "ticks_us"):
            return utime.ticks_us() & 0xFFFFFFFF
    except Exception:
        pass
    return int(time.time() * 1_000_000) & 0xFFFFFFFF


class RoipPacket:
    """
    Represents a decoded RoIP Data or Heartbeat packet.
    """

    def __init__(self, flags=0, sequence=0, timestamp=0, payload=b"", magic=ROIP_MAGIC):
        self.magic = magic
        self.flags = flags
        self.sequence = sequence & 0xFFFF
        self.timestamp = timestamp & 0xFFFFFFFF
        self.payload = payload  # 640 bytes for RoIP Data, b"" for Heartbeat

    @property
    def is_heartbeat(self):
        """Returns True if this is an 8-byte heartbeat packet without audio."""
        return len(self.payload) == 0

    @property
    def is_data(self):
        """Returns True if this is a 648-byte RoIP data packet."""
        return len(self.payload) == AUDIO_PAYLOAD_SIZE

    @property
    def ptt(self):
        """Push-to-Talk status (Bit 0)."""
        return bool(self.flags & FLAG_PTT)

    @property
    def cos(self):
        """Carrier-Operated Squelch status (Bit 1)."""
        return bool(self.flags & FLAG_COS)

    def to_bytes(self, endianness="!"):
        """
        Serializes packet to bytes.
        endianness: '!' for Network Byte Order (Big-Endian), '<' for Little-Endian.
        """
        fmt_hdr = f"{endianness}BBHI"
        hdr = struct.pack(fmt_hdr, self.magic, self.flags, self.sequence, self.timestamp)
        if self.payload:
            # Pad or truncate payload to exactly AUDIO_PAYLOAD_SIZE (640 bytes)
            if len(self.payload) < AUDIO_PAYLOAD_SIZE:
                padded = self.payload + (b"\x00" * (AUDIO_PAYLOAD_SIZE - len(self.payload)))
            else:
                padded = self.payload[:AUDIO_PAYLOAD_SIZE]
            return hdr + padded
        return hdr

    @classmethod
    def from_bytes(cls, data: bytes, endianness="!"):
        """
        Deserializes a raw byte buffer into a RoipPacket.
        Validates Magic byte (0x52) and packet size (8 or 648 bytes).
        """
        if len(data) < HEADER_SIZE:
            raise ValueError(f"Packet too short ({len(data)} bytes, minimum is {HEADER_SIZE})")

        fmt_hdr = f"{endianness}BBHI"
        magic, flags, sequence, timestamp = struct.unpack(fmt_hdr, data[:HEADER_SIZE])

        if magic != ROIP_MAGIC:
            raise ValueError(f"Invalid Magic byte 0x{magic:02X} (expected 0x{ROIP_MAGIC:02X})")

        payload = data[HEADER_SIZE:]
        if len(payload) > 0 and len(payload) != AUDIO_PAYLOAD_SIZE:
            raise ValueError(
                f"Invalid packet size: {len(data)} bytes. "
                f"Expected {ROIP_HEARTBEAT_SIZE}B (Heartbeat) or {ROIP_DATA_SIZE}B (Data)"
            )

        return cls(
            flags=flags,
            sequence=sequence,
            timestamp=timestamp,
            payload=payload,
            magic=magic
        )

    def __repr__(self):
        pkt_type = "Heartbeat" if self.is_heartbeat else f"Data({len(self.payload)}B)"
        return (
            f"<RoipPacket type={pkt_type} seq={self.sequence} "
            f"ts={self.timestamp} ptt={self.ptt} cos={self.cos}>"
        )


class ROIPUDP:
    """
    RoIP UDP Framing and Protocol Handler.
    Encodes/decodes 648-byte RoIP Data structs and 8-byte Heartbeat structs,
    and bridges them with an underlying UDPManager.
    """

    def __init__(self, udp_manager=None, endianness="!"):
        self.udp_manager = udp_manager
        self.endianness = endianness
        self._seq = 0

        # Event Callbacks
        self.on_data_received = None       # callback(packet: RoipPacket)
        self.on_heartbeat_received = None  # callback(packet: RoipPacket)
        self.on_packet_error = None        # callback(err: Exception, raw_bytes: bytes)

        # Hook into UDPManager if provided
        if self.udp_manager:
            self.udp_manager.on_packet_received = self._on_udp_packet

    def next_sequence(self):
        """Increments and returns sequence counter (uint16, 0-65535)."""
        seq = self._seq
        self._seq = (self._seq + 1) & 0xFFFF
        return seq

    def send_data(self, payload: bytes, ptt: bool = False, cos: bool = False, timestamp: int = None):
        """
        Encodes and transmits a 648-byte RoIP Data packet.
        Returns the assigned sequence number.
        """
        if self.udp_manager is None:
            raise RuntimeError("UDPManager not configured on ROIPUDP")

        flags = (FLAG_PTT if ptt else 0) | (FLAG_COS if cos else 0)
        seq = self.next_sequence()
        ts = get_current_timestamp_us() if timestamp is None else timestamp

        packet = RoipPacket(flags=flags, sequence=seq, timestamp=ts, payload=payload)
        raw_bytes = packet.to_bytes(endianness=self.endianness)
        self.udp_manager.send_packet(raw_bytes)
        return seq

    def send_heartbeat(self, ptt: bool = False, cos: bool = False, timestamp: int = None):
        """
        Encodes and transmits an 8-byte Heartbeat packet.
        Returns the assigned sequence number.
        """
        if self.udp_manager is None:
            raise RuntimeError("UDPManager not configured on ROIPUDP")

        flags = (FLAG_PTT if ptt else 0) | (FLAG_COS if cos else 0)
        seq = self.next_sequence()
        ts = get_current_timestamp_us() if timestamp is None else timestamp

        packet = RoipPacket(flags=flags, sequence=seq, timestamp=ts, payload=b"")
        raw_bytes = packet.to_bytes(endianness=self.endianness)
        self.udp_manager.send_packet(raw_bytes)
        return seq

    def send_raw_struct(self, data: bytes):
        """
        Directly sends a pre-packed raw struct buffer after validating length and magic.
        """
        if len(data) not in (ROIP_HEARTBEAT_SIZE, ROIP_DATA_SIZE):
            raise ValueError(f"Invalid struct size: {len(data)} bytes. Must be 8 or 648 bytes.")
        if data[0] != ROIP_MAGIC:
            raise ValueError(f"Invalid Magic byte 0x{data[0]:02X} (expected 0x{ROIP_MAGIC:02X})")

        if self.udp_manager is None:
            raise RuntimeError("UDPManager not configured on ROIPUDP")
        self.udp_manager.send_packet(data)

    def _on_udp_packet(self, data: bytes):
        """
        Inbound packet handler invoked by UDPManager.
        Validates Magic byte and unpacks into RoipPacket.
        """
        if not data or data[0] != ROIP_MAGIC:
            # Not a RoIP packet (could be unknown packet or noise)
            return

        try:
            packet = RoipPacket.from_bytes(data, endianness=self.endianness)
        except Exception as err:
            if self.on_packet_error:
                self.on_packet_error(err, data)
            return

        if packet.is_heartbeat:
            if self.on_heartbeat_received:
                self.on_heartbeat_received(packet)
        else:
            if self.on_data_received:
                self.on_data_received(packet)
