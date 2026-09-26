"""
DXoIP Peer CLI Entry Point
Demonstrates using UDPManager for P2P UDP NAT traversal and ROIPUDP
for transmitting 648-byte RoIP Data structs and 8-byte Heartbeat structs.
Compatible with standard Python 3 and MicroPython (Raspberry Pi Pico 2 W).
"""

import argparse
import sys
import threading
import time

from udp_manager import UDPManager, parse_endpoint
from roip_udp import (
    ROIPUDP,
    RoipPacket,
    ROIP_MAGIC,
    ROIP_DATA_SIZE,
    ROIP_HEARTBEAT_SIZE,
    AUDIO_PAYLOAD_SIZE,
)


def main():
    parser = argparse.ArgumentParser(description="DXoIP: P2P RoIP UDP Puncher with 648B Data & 8B Heartbeat Structs")
    parser.add_argument("--port", type=int, default=0, help="Local UDP port to bind (default: random OS port)")
    parser.add_argument("--server", type=str, default="https://udp-matchmaker.lvmlabs.org", help="HTTP Matchmaker URL")
    parser.add_argument("--room", type=str, default=None, help="Room key to join (e.g. 1234)")
    parser.add_argument("--peer", type=str, default=None, help="Direct remote peer endpoint (<IP>:<Port>) for manual mode")
    parser.add_argument("--stun", type=str, default="stun.l.google.com", help="STUN server host")
    parser.add_argument("--stun-port", type=int, default=19302, help="STUN server port")
    parser.add_argument("--endian", choices=["big", "little"], default="big", help="Header endianness (default: big / network byte order)")
    args = parser.parse_args()

    endian_char = "!" if args.endian == "big" else "<"

    # 1. Initialize UDPManager (Network Transport Layer)
    udp_mgr = UDPManager(local_port=args.port, stun_host=args.stun, stun_port=args.stun_port)
    print(f"[*] Bound local UDP socket on port {udp_mgr.local_port}")
    print(f"[*] Resolving STUN public endpoint ({args.stun}:{args.stun_port})...")

    try:
        wan_ep, lan_ep = udp_mgr.get_endpoints()
        my_wan = f"{wan_ep[0]}:{wan_ep[1]}"
        my_lan = f"{lan_ep[0]}:{lan_ep[1]}"
        print(f"\n==========================================")
        print(f" PUBLIC (WAN) ENDPOINT : {my_wan}")
        print(f" LOCAL (LAN) ENDPOINT  : {my_lan}")
        print(f"==========================================\n")
    except Exception as e:
        print(f"[!] STUN resolution failed: {e}")
        udp_mgr.stop()
        sys.exit(1)

    # 2. Initialize ROIPUDP (Framing / Struct Layer)
    roip = ROIPUDP(udp_manager=udp_mgr, endianness=endian_char)

    # State tracking
    ptt_state = False
    cos_state = False

    # Hook RoIP packet callbacks
    def on_data_received(packet: RoipPacket):
        # Extract audio preview (or ASCII preview if text was encoded in payload)
        payload_preview = packet.payload[:32].rstrip(b"\x00")
        try:
            preview_str = payload_preview.decode("utf-8")
        except UnicodeDecodeError:
            preview_str = f"{len(packet.payload)} bytes PCM audio"

        print(
            f"\n[<-- ROIP DATA] Seq: {packet.sequence} | TS: {packet.timestamp} | "
            f"PTT: {packet.ptt} | COS: {packet.cos} | Content: {preview_str}\n> ",
            end="",
            flush=True,
        )

    def on_heartbeat_received(packet: RoipPacket):
        print(
            f"\n[<-- HEARTBEAT] Seq: {packet.sequence} | TS: {packet.timestamp} | "
            f"PTT: {packet.ptt} | COS: {packet.cos} (8 bytes)\n> ",
            end="",
            flush=True,
        )

    def on_disconnected(reason):
        print(f"\n[!] Link disconnected: {reason}\n> ", end="", flush=True)

    roip.on_data_received = on_data_received
    roip.on_heartbeat_received = on_heartbeat_received
    udp_mgr.on_disconnected = on_disconnected

    # 3. Connection Setup (Matchmaker Room vs Manual Peer)
    server_url = args.server
    room_key = args.room

    if not server_url and not args.peer:
        print("Choose connection mode:")
        print(" [1] Automated HTTP Room Key (e.g. room 1234)")
        print(" [2] Manual endpoint entry")
        choice = input("Select [1/2] (default 1): ").strip()
        if choice == "2":
            pass
        else:
            server_url = input("Enter HTTP Server URL [https://udp-matchmaker.lvmlabs.org]: ").strip() or "https://udp-matchmaker.lvmlabs.org"
            room_key = input("Enter Room Key (e.g. 1234): ").strip() or "1234"

    connect_thread = None
    if server_url and room_key:
        print(f"[*] Connecting via Room '{room_key}' on {server_url}...")
        connect_thread = threading.Thread(
            target=udp_mgr.connect_room,
            args=(room_key, server_url),
            daemon=True
        )
    else:
        peer_endpoint = args.peer
        if not peer_endpoint:
            print("Enter the PEER's endpoint (format: <IP>:<Port>):")
            peer_endpoint = input("> ").strip()
        peer_ip, peer_port = parse_endpoint(peer_endpoint)
        print(f"[*] Punching directly towards {peer_ip}:{peer_port}...")
        connect_thread = threading.Thread(
            target=udp_mgr.connect_peer,
            args=(peer_ip, peer_port),
            daemon=True
        )

    connect_thread.start()

    # Wait for connection
    while not udp_mgr.connected and connect_thread.is_alive():
        time.sleep(0.1)

    if not udp_mgr.connected:
        print("[!] Connection failed or timed out.")
        udp_mgr.stop()
        sys.exit(1)

    print("\n[+] SUCCESS! Direct UDP Hole Punched and P2P Session Established!")
    print(f"[+] Active Peer Endpoint: {udp_mgr.peer_addr[0]}:{udp_mgr.peer_addr[1]}")
    print("[+] Commands:")
    print("      Type any message + Enter to send a 648-byte RoIP Data packet")
    print("      Type '/h' or '/heartbeat' to send an 8-byte Heartbeat packet")
    print("      Type '/ptt' to toggle PTT state (current: OFF)")
    print("      Type 'exit' to quit\n")

    try:
        while udp_mgr.connected:
            line = input("> ").strip()
            if not line:
                continue

            if line.lower() in ("exit", "quit"):
                break

            elif line.lower() in ("/h", "/heartbeat"):
                seq = roip.send_heartbeat(ptt=ptt_state, cos=cos_state)
                print(f"[--> Sent Heartbeat] Seq: {seq} (8 bytes)")

            elif line.lower() == "/ptt":
                ptt_state = not ptt_state
                state_str = "ON (Transmitting)" if ptt_state else "OFF (Receiving)"
                print(f"[*] PTT toggled to: {state_str}")
                # Transmit a heartbeat with the new PTT flag
                seq = roip.send_heartbeat(ptt=ptt_state, cos=cos_state)
                print(f"[--> Sent Heartbeat with PTT={ptt_state}] Seq: {seq}")

            else:
                # Encode text into 640-byte audio payload
                text_bytes = line.encode("utf-8")
                # Pad to 640 bytes
                payload = text_bytes.ljust(AUDIO_PAYLOAD_SIZE, b"\x00")
                seq = roip.send_data(payload=payload, ptt=ptt_state, cos=cos_state)
                print(f"[--> Sent RoIP Data] Seq: {seq} (648 bytes, PTT={ptt_state})")

    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        print("\n[*] Shutting down...")
        udp_mgr.stop()


if __name__ == "__main__":
    main()
