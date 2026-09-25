# DXoIP

A lightweight, zero-dependency peer-to-peer UDP hole puncher in Python with a serverless Cloudflare Worker matchmaker and RFC 5389 STUN discovery. Connect any two devices directly across NATs and firewalls without port forwarding or central relay servers.

## Quickstart

Run on two different machines (across separate Wi-Fi, cellular, or NAT networks):

### Device 1:
```bash
python peer.py --room 1234
```

### Device 2:
```bash
python peer.py --room 1234
```

## CLI Reference

```text
usage: peer.py [-h] [--port PORT] [--server SERVER] [--room ROOM]
               [--peer PEER] [--stun STUN] [--stun-port STUN_PORT]

options:
  -h, --help            Show this help message and exit
  --port PORT           Local UDP port to bind (default: 0 / dynamic OS port)
  --server SERVER       HTTP Matchmaker URL (default: https://udp-matchmaker.lvmlabs.org)
  --room ROOM           Room key to pair with peer (e.g. 1234)
  --peer PEER           Direct remote endpoint (<IP>:<Port>) for manual punch without matchmaker
  --stun STUN           STUN server hostname (default: stun.l.google.com)
  --stun-port STUN_PORT STUN server port (default: 19302)
```

---

## Repository Structure

```text
udptest/
├── peer.py               # P2P UDP client & interactive terminal chat
├── stun.py               # RFC 5389 STUN binary protocol implementation
├── README.md             # Documentation and usage guide
└── cloudflare/           # Serverless matchmaker service
    ├── worker.js         # Matchmaker API (/join and /poll endpoints)
    └── wrangler.toml     # Cloudflare Workers configuration
```

---

## Self-Hosting the Matchmaker

A live public matchmaker is pre-configured at `https://udp-matchmaker.lvmlabs.org`.

To deploy your own free instance on Cloudflare Workers:

1. Install the Cloudflare Wrangler CLI:
   ```bash
   npm install -g wrangler
   ```
2. Authenticate:
   ```bash
   wrangler login
   ```
3. Deploy from the `cloudflare` folder:
   ```bash
   cd cloudflare
   wrangler deploy
   ```
4. Run `peer.py` pointing to your custom worker:
   ```bash
   python peer.py --server https://<your-worker>.<your-subdomain>.workers.dev --room 1234
   ```

## License

MIT
