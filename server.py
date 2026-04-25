import argparse
import asyncio
import json
import random
import socket
import sqlite3
import time
from dataclasses import dataclass

import websockets


WIDTH = 1300
HEIGHT = 800
PADDLE_W = 20
PADDLE_H = 200
BALL_SIZE = 20

TICK_RATE = 60
SNAPSHOT_RATE = 30

PADDLE_SPEED = 650.0
BALL_SPEED_X = 580.0
BALL_SPEED_Y_MAX = 360.0
# --- Win Condition: first player to WIN_SCORE ends the match ---
WIN_SCORE = 13

LEFT_X = 50.0
RIGHT_X = WIDTH - 70.0


@dataclass
class Player:
    websocket: any
    name: str
    side: str
    y: float = (HEIGHT - PADDLE_H) / 2
    direction: int = 0
    last_input_seq: int = 0


@dataclass
class Ball:
    x: float
    y: float
    vx: float
    vy: float


class MatchStorage:
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        cursor = self.conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at REAL NOT NULL,
                ended_at REAL NOT NULL,
                left_player TEXT NOT NULL,
                right_player TEXT NOT NULL,
                left_score INTEGER NOT NULL,
                right_score INTEGER NOT NULL,
                winner TEXT NOT NULL,
                disconnect_reason TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS player_stats (
                player_name TEXT PRIMARY KEY,
                games INTEGER NOT NULL,
                wins INTEGER NOT NULL,
                losses INTEGER NOT NULL,
                total_points INTEGER NOT NULL,
                last_seen REAL NOT NULL
            )
            """
        )
        self.conn.commit()

    def _upsert_player(self, name: str, points: int, won: bool):
        cursor = self.conn.cursor()
        cursor.execute(
            """
            UPDATE player_stats
            SET games = games + 1,
                wins = wins + ?,
                losses = losses + ?,
                total_points = total_points + ?,
                last_seen = ?
            WHERE player_name = ?
            """,
            (1 if won else 0, 0 if won else 1, points, time.time(), name),
        )
        if cursor.rowcount == 0:
            cursor.execute(
                """
                INSERT INTO player_stats (player_name, games, wins, losses, total_points, last_seen)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (name, 1, 1 if won else 0, 0 if won else 1, points, time.time()),
            )

    def record_match(
        self,
        started_at: float,
        ended_at: float,
        left_player: str,
        right_player: str,
        left_score: int,
        right_score: int,
        disconnect_reason: str | None,
    ):
        if left_score > right_score:
            winner = left_player
        elif right_score > left_score:
            winner = right_player
        else:
            winner = "draw"

        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO matches (
                started_at, ended_at, left_player, right_player,
                left_score, right_score, winner, disconnect_reason
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                started_at,
                ended_at,
                left_player,
                right_player,
                left_score,
                right_score,
                winner,
                disconnect_reason,
            ),
        )

        self._upsert_player(left_player, left_score, left_score > right_score)
        self._upsert_player(right_player, right_score, right_score > left_score)
        self.conn.commit()


class PongRoom:
    def __init__(self, storage: MatchStorage):
        self.storage = storage
        self.lock = asyncio.Lock()
        self.players: dict[str, Player] = {}
        self.scores = {"left": 0, "right": 0}
        self.ball = self._centered_ball()
        self.running = False
        self.started_at = None
        self.tick_task = None
        # --- Serve Gate: loser must press SPACE before ball moves again ---
        self.waiting_for_serve_side: str | None = None
        self.status_message = "Waiting for players..."

    def _centered_ball(self):
        return Ball(
            x=(WIDTH - BALL_SIZE) / 2,
            y=(HEIGHT - BALL_SIZE) / 2,
            vx=0.0,
            vy=0.0,
        )

    def _launch_ball_from_side(self, serving_side: str):
        direction = 1 if serving_side == "left" else -1
        vertical = random.uniform(-BALL_SPEED_Y_MAX * 0.75, BALL_SPEED_Y_MAX * 0.75)
        minimum_vertical = BALL_SPEED_Y_MAX * 0.15
        if abs(vertical) < minimum_vertical:
            vertical = minimum_vertical if vertical >= 0 else -minimum_vertical
        return Ball(
            x=(WIDTH - BALL_SIZE) / 2,
            y=(HEIGHT - BALL_SIZE) / 2,
            vx=BALL_SPEED_X * direction,
            vy=vertical,
        )

    def _serve_prompt(self, side: str, prefix: str):
        name = self.players[side].name if side in self.players else side
        return f"{prefix} {name} press SPACE to serve."

    def _room_names(self):
        return {
            "left": self.players["left"].name if "left" in self.players else "waiting...",
            "right": self.players["right"].name if "right" in self.players else "waiting...",
        }

    async def _safe_send(self, websocket, payload):
        try:
            await websocket.send(json.dumps(payload))
        except websockets.ConnectionClosed:
            pass

    async def _broadcast(self, payload):
        sockets = []
        async with self.lock:
            sockets = [player.websocket for player in self.players.values()]
        if not sockets:
            return
        await asyncio.gather(*(self._safe_send(socket, payload) for socket in sockets))

    async def _broadcast_room_status(self):
        ready = len(self.players) == 2 and self.running
        message = self.status_message or ("Match is live." if ready else "Waiting for another player...")
        payload = {
            "type": "room_status",
            "ready": ready,
            "message": message,
            "players": self._room_names(),
        }
        await self._broadcast(payload)

    async def add_player(self, websocket, name: str):
        async with self.lock:
            if "left" not in self.players:
                side = "left"
            elif "right" not in self.players:
                side = "right"
            else:
                return None
            self.players[side] = Player(websocket=websocket, name=name, side=side)
            if len(self.players) == 2 and not self.running:
                self.running = True
                self.started_at = time.time()
                self.scores = {"left": 0, "right": 0}
                self.waiting_for_serve_side = random.choice(["left", "right"])
                self.ball = self._centered_ball()
                self.status_message = self._serve_prompt(self.waiting_for_serve_side, "Match start.")
                if self.tick_task is None or self.tick_task.done():
                    self.tick_task = asyncio.create_task(self._tick_loop())
            return side

    async def remove_player(self, side: str, reason: str):
        ended_payload = None
        async with self.lock:
            if side not in self.players:
                return

            was_running = self.running and len(self.players) == 2
            left_name = self.players["left"].name if "left" in self.players else "left-player"
            right_name = self.players["right"].name if "right" in self.players else "right-player"

            del self.players[side]
            self.waiting_for_serve_side = None
            self.status_message = "Waiting for another player..."

            if was_running:
                self.running = False
                self.storage.record_match(
                    started_at=self.started_at or time.time(),
                    ended_at=time.time(),
                    left_player=left_name,
                    right_player=right_name,
                    left_score=self.scores["left"],
                    right_score=self.scores["right"],
                    disconnect_reason=reason,
                )
                ended_payload = {
                    "type": "match_end",
                    "message": f"Match ended due to disconnect: {reason}",
                }

        if ended_payload:
            await self._broadcast(ended_payload)
        await self._broadcast_room_status()

    async def set_input(self, side: str, direction: int, seq: int):
        async with self.lock:
            player = self.players.get(side)
            if not player:
                return
            if seq < player.last_input_seq:
                return
            player.last_input_seq = seq
            if direction < 0:
                player.direction = -1
            elif direction > 0:
                player.direction = 1
            else:
                player.direction = 0

    async def set_serve(self, side: str):
        snapshot_payload = None
        async with self.lock:
            if not self.running or len(self.players) < 2:
                return
            if self.waiting_for_serve_side != side:
                return

            self.ball = self._launch_ball_from_side(side)
            self.waiting_for_serve_side = None
            self.status_message = "Match is live."
            snapshot_payload = self._snapshot_payload()

        await self._broadcast_room_status()
        if snapshot_payload:
            await self._broadcast(snapshot_payload)

    def _vertical_sweep_overlap(self, previous_y: float, current_y: float, paddle_y: float):
        ball_top = min(previous_y, current_y)
        ball_bottom = max(previous_y + BALL_SIZE, current_y + BALL_SIZE)
        paddle_top = paddle_y
        paddle_bottom = paddle_y + PADDLE_H
        return ball_top < paddle_bottom and ball_bottom > paddle_top

    def _check_ball_paddle_collision(self, paddle_x: float, paddle_y: float):
        ball_left = self.ball.x
        ball_right = self.ball.x + BALL_SIZE
        ball_top = self.ball.y
        ball_bottom = self.ball.y + BALL_SIZE

        paddle_left = paddle_x
        paddle_right = paddle_x + PADDLE_W
        paddle_top = paddle_y
        paddle_bottom = paddle_y + PADDLE_H

        horizontal_overlap = ball_left < paddle_right and ball_right > paddle_left
        vertical_overlap = ball_top < paddle_bottom and ball_bottom > paddle_top
        return horizontal_overlap and vertical_overlap

    def _simulate_tick(self, dt: float):
        room_status_changed = False
        match_end_payload = None
        force_snapshot = False

        left = self.players["left"]
        right = self.players["right"]

        left.y = max(0.0, min(HEIGHT - PADDLE_H, left.y + (left.direction * PADDLE_SPEED * dt)))
        right.y = max(0.0, min(HEIGHT - PADDLE_H, right.y + (right.direction * PADDLE_SPEED * dt)))

        if self.waiting_for_serve_side is not None:
            return room_status_changed, match_end_payload, force_snapshot

        previous_x = self.ball.x
        previous_y = self.ball.y
        self.ball.x += self.ball.vx * dt
        self.ball.y += self.ball.vy * dt

        if self.ball.y <= 0:
            self.ball.y = 0
            self.ball.vy *= -1
        elif self.ball.y >= HEIGHT - BALL_SIZE:
            self.ball.y = HEIGHT - BALL_SIZE
            self.ball.vy *= -1

        # --- Paddle Collision Fix: swept check to prevent ball passing through paddles ---
        left_face = LEFT_X + PADDLE_W
        crossed_left_face = previous_x >= left_face and self.ball.x <= left_face
        left_vertical_hit = self._vertical_sweep_overlap(previous_y, self.ball.y, left.y)
        if self.ball.vx < 0 and (
            (crossed_left_face and left_vertical_hit) or self._check_ball_paddle_collision(LEFT_X, left.y)
        ):
            self.ball.x = LEFT_X + PADDLE_W
            self.ball.vx = abs(self.ball.vx)
            impact = ((self.ball.y + BALL_SIZE / 2) - (left.y + PADDLE_H / 2)) / (PADDLE_H / 2)
            self.ball.vy = impact * BALL_SPEED_Y_MAX

        right_face = RIGHT_X
        previous_right = previous_x + BALL_SIZE
        current_right = self.ball.x + BALL_SIZE
        crossed_right_face = previous_right <= right_face and current_right >= right_face
        right_vertical_hit = self._vertical_sweep_overlap(previous_y, self.ball.y, right.y)
        if self.ball.vx > 0 and (
            (crossed_right_face and right_vertical_hit) or self._check_ball_paddle_collision(RIGHT_X, right.y)
        ):
            self.ball.x = RIGHT_X - BALL_SIZE
            self.ball.vx = -abs(self.ball.vx)
            impact = ((self.ball.y + BALL_SIZE / 2) - (right.y + PADDLE_H / 2)) / (PADDLE_H / 2)
            self.ball.vy = impact * BALL_SPEED_Y_MAX

        if self.ball.x < 0:
            self.scores["right"] += 1
            losing_side = "left"
            if self.scores["right"] >= WIN_SCORE:
                self.running = False
                winner_name = right.name
                self.status_message = f"{winner_name} wins {self.scores['right']}:{self.scores['left']}."
                self.storage.record_match(
                    started_at=self.started_at or time.time(),
                    ended_at=time.time(),
                    left_player=left.name,
                    right_player=right.name,
                    left_score=self.scores["left"],
                    right_score=self.scores["right"],
                    disconnect_reason=None,
                )
                match_end_payload = {
                    "type": "match_end",
                    "message": f"{winner_name} reached {WIN_SCORE} and wins the match.",
                    "scores": dict(self.scores),
                }
                room_status_changed = True
                force_snapshot = True
            else:
                self.ball = self._centered_ball()
                self.waiting_for_serve_side = losing_side
                self.status_message = self._serve_prompt(losing_side, "Point scored.")
                room_status_changed = True
                force_snapshot = True
        elif self.ball.x > WIDTH - BALL_SIZE:
            self.scores["left"] += 1
            losing_side = "right"
            if self.scores["left"] >= WIN_SCORE:
                self.running = False
                winner_name = left.name
                self.status_message = f"{winner_name} wins {self.scores['left']}:{self.scores['right']}."
                self.storage.record_match(
                    started_at=self.started_at or time.time(),
                    ended_at=time.time(),
                    left_player=left.name,
                    right_player=right.name,
                    left_score=self.scores["left"],
                    right_score=self.scores["right"],
                    disconnect_reason=None,
                )
                match_end_payload = {
                    "type": "match_end",
                    "message": f"{winner_name} reached {WIN_SCORE} and wins the match.",
                    "scores": dict(self.scores),
                }
                room_status_changed = True
                force_snapshot = True
            else:
                self.ball = self._centered_ball()
                self.waiting_for_serve_side = losing_side
                self.status_message = self._serve_prompt(losing_side, "Point scored.")
                room_status_changed = True
                force_snapshot = True

        return room_status_changed, match_end_payload, force_snapshot

    def _snapshot_payload(self):
        left = self.players["left"]
        right = self.players["right"]
        return {
            "type": "snapshot",
            "scores": self.scores,
            "state": {
                "left_y": left.y,
                "right_y": right.y,
                "ball_x": self.ball.x,
                "ball_y": self.ball.y,
            },
        }

    async def _tick_loop(self):
        tick_interval = 1.0 / TICK_RATE
        snapshot_interval = 1.0 / SNAPSHOT_RATE
        last_tick = time.perf_counter()
        last_snapshot = last_tick

        while True:
            now = time.perf_counter()
            dt = now - last_tick
            if dt <= 0:
                dt = tick_interval
            last_tick = now

            should_stop = False
            payload = None
            room_status_changed = False
            match_end_payload = None
            async with self.lock:
                if not self.running or len(self.players) < 2:
                    should_stop = True
                else:
                    room_status_changed, match_end_payload, force_snapshot = self._simulate_tick(dt)
                    if (now - last_snapshot >= snapshot_interval) or force_snapshot:
                        payload = self._snapshot_payload()
                        last_snapshot = now
                    if not self.running:
                        should_stop = True

            if room_status_changed:
                await self._broadcast_room_status()
            if match_end_payload:
                await self._broadcast(match_end_payload)
            if should_stop:
                return
            if payload:
                await self._broadcast(payload)

            elapsed = time.perf_counter() - now
            await asyncio.sleep(max(0.0, tick_interval - elapsed))


async def client_handler(websocket, room: PongRoom):
    side = None
    try:
        raw_join = await asyncio.wait_for(websocket.recv(), timeout=10.0)
        join_message = json.loads(raw_join)
        if join_message.get("type") != "join":
            await websocket.send(json.dumps({"type": "error", "message": "First message must be type=join"}))
            return

        player_name = str(join_message.get("name") or "anonymous").strip()[:48]
        if not player_name:
            player_name = "anonymous"

        side = await room.add_player(websocket, player_name)
        if side is None:
            await websocket.send(json.dumps({"type": "error", "message": "Room is full"}))
            return

        await room._safe_send(
            websocket,
            {
                "type": "welcome",
                "side": side,
                "scores": room.scores,
            },
        )
        await room._broadcast_room_status()

        async for raw in websocket:
            message = json.loads(raw)
            if message.get("type") == "input":
                direction = int(message.get("direction", 0))
                seq = int(message.get("seq", 0))
                await room.set_input(side, direction, seq)
            elif message.get("type") == "serve":
                await room.set_serve(side)
    except (asyncio.TimeoutError, json.JSONDecodeError):
        pass
    except websockets.ConnectionClosed:
        pass
    finally:
        if side is not None:
            await room.remove_player(side, reason=f"{side} player disconnected")


def parse_args():
    parser = argparse.ArgumentParser(description="Multiplayer Pong authoritative server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", default=8765, type=int, help="Bind port")
    parser.add_argument("--db", default="pong.db", help="SQLite file path")
    return parser.parse_args()


def detect_lan_ip():
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


async def main():
    args = parse_args()
    storage = MatchStorage(args.db)
    room = PongRoom(storage)

    async def handler(websocket):
        await client_handler(websocket, room)

    async with websockets.serve(handler, args.host, args.port):
        print(f"Pong server listening on ws://{args.host}:{args.port}")
        if args.host == "0.0.0.0":
            lan_ip = detect_lan_ip()
            print(f"LAN clients can connect with: ws://{lan_ip}:{args.port}")
            print(f"For WAN access, forward TCP {args.port} and open firewall rules on this host.")
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
