# Pong Multiplayer

## 1. Install Dependencies

From the project root, install everything in `requirements.txt`:

```bash
pip install -r requirements.txt
```

## 2. Run the Server

Start the authoritative game server:

```bash
python server.py --host 0.0.0.0 --port 8765 --db pong.db
```

- `--host 0.0.0.0` lets other machines connect.
- `--port 8765` is the websocket port.
- `--db pong.db` stores match/player stats in SQLite.

## 3. Run the Clients

Open one terminal per player.

### Local machine test (same PC)

```bash
python game.py --server ws://127.0.0.1:8765 --name Player1
```

```bash
python game.py --server ws://127.0.0.1:8765 --name Player2
```

### LAN test (different PCs on same network)

Replace `192.168.1.50` with the server machine LAN IP:

```bash
python game.py --server ws://192.168.1.50:8765 --name Player1
```

```bash
python game.py --server ws://192.168.1.50:8765 --name Player2
```

## 4. Controls

- `W/S` or `Up/Down` to move paddle.
