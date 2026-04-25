import argparse
import asyncio
import contextlib
import json
import socket
import time

import pygame
import websockets


WIDTH = 1300
HEIGHT = 800
PADDLE_W = 20
PADDLE_H = 200
BALL_SIZE = 20

BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
GRAY = (140, 140, 140)
GREEN = (80, 220, 120)


class ClientState:
    def __init__(self):
        self.side = None
        self.player_name = "You"
        self.opponent_name = "Opponent"
        self.status = "Connecting to server..."
        self.connected = False
        self.match_running = False
        self.scores = {"left": 0, "right": 0}
        self.target = None
        self.display = None


def parse_args():
    parser = argparse.ArgumentParser(description="Multiplayer Pong client")
    parser.add_argument("--server", default="ws://127.0.0.1:8765", help="WebSocket server URL")
    parser.add_argument(
        "--name",
        default=f"player-{socket.gethostname()}",
        help="Display name sent to the server",
    )
    return parser.parse_args()


async def send_json(websocket, payload):
    try:
        await websocket.send(json.dumps(payload))
    except websockets.ConnectionClosed:
        return


async def receiver_loop(websocket, state):
    async for raw_message in websocket:
        message = json.loads(raw_message)
        msg_type = message.get("type")

        if msg_type == "welcome":
            state.side = message["side"]
            state.connected = True
            state.status = "Connected. Waiting for another player..."
            state.scores = message.get("scores", state.scores)

        elif msg_type == "room_status":
            state.status = message.get("message", state.status)
            state.match_running = message.get("ready", False)
            players = message.get("players", {})
            if state.side == "left":
                state.player_name = players.get("left", state.player_name)
                state.opponent_name = players.get("right", state.opponent_name)
            else:
                state.player_name = players.get("right", state.player_name)
                state.opponent_name = players.get("left", state.opponent_name)

        elif msg_type == "snapshot":
            state.scores = message["scores"]
            state.target = message["state"]
            if state.display is None:
                state.display = dict(state.target)

        elif msg_type == "match_end":
            state.match_running = False
            state.status = message.get("message", "Match ended.")

        elif msg_type == "error":
            state.status = message.get("message", "Server error")


def get_move_direction():
    keys = pygame.key.get_pressed()
    move_up = keys[pygame.K_w] or keys[pygame.K_UP]
    move_down = keys[pygame.K_s] or keys[pygame.K_DOWN]
    if move_up and not move_down:
        return -1
    if move_down and not move_up:
        return 1
    return 0


def update_display_state(state, dt):
    if state.target is None:
        return
    if state.display is None:
        state.display = dict(state.target)
        return

    smoothing = min(1.0, dt * 12.0)
    for key in ("left_y", "right_y", "ball_x", "ball_y"):
        state.display[key] += (state.target[key] - state.display[key]) * smoothing


def draw_scene(screen, font, small_font, state):
    screen.fill(BLACK)
    pygame.draw.line(screen, GRAY, (WIDTH // 2, 0), (WIDTH // 2, HEIGHT), 2)

    left_score = state.scores.get("left", 0)
    right_score = state.scores.get("right", 0)
    score_text = font.render(f"{left_score}   :   {right_score}", True, WHITE)
    screen.blit(score_text, (WIDTH // 2 - score_text.get_width() // 2, 20))

    identity = f"{state.player_name} ({state.side or '?'}) vs {state.opponent_name}"
    info_text = small_font.render(identity, True, GRAY)
    screen.blit(info_text, (20, 20))

    status_color = GREEN if state.match_running else GRAY
    status_text = small_font.render(state.status, True, status_color)
    screen.blit(status_text, (20, HEIGHT - 40))

    if state.display:
        left_rect = pygame.Rect(50, int(state.display["left_y"]), PADDLE_W, PADDLE_H)
        right_rect = pygame.Rect(WIDTH - 70, int(state.display["right_y"]), PADDLE_W, PADDLE_H)
        ball_rect = pygame.Rect(
            int(state.display["ball_x"]),
            int(state.display["ball_y"]),
            BALL_SIZE,
            BALL_SIZE,
        )
        pygame.draw.rect(screen, WHITE, left_rect)
        pygame.draw.rect(screen, WHITE, right_rect)
        pygame.draw.rect(screen, WHITE, ball_rect)


async def run_client():
    args = parse_args()
    pygame.init()
    pygame.display.set_caption("Pong Multiplayer Client")
    screen = pygame.display.set_mode((WIDTH, HEIGHT))
    clock = pygame.time.Clock()
    font = pygame.font.SysFont(None, 60)
    small_font = pygame.font.SysFont(None, 32)
    state = ClientState()

    async with websockets.connect(args.server) as websocket:
        await send_json(websocket, {"type": "join", "name": args.name})
        recv_task = asyncio.create_task(receiver_loop(websocket, state))
        last_move = None
        input_seq = 0
        running = True

        while running:
            dt = clock.tick(120) / 1000.0
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False

            current_move = get_move_direction()
            if current_move != last_move:
                input_seq += 1
                await send_json(
                    websocket,
                    {
                        "type": "input",
                        "direction": current_move,
                        "seq": input_seq,
                        "timestamp": time.time(),
                    },
                )
                last_move = current_move

            update_display_state(state, dt)
            draw_scene(screen, font, small_font, state)
            pygame.display.flip()
            await asyncio.sleep(0)

        recv_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await recv_task

    pygame.quit()


if __name__ == "__main__":
    try:
        asyncio.run(run_client())
    except KeyboardInterrupt:
        pass
