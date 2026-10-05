"""
UDP Manager for DXoIP
Handles local socket binding, STUN discovery (RFC 5389), HTTP matchmaker signaling,
UDP hole punching across candidate endpoints, keepalive, and 10s watchdog tracking.
Compatible with standard Python 3 and MicroPython (Raspberry Pi Pico 2 W).
"""

import json
import socket
import struct
import sys
import threading
import time
import urllib.parse
import urllib.request
import stun

# Wire control packet prefixes
PREFIX_PUNCH   = b"PUNCH:"
PREFIX_ACK     = b"ACK:"
PREFIX_PING    = b"PING"
PREFIX_PONG    = b"PONG"
PREFIX_TIMEOUT = b"TIMEOUT"

KEEPALIVE_INTERVAL = 5.0  # seconds between NAT keep-alive pings
PUNCH_INTERVAL     = 0.3  # seconds between punch attempts during handshake
PUNCH_TIMEOUT      = 60.0 # max seconds to attempt punching before timeout
WATCHDOG_TIMEOUT   = 10.0 # seconds of silence before triggering disconnect


def get_local_ips():
    """Returns local LAN IP addresses of this host, prioritizing the routable default interface."""
    ips = []
    # 1. Primary routable interface via UDP dummy connect
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        if not local_ip.startswith("127."):
            ips.append(local_ip)
    except Exception:
        pass

    # 2. Additional host interfaces
    try:
        host_name = socket.gethostname()
        for ip in socket.gethostbyname_ex(host_name)[2]:
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass

    return ips


def parse_endpoint(endpoint_str):
    """Parses 'ip:port' into (ip, int(port))."""
    endpoint_str = endpoint_str.strip()
    if ":" in endpoint_str:
        ip, port = endpoint_str.rsplit(":", 1)
    else:
        raise ValueError(f"Invalid format '{endpoint_str}'. Expected <IP>:<Port>")
    return ip.strip(), int(port.strip())


class UDPManager:
    """
    Self-contained UDP Network Transport Manager.
    Handles STUN discovery, room matchmaking, hole punching, keepalive,
    and raw packet delivery.
    """

    def __init__(self, local_port=0, stun_host="stun.l.google.com", stun_port=19302):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if local_port == 0:
            try:
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            except (AttributeError, OSError):
                pass

        # Increase UDP socket buffer sizes to absorb bursts and prevent packet drops
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
        except (AttributeError, OSError):
            pass

        self.sock.bind(("0.0.0.0", local_port))
        self.local_port = self.sock.getsockname()[1]
        self.stun_host = stun_host
        self.stun_port = stun_port

        self.public_ip = None
        self.public_port = None
        self.peer_addr = None        # Locked remote target (ip, port)
        self.candidate_addrs = []   # Candidate endpoints to punch (WAN, LAN)

        self.connected = False
        self.running = False
        self.last_rx_time = 0.0
        self.last_rtt_us = 0
        self.last_rtt_ms = 0.0
        self.last_error = None

        # Background threads
        self._rx_thread = None
        self._watchdog_thread = None

        # Event callbacks
        self.on_packet_received = None  # callback: on_packet_received(data: bytes)
        self.on_connected = None        # callback: on_connected()
        self.on_disconnected = None     # callback: on_disconnected(reason: str)

    def discover_public_endpoint(self):
        """Discovers this device's public IP and port via STUN on the bound socket."""
        self.public_ip, self.public_port = stun.get_mapped_address(
            self.sock,
            stun_host=self.stun_host,
            stun_port=self.stun_port
        )
        return self.public_ip, self.public_port

    def get_endpoints(self):
        """Returns ((wan_ip, wan_port), (lan_ip, lan_port))."""
        if not self.public_ip:
            self.discover_public_endpoint()
        local_ips = get_local_ips()
        lan_ip = local_ips[0] if local_ips else "127.0.0.1"
        return (self.public_ip, self.public_port), (lan_ip, self.local_port)

    # --- Matchmaking Signaling ---

    def _http_request(self, url, timeout=10.0):
        """Performs HTTP GET with User-Agent header and returns parsed JSON."""
        req = urllib.request.Request(url, headers={"User-Agent": "DXoIP-UDPManager/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def join_matchmaker_room(self, server_url, room_key, poll_timeout=45.0):
        """
        Contacts the HTTP matchmaker server to register endpoints and retrieve peer endpoints.
        Includes automatic recovery from stale rooms (HTTP 409) and rejection of self-matches.
        Returns: (peer_wan_str, peer_lan_str)
        """
        wan_ep, lan_ep = self.get_endpoints()
        my_wan = f"{wan_ep[0]}:{wan_ep[1]}"
        my_lan = f"{lan_ep[0]}:{lan_ep[1]}"

        server_url = server_url.rstrip("/")
        params = urllib.parse.urlencode({
            "room": room_key,
            "wan": my_wan,
            "lan": my_lan
        })
        join_url = f"{server_url}/join?{params}"
        poll_url = f"{server_url}/poll?room={urllib.parse.quote(room_key)}"

        data = None
        try:
            data = self._http_request(join_url, timeout=15.0)
        except urllib.error.HTTPError as e:
            if e.code == 409:
                # Room already matched or in dirty state from a prior dead session.
                # Attempt to consume and reset the stale room by polling it.
                try:
                    self._http_request(poll_url, timeout=5.0)
                except Exception:
                    pass
                time.sleep(0.5)
                # Retry /join once
                try:
                    data = self._http_request(join_url, timeout=15.0)
                except urllib.error.HTTPError as retry_e:
                    if retry_e.code == 409:
                        raise RuntimeError(
                            f"Room '{room_key}' is currently occupied by active peers. "
                            f"Please select a different room key or retry shortly."
                        )
                    raise RuntimeError(f"Matchmaker server HTTP {retry_e.code}: {retry_e.read().decode('utf-8', errors='ignore')}")
            else:
                err_msg = e.read().decode("utf-8", errors="ignore")
                raise RuntimeError(f"Matchmaker server HTTP {e.code}: {err_msg}")
        except Exception as e:
            raise RuntimeError(f"Failed to reach matchmaker server ({server_url}): {e}")

        # Check for self-match if server returned immediate match (e.g. re-joining same room)
        if data.get("status") == "matched":
            peer_wan = data.get("peer_wan")
            peer_lan = data.get("peer_lan")
            if peer_wan == my_wan and peer_lan == my_lan:
                # Matched with our own previous dead registration!
                # Consume that match and re-join fresh
                try:
                    self._http_request(poll_url, timeout=5.0)
                except Exception:
                    pass
                time.sleep(0.5)
                try:
                    data = self._http_request(join_url, timeout=15.0)
                except Exception as e:
                    raise RuntimeError(f"Error re-registering room '{room_key}' after self-match reset: {e}")

        # If now matched with a valid distinct peer:
        if data.get("status") == "matched":
            peer_wan = data.get("peer_wan")
            peer_lan = data.get("peer_lan")
            if peer_wan != my_wan or peer_lan != my_lan:
                return peer_wan, peer_lan

        # Waiting state (Peer 1 registered, poll for Peer 2)
        if data.get("status") == "waiting":
            start_time = time.time()

            while time.time() - start_time < poll_timeout:
                time.sleep(1.0)
                try:
                    p_data = self._http_request(poll_url, timeout=5.0)
                    if p_data.get("status") == "matched":
                        p_wan = p_data.get("peer_wan")
                        p_lan = p_data.get("peer_lan")
                        if p_wan == my_wan and p_lan == my_lan:
                            # Matched with self; continue waiting for actual peer
                            continue
                        return p_wan, p_lan
                except urllib.error.HTTPError as e:
                    if e.code == 404:
                        # Room expired on server while waiting; re-register to keep room alive
                        try:
                            re_data = self._http_request(join_url, timeout=5.0)
                            if re_data.get("status") == "matched":
                                r_wan = re_data.get("peer_wan")
                                r_lan = re_data.get("peer_lan")
                                if r_wan != my_wan or r_lan != my_lan:
                                    return r_wan, r_lan
                        except Exception:
                            pass
                except Exception:
                    pass

            raise TimeoutError(f"Timed out waiting for peer in Room '{room_key}' after {int(poll_timeout)}s.")

        raise RuntimeError(f"Unexpected response from server: {data}")

    def connect_room(self, room_key, server_url="https://udp-matchmaker.lvmlabs.org"):
        """Resolves peer endpoints via HTTP matchmaker room, then starts hole punching."""
        self.last_error = None
        try:
            peer_wan, peer_lan = self.join_matchmaker_room(server_url, room_key)
            wan_ip, wan_port = parse_endpoint(peer_wan)
            lan_ip, lan_port = parse_endpoint(peer_lan) if peer_lan else (None, None)

            # Hairpinning check: if same WAN IP, prioritize LAN candidate
            if wan_ip == self.public_ip and lan_ip:
                return self.punch_candidates(primary=(lan_ip, lan_port), fallback=(wan_ip, wan_port))
            else:
                return self.punch_candidates(primary=(wan_ip, wan_port), fallback=(lan_ip, lan_port) if lan_ip else None)
        except Exception as e:
            self.last_error = str(e)
            raise e

    def connect_peer(self, peer_ip, peer_port, fallback_ip=None, fallback_port=None):
        """Direct connection to a known endpoint without using the HTTP matchmaker."""
        self.last_error = None
        try:
            if not self.public_ip:
                try:
                    self.discover_public_endpoint()
                except Exception:
                    pass
            fallback = (fallback_ip, int(fallback_port)) if fallback_ip and fallback_port else None
            return self.punch_candidates(primary=(peer_ip, int(peer_port)), fallback=fallback)
        except Exception as e:
            self.last_error = str(e)
            raise e

    # --- Hole Punching Engine ---

    def punch_candidates(self, primary, fallback=None):
        """Punches UDP hole towards candidate endpoints."""
        self.peer_addr = primary
        self.candidate_addrs = [primary]
        if fallback and fallback not in self.candidate_addrs:
            self.candidate_addrs.append(fallback)

        self.running = True
        self.last_rx_time = time.time()

        # Start receiver thread if not already running
        if not self._rx_thread or not self._rx_thread.is_alive():
            self._rx_thread = threading.Thread(target=self._receive_loop, daemon=True)
            self._rx_thread.start()

        # Punch loop
        start_time = time.time()
        punch_count = 0
        while not self.connected and self.running:
            if time.time() - start_time > PUNCH_TIMEOUT:
                self.stop()
                raise TimeoutError("Hole punching timed out without receiving response from peer.")

            punch_count += 1
            punch_packet = PREFIX_PUNCH + str(punch_count).encode("ascii")

            for addr in self.candidate_addrs:
                try:
                    self.sock.sendto(punch_packet, addr)
                except OSError:
                    pass

            time.sleep(PUNCH_INTERVAL)

        # Once connected, start watchdog and keepalive thread
        if self.connected:
            if not self._watchdog_thread or not self._watchdog_thread.is_alive():
                self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
                self._watchdog_thread.start()

        return self.connected

    # --- Outbound Transmission ---

    def send_packet(self, data: bytes):
        """Sends a raw byte packet to the connected peer."""
        if not self.peer_addr:
            raise RuntimeError("Peer address not configured or connected")
        try:
            self.sock.sendto(data, self.peer_addr)
        except OSError as e:
            if not self.running:
                return
            raise e

    # --- Background Loops ---

    def _receive_loop(self):
        """Background receiver loop handling control packets and forwarding data."""
        self.sock.settimeout(0.5)
        while self.running:
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break

            # Fast-path for locked peer application packets (e.g. RoIP 50 Hz audio frames)
            if addr == self.peer_addr and data:
                first_byte = data[0]
                # Control packets begin with b'P' (0x50), b'A' (0x41), or b'T' (0x54)
                if first_byte not in (0x50, 0x41, 0x54):
                    self.last_rx_time = time.time()
                    if self.on_packet_received:
                        self.on_packet_received(data)
                    continue

            # Validate sender is one of the candidates or packet is handshake
            is_valid_sender = (
                addr == self.peer_addr
                or any(addr[0] == c[0] for c in self.candidate_addrs)
                or data.startswith(PREFIX_PUNCH)
                or data.startswith(PREFIX_ACK)
            )

            if not is_valid_sender:
                continue

            self.last_rx_time = time.time()

            # Lock peer address if different
            if self.peer_addr != addr:
                self.peer_addr = addr

            # Control: PUNCH
            if data.startswith(PREFIX_PUNCH):
                try:
                    self.sock.sendto(PREFIX_ACK + b"OK", self.peer_addr)
                except OSError:
                    pass
                if not self.connected:
                    self.connected = True
                    if self.on_connected:
                        self.on_connected()

            # Control: ACK
            elif data.startswith(PREFIX_ACK):
                if not self.connected:
                    self.connected = True
                    try:
                        self.sock.sendto(PREFIX_ACK + b"CONFIRMED", self.peer_addr)
                    except OSError:
                        pass
                    if self.on_connected:
                        self.on_connected()

            # Control: PING
            elif data.startswith(PREFIX_PING):
                # Echo timestamp payload back in PONG for RTT calculation
                pong_payload = PREFIX_PONG + data[len(PREFIX_PING):]
                try:
                    self.sock.sendto(pong_payload, self.peer_addr)
                except OSError:
                    pass

            # Control: PONG
            elif data.startswith(PREFIX_PONG):
                pong_payload = data[len(PREFIX_PONG):]
                if len(pong_payload) >= 4:
                    try:
                        send_ts = struct.unpack("!I", pong_payload[:4])[0]
                        now_us = int(time.time() * 1_000_000) & 0xFFFFFFFF
                        rtt = (now_us - send_ts) & 0xFFFFFFFF
                        self.last_rtt_us = rtt
                        self.last_rtt_ms = rtt / 1000.0
                    except Exception:
                        pass

            # Control: TIMEOUT from peer
            elif data == PREFIX_TIMEOUT:
                self._handle_disconnect(reason="Peer sent TIMEOUT notification")

            # RoIP / User Application Data
            else:
                if self.on_packet_received:
                    self.on_packet_received(data)

    def _watchdog_loop(self):
        """Periodic keepalive pinger and 10s inactivity watchdog."""
        last_ping_time = time.time()
        while self.running and self.connected:
            time.sleep(0.5)
            if not self.running or not self.connected:
                break

            now = time.time()

            # 1. Send keepalive PING to hold NAT pinhole open every KEEPALIVE_INTERVAL
            if now - last_ping_time >= KEEPALIVE_INTERVAL:
                last_ping_time = now
                if self.peer_addr:
                    try:
                        ts_us = int(now * 1_000_000) & 0xFFFFFFFF
                        ping_pkt = PREFIX_PING + struct.pack("!I", ts_us)
                        self.sock.sendto(ping_pkt, self.peer_addr)
                    except OSError:
                        pass

            # 2. Check 10-second inactivity timeout promptly
            silence_duration = now - self.last_rx_time
            if silence_duration > WATCHDOG_TIMEOUT:
                # Transmit TIMEOUT notification packet to peer before disconnecting
                try:
                    if self.peer_addr:
                        self.sock.sendto(PREFIX_TIMEOUT, self.peer_addr)
                except OSError:
                    pass
                self._handle_disconnect(reason=f"Inactivity watchdog triggered ({silence_duration:.1f}s silence)")
                break

    def _handle_disconnect(self, reason="Disconnected"):
        """Internal handler for link disconnection."""
        if not self.connected:
            return
        self.connected = False
        if self.on_disconnected:
            try:
                self.on_disconnected(reason)
            except Exception:
                pass

    def stop(self):
        """Stops the hole puncher and closes the socket cleanly."""
        self.running = False
        self.connected = False
        try:
            self.sock.close()
        except OSError:
            pass
