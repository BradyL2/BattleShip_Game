"""Battleship game client (Sprint 3).

Connects to the server over TCP, speaks newline delimited JSON
(see protocol_blueprint.md), and draws the boards in the terminal.
One thread: selectors watches both the server socket and the keyboard.

Usage:
    python3 client.py -i server.langerman.edu -p 8080
"""

import argparse
import json
import random
import re
import selectors
import socket
import sys
import time

BOARD_SIZE = 10
MAX_MESSAGE_SIZE = 4096
ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,16}$")


def parse_port(text):
    """Turn a -p value into a port number between 1024 and 65535."""
    try:
        port = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"port must be a number, got '{text}'")
    if port < 1024 or port > 65535:
        raise argparse.ArgumentTypeError(
            f"port must be between 1024 and 65535, got {port}")
    return port


def parse_args():
    """Read the -h, -i, and -p command line flags."""
    parser = argparse.ArgumentParser(
        description="Battleship game client.",
        epilog="Example: python3 client.py -i server.langerman.edu -p 8080")
    parser.add_argument("-i", "--hostname", required=True,
                        help="server DNS hostname (or IP address)")
    parser.add_argument("-p", "--port", type=parse_port, required=True,
                        help="server port (1024-65535)")
    return parser.parse_args()


def resolve_hostname(hostname):
    """Look up the server's IP with DNS, or exit cleanly on failure."""
    try:
        return socket.gethostbyname(hostname)
    except socket.gaierror:
        print(f"[ERROR] Unable to resolve DNS hostname '{hostname}'.")
        print("Please check your Cisco router DNS configuration "
              "('ip dns server' / 'ip host') or verify domain spelling.")
        sys.exit(1)


def random_placement(fleet):
    """Pick random non-overlapping positions for every ship."""
    used = set()
    ships = []
    for ship in fleet:
        while True:
            orientation = random.choice(["H", "V"])
            if orientation == "H":
                row = random.randint(0, BOARD_SIZE - 1)
                col = random.randint(0, BOARD_SIZE - ship["size"])
                cells = [(row, col + i) for i in range(ship["size"])]
            else:
                row = random.randint(0, BOARD_SIZE - ship["size"])
                col = random.randint(0, BOARD_SIZE - 1)
                cells = [(row + i, col) for i in range(ship["size"])]
            if not used.intersection(cells):
                used.update(cells)
                ships.append({"name": ship["name"], "row": row, "col": col,
                              "orientation": orientation})
                break
    return ships, used


class GameClient:
    """Holds the connection and what this player knows about the game."""

    def __init__(self, sock, alias):
        self.sock = sock
        self.alias = alias
        self.buffer = b""
        self.role = None
        self.opponent_role = None
        self.opponent_alias = None
        self.my_cells = set()
        self.turn = None
        self.phase = "LOBBY"
        self.running = True

    def send(self, msg_type, payload):
        """Frame one message as compact JSON plus a newline and send it."""
        message = {"msg_type": msg_type, "player_id": self.alias,
                   "timestamp": int(time.time()), "payload": payload}
        line = json.dumps(message, separators=(",", ":")) + "\n"
        self.sock.sendall(line.encode("utf-8"))

    def receive(self):
        """Read from the socket and return every complete message."""
        data = self.sock.recv(4096)
        if not data:
            print("\n[INFO] Server closed the connection.")
            self.running = False
            return []
        self.buffer += data
        messages = []
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            if len(line) > 0:
                messages.append(json.loads(line.decode("utf-8")))
        if len(self.buffer) > MAX_MESSAGE_SIZE:
            self.buffer = b""
        return messages

    # ---------- drawing ----------

    def draw_boards(self, boards):
        """Print my fleet and my view of the opponent side by side."""
        mine = boards[self.role]
        theirs = boards[self.opponent_role]
        header = "   " + " ".join(str(c) for c in range(BOARD_SIZE))
        print(f"\n{'YOUR FLEET':<24}   {'ENEMY WATERS':<24}")
        print(f"{header}    {header}")
        for r in range(BOARD_SIZE):
            left = []
            for c in range(BOARD_SIZE):
                shot = mine[r][c]
                if shot == ".":
                    left.append("S" if (r, c) in self.my_cells else "~")
                else:
                    left.append("X" if shot == "H" else "o")
            right = []
            for c in range(BOARD_SIZE):
                shot = theirs[r][c]
                right.append({"H": "X", "M": "o"}.get(shot, "~"))
            print(f"{r:>2} {' '.join(left)}    {r:>2} {' '.join(right)}")
        print("Legend: S your ship, X hit, o miss, ~ water")

    def prompt_turn(self):
        if self.turn == self.role:
            print(">>> Your Turn: enter 'row col' (example: 3 4), "
                  "or 'quit' to forfeit")
        else:
            print(f"... Waiting for {self.opponent_alias} "
                  f"({self.opponent_role}) to fire...")

    # ---------- message handling ----------

    def handle(self, message):
        msg_type = message["msg_type"]
        payload = message["payload"]
        if msg_type == "LOBBY_WAIT":
            print(f"[LOBBY] {payload['message']}...")
        elif msg_type == "GAME_START":
            self.role = payload["role"]
            self.opponent_alias = payload["opponent"]
            if self.role == "PLAYER_1":
                self.opponent_role = "PLAYER_2"
            else:
                self.opponent_role = "PLAYER_1"
            print(f"\n[GAME START] You are {self.role} vs "
                  f"{self.opponent_alias}")
            ships, self.my_cells = random_placement(payload["fleet"])
            print("[PLACEMENT] Placing your 5 ships randomly...")
            self.send("PLACE_SHIPS", {"ships": ships})
        elif msg_type == "STATE_UPDATE":
            self.handle_state(payload)
        elif msg_type == "ERROR":
            print(f"[ERROR {payload['code']}] {payload['detail']}")
            if payload["code"] == "GAME_FULL":
                self.running = False
            elif self.turn == self.role and self.phase == "BATTLE":
                self.prompt_turn()
        elif msg_type == "GAME_OVER":
            self.show_game_over(payload)

    def handle_state(self, payload):
        if payload["phase"] == "PLACEMENT":
            ready = payload["ready"]
            print(f"[PLACEMENT] Ready - PLAYER_1: {ready['PLAYER_1']}, "
                  f"PLAYER_2: {ready['PLAYER_2']}")
            return
        self.phase = "BATTLE"
        self.turn = payload["turn"]
        shot = payload["last_shot"]
        if shot is None:
            print("\n[BATTLE] All ships placed. Battle begins!")
        else:
            who = "You" if shot["by"] == self.role else self.opponent_alias
            text = f"\n[SHOT] {who} fired at ({shot['row']}, {shot['col']})"
            text += f": {shot['result']}"
            if shot["ship_sunk"]:
                text += f" - {shot['ship_sunk']} sunk!"
            print(text)
        self.draw_boards(payload["boards"])
        scores = payload["scores"]
        remaining = payload["ships_remaining"]
        print(f"Score  You: {scores[self.role]}  "
              f"{self.opponent_alias}: {scores[self.opponent_role]}   "
              f"Ships left  You: {remaining[self.role]}  "
              f"{self.opponent_alias}: {remaining[self.opponent_role]}")
        self.prompt_turn()

    def show_game_over(self, payload):
        result = "YOU WIN!" if payload["winner"] == self.role else "You lost."
        print(f"\n[GAME OVER] {payload['outcome']}: {result} "
              f"({payload['reason']})")
        final = payload["final_scores"]
        print(f"Final scores  PLAYER_1: {final['PLAYER_1']}  "
              f"PLAYER_2: {final['PLAYER_2']}")
        if payload.get("leaderboard"):
            print(f"Leaderboard (rounds won): {payload['leaderboard']}")
        self.running = False

    def handle_input(self, line):
        """Turn a typed line into a MOVE or DISCONNECT."""
        text = line.strip().lower()
        if text in ("quit", "exit", "q"):
            self.send("DISCONNECT", {"reason": "QUIT"})
            print("[INFO] You quit the game.")
            self.running = False
            return
        if self.phase != "BATTLE":
            print("[INFO] The battle has not started yet.")
            return
        parts = text.replace(",", " ").split()
        if len(parts) != 2 or not parts[0].isdigit() or \
                not parts[1].isdigit():
            print("[INFO] Type two numbers like: 3 4")
            return
        self.send("MOVE", {"row": int(parts[0]), "col": int(parts[1])})


def ask_alias():
    """Ask for a 1-16 character alias."""
    while True:
        print("Enter your alias (1-16 letters, digits, or _): ",
              end="", flush=True)
        line = sys.stdin.readline()
        if not line:
            sys.exit(0)
        alias = line.strip()
        if ALIAS_PATTERN.match(alias):
            return alias
        print("[INFO] Invalid alias, try again.")


def main():
    args = parse_args()
    server_ip = resolve_hostname(args.hostname)
    alias = ask_alias()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect((server_ip, args.port))
    except (ConnectionRefusedError, TimeoutError, OSError) as error:
        print(f"[ERROR] Cannot connect to {args.hostname} "
              f"({server_ip}:{args.port}): {error}")
        sys.exit(1)
    print(f"[INFO] Connected to {args.hostname} ({server_ip}:{args.port})")

    client = GameClient(sock, alias)
    client.send("CONNECT", {})
    sel = selectors.DefaultSelector()
    sel.register(sock, selectors.EVENT_READ, "server")
    sel.register(sys.stdin, selectors.EVENT_READ, "keyboard")
    try:
        while client.running:
            for key, mask in sel.select():
                if key.data == "server":
                    for message in client.receive():
                        client.handle(message)
                else:
                    line = sys.stdin.readline()
                    if not line:
                        sel.unregister(sys.stdin)
                    else:
                        client.handle_input(line)
                if not client.running:
                    break
    except KeyboardInterrupt:
        client.send("DISCONNECT", {"reason": "QUIT"})
        print("\n[INFO] You quit the game.")
    except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
        print("\n[ERROR] Lost connection to the server.")
    finally:
        sel.close()
        sock.close()


if __name__ == "__main__":
    main()
