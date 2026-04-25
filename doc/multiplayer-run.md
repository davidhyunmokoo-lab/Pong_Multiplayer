# Running Multiplayer Pong

## Install dependencies

```bash
pip install -r requirements.txt
```

## Start the authoritative server

```bash
python server.py --host 0.0.0.0 --port 8765 --db pong.db
```

The server keeps live match state in memory and writes finished match data to `pong.db`.

## LAN network play (different machines, same network)

1. Find the server machine LAN IP (example: `192.168.1.50`).
2. Start server on that machine:

```bash
python server.py --host 0.0.0.0 --port 8765 --db pong.db
```

3. On each client machine:

```bash
python game.py --server ws://192.168.1.50:8765 --name Alice
```

```bash
python game.py --server ws://192.168.1.50:8765 --name Bob
```

## WAN / Internet play

1. Run the server with `--host 0.0.0.0`.
2. Allow inbound TCP `8765` in OS firewall on server host.
3. Forward router TCP port `8765` to the server machine (if behind NAT).
4. Clients connect using your public IP:

```bash
python game.py --server ws://<public-ip>:8765 --name RemotePlayer
```

Controls: `W/S` or `Up/Down`.

Note: `ws://` is unencrypted. For public deployment, terminate TLS and use `wss://` behind a reverse proxy (Nginx/Caddy/Cloudflare Tunnel).

## Runtime model implemented

- Inputs are sent from client to server with sequence numbers.
- Server simulates gameplay at fixed 60 Hz.
- Server broadcasts state snapshots at 30 Hz.
- Clients smooth movement by interpolating toward received snapshot state.

## Stored data in SQLite (`pong.db`)

- `matches`: start/end times, players, score, winner, disconnect reason.
- `player_stats`: games, wins, losses, total points, last seen.
