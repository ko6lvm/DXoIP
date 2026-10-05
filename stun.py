"""
Lightweight RFC 5389 STUN Client
Compatible with standard Python 3 and MicroPython (Raspberry Pi Pico 2 W).
Zero external dependencies (uses only standard socket and struct).
"""

import socket
import struct
import os
import random

# RFC 5389 Constants
STUN_BINDING_REQUEST = 0x0001
STUN_BINDING_RESPONSE = 0x0101
STUN_MAGIC_COOKIE = 0x2112A442

ATTR_MAPPED_ADDRESS = 0x0001
ATTR_XOR_MAPPED_ADDRESS = 0x0020


_STUN_HDR_STRUCT = struct.Struct("!HHI12s")
_STUN_ATTR_HDR = struct.Struct("!HH")
_STUN_MAPPED_V4 = struct.Struct("!BBH4s")


def generate_transaction_id():
    """Generates 12 random bytes for the STUN transaction ID."""
    try:
        return os.urandom(12)
    except (AttributeError, NotImplementedError):
        pass
    try:
        return random.getrandbits(96).to_bytes(12, "big")
    except (AttributeError, OverflowError):
        return bytes([random.randint(0, 255) for _ in range(12)])


def build_binding_request(transaction_id):
    """
    Builds a 20-byte STUN Binding Request header.
    Message Type: 0x0001 (Binding Request)
    Message Length: 0x0000 (0 bytes of attributes)
    Magic Cookie: 0x2112A442
    Transaction ID: 12 bytes
    """
    return _STUN_HDR_STRUCT.pack(STUN_BINDING_REQUEST, 0, STUN_MAGIC_COOKIE, transaction_id)


def parse_binding_response(data, transaction_id):
    """
    Parses a STUN Binding Response and extracts (ip, port).
    Supports both XOR-MAPPED-ADDRESS (RFC 5389) and MAPPED-ADDRESS (RFC 3489).
    Uses unpack_from to eliminate buffer slicing.
    """
    if len(data) < 20:
        raise ValueError("Response too short to be a valid STUN message")

    msg_type, msg_len, magic_cookie, resp_tx_id = _STUN_HDR_STRUCT.unpack_from(data, 0)

    if msg_type != STUN_BINDING_RESPONSE:
        raise ValueError(f"Expected Binding Response (0x0101), got 0x{msg_type:04x}")

    if magic_cookie != STUN_MAGIC_COOKIE:
        raise ValueError("Invalid STUN magic cookie in response")

    if resp_tx_id != transaction_id:
        raise ValueError("Transaction ID mismatch in STUN response")

    offset = 20
    end = 20 + msg_len
    data_len = len(data)

    while offset + 4 <= end and offset + 4 <= data_len:
        attr_type, attr_len = _STUN_ATTR_HDR.unpack_from(data, offset)
        attr_offset = offset + 4
        offset = attr_offset + ((attr_len + 3) & ~3)

        if attr_type == ATTR_XOR_MAPPED_ADDRESS:
            if attr_len >= 8 and attr_offset + 8 <= data_len:
                _, family, xor_port, xor_ip_bytes = _STUN_MAPPED_V4.unpack_from(data, attr_offset)
                port = xor_port ^ (STUN_MAGIC_COOKIE >> 16)
                if family == 0x01:  # IPv4
                    xor_ip = struct.unpack("!I", xor_ip_bytes)[0]
                    ip_int = xor_ip ^ STUN_MAGIC_COOKIE
                    ip = socket.inet_ntoa(struct.pack("!I", ip_int))
                    return (ip, port)

        elif attr_type == ATTR_MAPPED_ADDRESS:
            if attr_len >= 8 and attr_offset + 8 <= data_len:
                _, family, port, ip_bytes = _STUN_MAPPED_V4.unpack_from(data, attr_offset)
                if family == 0x01:  # IPv4
                    ip = socket.inet_ntoa(ip_bytes)
                    return (ip, port)

    raise ValueError("No mapped address attribute found in STUN response")


def get_mapped_address(sock, stun_host="stun.l.google.com", stun_port=19302, timeout=3.0, max_retries=3):
    """
    Queries a public STUN server using an existing bound UDP socket.
    Returns: (public_ip, public_port)
    """
    try:
        stun_ip = socket.gethostbyname(stun_host)
    except socket.gaierror as e:
        raise RuntimeError(f"Failed to resolve STUN server {stun_host}: {e}")

    prev_timeout = sock.gettimeout()
    sock.settimeout(timeout)

    try:
        for attempt in range(max_retries):
            tx_id = generate_transaction_id()
            req = build_binding_request(tx_id)
            try:
                sock.sendto(req, (stun_ip, stun_port))
                while True:
                    data, addr = sock.recvfrom(1024)
                    # Check if response came from the STUN server
                    if addr[0] == stun_ip:
                        try:
                            return parse_binding_response(data, tx_id)
                        except ValueError:
                            continue
            except socket.timeout:
                continue

        raise TimeoutError(f"STUN request timed out after {max_retries} attempts to {stun_host}:{stun_port}")
    finally:
        sock.settimeout(prev_timeout)


if __name__ == "__main__":
    # Quick standalone test
    print("Testing STUN resolution...")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", 0))
    local_port = s.getsockname()[1]
    print(f"Local UDP socket bound to port {local_port}")

    for host in ["stun.l.google.com", "stun.cloudflare.com"]:
        try:
            print(f"Querying {host}...")
            pub_ip, pub_port = get_mapped_address(s, stun_host=host, timeout=2.5)
            print(f"  -> SUCCESS: Public endpoint is {pub_ip}:{pub_port}")
            break
        except Exception as err:
            print(f"  -> Failed ({host}): {err}")
    s.close()
