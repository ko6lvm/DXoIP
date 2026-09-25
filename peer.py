"""
UDP Hole Puncher Client
Designed for both desktop Python 3 and MicroPython (Raspberry Pi Pico 2 W).

Workflow:
1. Binds local UDP socket.
2. Queries STUN server (RFC 5389) to find public mapped (IP, Port).
3. Takes the peer's public (IP, Port).
4. Punches UDP hole by sending periodic PUNCH packets using the SAME socket.
5. Listens for incoming PUNCH / ACK packets to confirm bidirectional traversal.
6. Enters CONNECTED state: maintains NAT pinhole with periodic PING keepalives
   and enables full-duplex messaging.
"""

import json
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
import stun
def get_local_ips():
    """Returns local LAN IP addresses of this host."""
    ips = []
    try:
        host_name = socket.gethostname()
        for ip in socket.gethostbyname_ex(host_name)[2]:
            if not ip.startswith("127."):
                ips.append(ip)
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        if local_ip not in ips and not local_ip.startswith("127."):
            ips.append(local_ip)
    except Exception:
        pass
    return ips

# Protocol packet prefixes
PREFIX_PUNCH = b"PUNCH:"
PREFIX_ACK   = b"ACK:"
PREFIX_PING  = b"PING"
PREFIX_PONG  = b"PONG"
PREFIX_MSG   = b"MSG:"

KEEPALIVE_INTERVAL = 5.0  # seconds between NAT keep-alive pings
PUNCH_INTERVAL = 0.3      # seconds between punch attempts during handshake
PUNCH_TIMEOUT = 60.0      # max seconds to attempt punching before timeout


class UDPHolePuncher:
    def __init__(self, local_port=0, stun_host="stun.l.google.com", stun_port=19302):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except (AttributeError, OSError):
            pass

        self.sock.bind(("0.0.0.0", local_port))
        self.local_port = self.sock.getsockname()[1]
        self.stun_host = stun_host
        self.stun_port = stun_port

        self.public_ip = None
        self.public_port = None
        self.peer_addr = None        # Primary target (ip, port)
        self.candidate_addrs = []   # List of (ip, port) candidates to punch

        self.connected = False
        self.running = False

        self._rx_thread = None
        self._keepalive_thread = None
        self.on_message = None  # callback: on_message(str)
        self.on_connected = None  # callback: on_connected()

    def discover_public_endpoint(self):
        """Discovers this device's public IP and port via STUN on the bound socket."""
        self.public_ip, self.public_port = stun.get_mapped_address(
            self.sock,
            stun_host=self.stun_host,
            stun_port=self.stun_port
        )
        return self.public_ip, self.public_port

    def start(self, peer_ip, peer_port, fallback_ip=None, fallback_port=None):
        """
        Starts the punch process towards peer_ip:peer_port.
        If fallback_ip:fallback_port is provided (e.g. LAN candidate), punches both.
        """
        self.peer_addr = (peer_ip, int(peer_port))
        self.candidate_addrs = [self.peer_addr]
        if fallback_ip and fallback_port:
            self.candidate_addrs.append((fallback_ip, int(fallback_port)))

        self.running = True

        # Start background receiver
        self._rx_thread = threading.Thread(target=self._receive_loop, daemon=True)
        self._rx_thread.start()

        # Punch loop
        start_time = time.time()
        punch_count = 0
        while not self.connected and self.running:
            if time.time() - start_time > PUNCH_TIMEOUT:
                raise TimeoutError("Hole punching timed out without receiving response from peer.")

            punch_count += 1
            punch_packet = PREFIX_PUNCH + str(punch_count).encode("ascii")

            # Fire at all candidate endpoints (WAN + LAN)
            for addr in self.candidate_addrs:
                try:
                    self.sock.sendto(punch_packet, addr)
                except OSError as e:
                    pass

            if punch_count % 3 == 1:
                targets_str = ", ".join(f"{a[0]}:{a[1]}" for a in self.candidate_addrs)
                print(f"[*] Sent punch #{punch_count} towards [{targets_str}]...")

            time.sleep(PUNCH_INTERVAL)

        # Start keepalive loop once connected
        if self.connected:
            self._keepalive_thread = threading.Thread(target=self._keepalive_loop, daemon=True)
            self._keepalive_thread.start()

    def send_message(self, text):
        """Sends a text message to the peer."""
        if not self.peer_addr:
            raise RuntimeError("Peer address not configured")
        packet = PREFIX_MSG + text.encode("utf-8")
        self.sock.sendto(packet, self.peer_addr)

    def _receive_loop(self):
        """Background receiver loop."""
        self.sock.settimeout(0.5)
        while self.running:
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break

            # If sender matches any candidate, or valid punch/ack received
            is_valid_sender = any(addr[0] == c[0] for c in self.candidate_addrs) or data.startswith(PREFIX_PUNCH) or data.startswith(PREFIX_ACK)

            if is_valid_sender:
                if self.peer_addr != addr:
                    print(f"\n[+] Direct connection locked to: {addr[0]}:{addr[1]}")
                    self.peer_addr = addr

                if data.startswith(PREFIX_PUNCH):
                    try:
                        self.sock.sendto(PREFIX_ACK + b"OK", self.peer_addr)
                    except OSError:
                        pass
                    if not self.connected:
                        self.connected = True
                        if self.on_connected:
                            self.on_connected()

                elif data.startswith(PREFIX_ACK):
                    if not self.connected:
                        self.connected = True
                        try:
                            self.sock.sendto(PREFIX_ACK + b"CONFIRMED", self.peer_addr)
                        except OSError:
                            pass
                        if self.on_connected:
                            self.on_connected()

                elif data == PREFIX_PING:
                    try:
                        self.sock.sendto(PREFIX_PONG, self.peer_addr)
                    except OSError:
                        pass

                elif data == PREFIX_PONG:
                    pass

                elif data.startswith(PREFIX_MSG):
                    msg_text = data[len(PREFIX_MSG):].decode("utf-8", errors="replace")
                    if self.on_message:
                        self.on_message(msg_text)
                    else:
                        print(f"\n[Peer]: {msg_text}\n> ", end="", flush=True)

    def _keepalive_loop(self):
        """Sends periodic lightweight PINGs to keep NAT pinhole alive."""
        while self.running and self.connected:
            time.sleep(KEEPALIVE_INTERVAL)
            if self.running and self.connected and self.peer_addr:
                try:
                    self.sock.sendto(PREFIX_PING, self.peer_addr)
                except OSError:
                    pass

    def stop(self):
        """Stops the hole puncher and closes the socket."""
        self.running = False
        self.connected = False
        try:
            self.sock.close()
        except OSError:
            pass


def parse_endpoint(endpoint_str):
    """Parses 'ip:port' into (ip, int(port))."""
    endpoint_str = endpoint_str.strip()
    if ":" in endpoint_str:
        ip, port = endpoint_str.rsplit(":", 1)
    else:
        raise ValueError(f"Invalid format '{endpoint_str}'. Expected <IP>:<Port>")
    return ip.strip(), int(port.strip())


def join_http_room(server_url, room_key, wan_endpoint, lan_endpoint, poll_timeout=45.0):
    """
    Contacts the HTTP matchmaker server (Python server or Cloudflare Worker)
    to exchange endpoints for a room key.
    """
    server_url = server_url.rstrip("/")
    params = urllib.parse.urlencode({
        "room": room_key,
        "wan": wan_endpoint,
        "lan": lan_endpoint
    })
    join_url = f"{server_url}/join?{params}"
    print(f"[*] Contacting Matchmaker at {server_url} (Room '{room_key}')...")

    req = urllib.request.Request(join_url, headers={"User-Agent": "UDPHolePuncher/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=50.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8")
        raise RuntimeError(f"Server returned HTTP {e.code}: {err_msg}")

    # Immediate match (Python server long-poll or Peer 2 on Cloudflare)
    if data.get("status") == "matched":
        return data["peer_wan"], data["peer_lan"]

    # Waiting state (Cloudflare Worker polling)
    if data.get("status") == "waiting":
        print(f"[*] Registered in room '{room_key}'. Waiting for peer to connect (polling Cloudflare Worker)...")
        poll_url = f"{server_url}/poll?room={urllib.parse.quote(room_key)}"
        start_time = time.time()

        while time.time() - start_time < poll_timeout:
            time.sleep(1.0)
            try:
                p_req = urllib.request.Request(poll_url, headers={"User-Agent": "UDPHolePuncher/1.0"})
                with urllib.request.urlopen(p_req, timeout=5.0) as p_resp:
                    p_data = json.loads(p_resp.read().decode("utf-8"))
                    if p_data.get("status") == "matched":
                        return p_data["peer_wan"], p_data["peer_lan"]
            except urllib.error.HTTPError:
                pass
            except Exception:
                pass

        raise TimeoutError(f"Timed out waiting for peer in Room '{room_key}' after {poll_timeout}s.")

    raise RuntimeError(f"Unexpected response from server: {data}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="P2P UDP Hole Puncher with HTTP Matchmaker & STUN")
    parser.add_argument("--port", type=int, default=0, help="Local UDP port to bind (default: random OS port)")
    parser.add_argument("--server", type=str, default="https://udp-matchmaker.lvmlabs.org", help="HTTP Matchmaker URL (default: https://udp-matchmaker.lvmlabs.org)")
    parser.add_argument("--room", type=str, default=None, help="Room key to join (e.g. 1234)")
    parser.add_argument("--peer", type=str, default=None, help="Direct remote peer endpoint (<IP>:<Port>) for manual mode")
    parser.add_argument("--stun", type=str, default="stun.l.google.com", help="STUN server host")
    parser.add_argument("--stun-port", type=int, default=19302, help="STUN server port")
    args = parser.parse_args()

    local_ips = get_local_ips()
    local_lan_ip = local_ips[0] if local_ips else "127.0.0.1"

    puncher = UDPHolePuncher(local_port=args.port, stun_host=args.stun, stun_port=args.stun_port)
    print(f"[*] Bound local UDP socket on port {puncher.local_port}")
    print(f"[*] Querying STUN server ({args.stun}:{args.stun_port})...")

    try:
        pub_ip, pub_port = puncher.discover_public_endpoint()
        my_wan = f"{pub_ip}:{pub_port}"
        my_lan = f"{local_lan_ip}:{puncher.local_port}"
        print(f"\n==========================================")
        print(f" PUBLIC (WAN) ENDPOINT : {my_wan}")
        print(f" LOCAL (LAN) ENDPOINT  : {my_lan}")
        print(f"==========================================\n")
    except Exception as e:
        print(f"[!] STUN resolution failed: {e}")
        puncher.stop()
        sys.exit(1)

    peer_wan = None
    peer_lan = None

    # Option A: HTTP Room Key matching
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

    if server_url and room_key:
        try:
            peer_wan, peer_lan = join_http_room(server_url, room_key, my_wan, my_lan)
            print(f"\n[+] MATCH FOUND in Room '{room_key}'!")
            print(f"    Peer Public (WAN): {peer_wan}")
            print(f"    Peer Local  (LAN): {peer_lan}")
        except Exception as err:
            print(f"[!] Room matchmaking failed: {err}")
            puncher.stop()
            sys.exit(1)
    else:
        # Option B: Manual mode
        peer_endpoint = args.peer
        if not peer_endpoint:
            print("Enter the PEER's endpoint (format: <IP>:<Port>):")
            peer_endpoint = input("> ").strip()
        peer_wan = peer_endpoint

    wan_ip, wan_port = parse_endpoint(peer_wan)
    lan_ip, lan_port = parse_endpoint(peer_lan) if peer_lan else (None, None)

    # Intelligently set primary & fallback targets
    if wan_ip == pub_ip and lan_ip:
        print("\n[*] Both devices share the same public IP -> Targeting Local LAN first!")
        primary_ip, primary_port = lan_ip, lan_port
        fallback_ip, fallback_port = wan_ip, wan_port
    else:
        primary_ip, primary_port = wan_ip, wan_port
        fallback_ip, fallback_port = lan_ip, lan_port

    print(f"[*] Starting hole punch towards {primary_ip}:{primary_port} (and fallback: {fallback_ip}:{fallback_port})...")
    puncher_thread = threading.Thread(
        target=puncher.start,
        args=(primary_ip, primary_port, fallback_ip, fallback_port),
        daemon=True
    )
    puncher_thread.start()

    # Wait for connection
    while not puncher.connected and puncher_thread.is_alive():
        time.sleep(0.1)

    if not puncher.connected:
        print("[!] Failed to connect to peer.")
        puncher.stop()
        sys.exit(1)

    print("\n[+] SUCCESS! Direct UDP Hole Punched and P2P Connection Established!")
    print("[+] Type a message and press Enter (or 'exit' to quit):\n")

    try:
        while puncher.connected:
            msg = input("> ").strip()
            if msg.lower() in ("exit", "quit"):
                break
            if msg:
                puncher.send_message(msg)
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        print("\n[*] Closing connection...")
        puncher.stop()


if __name__ == "__main__":
    main()

