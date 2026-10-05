"""
DXoIP P2P NAT Traversal & Matchmaking Entry Point (peer.py)
Handles STUN endpoint resolution, HTTP room matchmaking, and direct UDP hole punching.
Compatible with standard Python 3 and MicroPython (Raspberry Pi Pico 2 W).
"""

import argparse
import sys
import threading
import time

from udp_manager import UDPManager, parse_endpoint


def main():
    parser = argparse.ArgumentParser(description="DXoIP: P2P UDP NAT Traversal & Hole Puncher")
    parser.add_argument("--port", type=int, default=0, help="Local UDP port to bind (default: random OS port)")
    parser.add_argument("--server", type=str, default="https://udp-matchmaker.lvmlabs.org", help="HTTP Matchmaker URL")
    parser.add_argument("--room", type=str, default=None, help="Room key to join (e.g. 1234)")
    parser.add_argument("--peer", type=str, default=None, help="Direct remote peer endpoint (<IP>:<Port>) for manual mode")
    parser.add_argument("--stun", type=str, default="stun.l.google.com", help="STUN server host")
    parser.add_argument("--stun-port", type=int, default=19302, help="STUN server port")
    args = parser.parse_args()

    # 1. Endpoint Resolution (STUN WAN & LAN)
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

    def on_disconnected(reason):
        print(f"\n[!] Link disconnected: {reason}")

    udp_mgr.on_disconnected = on_disconnected

    # 2. Matchmaking & P2P Hole Punching
    server_url = args.server
    room_key = args.room

    if not room_key and not args.peer:
        print("Choose connection mode:")
        print(" [1] Automated HTTP Room Key (e.g. room 1234)")
        print(" [2] Manual endpoint entry")
        choice = input("Select [1/2] (default 1): ").strip()
        if choice != "2":
            server_url = input(f"Enter HTTP Server URL [{server_url}]: ").strip() or server_url
            room_key = input("Enter Room Key (default 1234): ").strip() or "1234"

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
        err_msg = udp_mgr.last_error or "Connection failed or timed out."
        print(f"[!] {err_msg}")
        udp_mgr.stop()
        sys.exit(1)

    def on_packet_received(data):
        if len(data) >= 8 and data[0] == 0x52:  # 'R' RoIP packet
            flags = data[1]
            ptt = bool(flags & 0x01)
            cos = bool(flags & 0x02)
            pkt_type = "Heartbeat" if len(data) == 8 else f"AudioData({len(data)}B)"
            print(f"[RX] {pkt_type} | PTT={ptt} | COS={cos}")
        else:
            print(f"[RX] Raw Packet: {len(data)} bytes")

    udp_mgr.on_packet_received = on_packet_received

    print("\n[+] SUCCESS! Direct UDP Hole Punched and P2P Session Established!")
    print(f"[+] Active Peer Endpoint: {udp_mgr.peer_addr[0]}:{udp_mgr.peer_addr[1]}")
    print("[*] Connection active. Press Ctrl+C to disconnect.\n")

    # 3. Maintain connection until interrupted
    try:
        last_status_print = time.time()
        while udp_mgr.connected:
            time.sleep(0.5)
            now = time.time()
            if now - last_status_print >= 5.0 and udp_mgr.connected:
                rtt_str = f"{udp_mgr.last_rtt_ms:.1f}ms" if udp_mgr.last_rtt_ms > 0 else "measuring..."
                print(f"[*] Link active | Peer: {udp_mgr.peer_addr[0]}:{udp_mgr.peer_addr[1]} | Keepalive RTT: {rtt_str}")
                last_status_print = now
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        print("\n[*] Shutting down...")
        udp_mgr.stop()
        print("[*] Disconnected.")


if __name__ == "__main__":
    main()
