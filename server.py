"""Battleship game server (Sprint 3).

One process, one thread, non-blocking sockets multiplexed with
selectors.DefaultSelector() (see concurrency_design.md).
Messages are newline delimited UTF-8 JSON (see protocol_blueprint.md).
The server FSM follows fsm_specification.md.

Usage:
    python3 server.py -p 8080
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
TURN_TIMEOUT = 120
FLEET = [
    {"name": "Carrier", "size": 5},
    {"name": "Battleship", "size": 4},
    {"name": "Cruiser", "size": 3},
    {"name": "Submarine", "size": 3},
    {"name": "Destroyer", "size": 2},
]
MESSAGE_TYPES = [
    "CONNECT", "LOBBY_WAIT", "GAME_START", "PLACE_SHIPS", "MOVE",
    "STATE_UPDATE", "ERROR", "DISCONNECT", "GAME_OVER",
]
REQUIRED_KEYS = ["msg_type", "player_id", "timestamp", "payload"]
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
    """Read the -h and -p command line flags."""
    parser = argparse.ArgumentParser(
        description="Battleship game server. Listens on 0.0.0.0.",
        epilog="Example: python3 server.py -p 8080")
    parser.add_argument("-p", "--port", type=parse_port, required=True,
                        help="listening port (1024-65535)")
    return parser.parse_args()


def make_message(msg_type, payload):
    """Build a protocol message sent by the server."""
    return {
        "msg_type": msg_type,
        "player_id": "SERVER",
        "timestamp": int(time.time()),
        "payload": payload,
    }


def ship_cells(row, col, size, orientation):
    """Return the list of (row, col) cells a ship covers."""
    cells = []
    for i in range(size):
        if orientation == "H":
            cells.append((row, col + i))
        else:
            cells.append((row + i, col))
    return cells


def validate_placement(ships):
    """Check a PLACE_SHIPS list. Return (fleet, error_text)."""
    if not isinstance(ships, list) or len(ships) != len(FLEET):
        return None, "exactly 5 ships are required"
    sizes = {}
    for ship in FLEET:
        sizes[ship["name"]] = ship["size"]
    used_cells = set()
    placed = []
    for ship in ships:
        if not isinstance(ship, dict):
            return None, "each ship must be an object"
        name = ship.get("name")
        row = ship.get("row")
        col = ship.get("col")
        orientation = ship.get("orientation")
        if name not in sizes:
            return None, f"unknown or repeated ship '{name}'"
        if type(row) is not int or type(col) is not int:
            return None, f"{name} needs integer row and col"
        if orientation not in ("H", "V"):
            return None, f"{name} orientation must be H or V"
        cells = ship_cells(row, col, sizes[name], orientation)
        for r, c in cells:
            if r < 0 or r >= BOARD_SIZE or c < 0 or c >= BOARD_SIZE:
                return None, f"{name} goes off the board"
            if (r, c) in used_cells:
                return None, f"{name} overlaps another ship"
            used_cells.add((r, c))
        placed.append({"name": name, "cells": set(cells), "hits": set()})
        del sizes[name]
    return placed, None


class Session:
    """One connected client and its input/output buffers."""

    def __init__(self, sock, address):
        self.sock = sock
        self.address = address
        self.bin = b""
        self.bout = b""
        self.alias = None
        self.role = None
        self.closing = False


class GameServer:
    """Event loop plus the Battleship FSM."""

    def __init__(self, port):
        self.port = port
        self.sel = selectors.DefaultSelector()
        self.leaderboard = {}
        self.state = "INIT"
        self.reset_game()

    # ---------- FSM helpers ----------

    def set_state(self, new_state):
        """Move the FSM to a new state and log it."""
        if new_state != self.state:
            print(f"[FSM] {self.state} -> {new_state}")
        self.state = new_state

    def reset_game(self):
        """Clear all per-round game data."""
        self.players = {}
        self.fleets = {}
        self.shots = {}
        self.scores = {"PLAYER_1": 0, "PLAYER_2": 0}
        self.turn = None
        self.turn_started = None

    def opponent(self, role):
        if role == "PLAYER_1":
            return "PLAYER_2"
        return "PLAYER_1"

    # ---------- sending ----------

    def send(self, session, message):
        """Queue one framed message in a client's output buffer."""
        line = json.dumps(message, separators=(",", ":")) + "\n"
        session.bout += line.encode("utf-8")
        self.sel.modify(session.sock,
                        selectors.EVENT_READ | selectors.EVENT_WRITE, session)

    def send_error(self, session, code, detail):
        self.send(session, make_message("ERROR",
                                        {"code": code, "detail": detail}))

    def broadcast(self, message):
        """Queue the same message for both players."""
        for session in self.players.values():
            self.send(session, message)

    # ---------- socket events ----------

    def start(self):
        """Bind to 0.0.0.0, listen, and run the event loop."""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("0.0.0.0", self.port))
        except OSError as error:
            print(f"[ERROR] Cannot bind to port {self.port}: {error}")
            sys.exit(1)
        listener.listen()
        listener.setblocking(False)
        self.sel.register(listener, selectors.EVENT_READ, None)
        print(f"[SERVER] Listening on 0.0.0.0:{self.port}")
        self.set_state("WAITING_FOR_PLAYERS")
        try:
            while True:
                for key, mask in self.sel.select(timeout=1.0):
                    if key.data is None:
                        self.accept(key.fileobj)
                    else:
                        self.service(key, mask)
                self.check_turn_timeout()
        except KeyboardInterrupt:
            print("\n[SERVER] Shutting down")
        finally:
            self.sel.close()

    def accept(self, listener):
        sock, address = listener.accept()
        sock.setblocking(False)
        session = Session(sock, address)
        self.sel.register(sock, selectors.EVENT_READ, session)
        print(f"[SERVER] Connection from {address[0]}:{address[1]}")

    def service(self, key, mask):
        session = key.data
        if mask & selectors.EVENT_READ:
            self.read(session)
        if mask & selectors.EVENT_WRITE and session.sock.fileno() != -1:
            self.write(session)

    def read(self, session):
        """Receive bytes, split on newlines, handle each full message."""
        try:
            data = session.sock.recv(4096)
        except (ConnectionResetError, ConnectionAbortedError, TimeoutError):
            self.drop(session, "connection lost")
            return
        if not data:
            self.drop(session, "EOF")
            return
        session.bin += data
        while b"\n" in session.bin:
            line, session.bin = session.bin.split(b"\n", 1)
            if len(line) > 0:
                self.handle_line(session, line)
            if session.sock.fileno() == -1:
                return
        if len(session.bin) > MAX_MESSAGE_SIZE:
            session.bin = b""
            self.send_error(session, "MESSAGE_TOO_LARGE",
                            "more than 4096 bytes without a newline")

    def write(self, session):
        """Send whatever is waiting in the output buffer."""
        try:
            sent = session.sock.send(session.bout)
            session.bout = session.bout[sent:]
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.drop(session, "send failed")
            return
        if not session.bout:
            if session.closing:
                self.close_socket(session)
            else:
                self.sel.modify(session.sock, selectors.EVENT_READ, session)

    def close_socket(self, session):
        try:
            self.sel.unregister(session.sock)
        except (KeyError, ValueError):
            pass
        session.sock.close()

    # ---------- message handling ----------

    def handle_line(self, session, line):
        """Decode and validate one message, then dispatch it."""
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_error(session, "MALFORMED_MESSAGE", "not valid JSON")
            return
        if not isinstance(message, dict):
            self.send_error(session, "MALFORMED_MESSAGE",
                            "message must be a JSON object")
            return
        for key in REQUIRED_KEYS:
            if key not in message:
                self.send_error(session, "MALFORMED_MESSAGE",
                                f"missing key '{key}'")
                return
        if not isinstance(message["payload"], dict):
            self.send_error(session, "MALFORMED_MESSAGE",
                            "payload must be an object")
            return
        msg_type = message["msg_type"]
        if msg_type not in MESSAGE_TYPES:
            self.send_error(session, "UNKNOWN_MESSAGE_TYPE",
                            f"'{msg_type}' is not a known type")
            return
        if msg_type == "CONNECT":
            self.handle_connect(session, message)
        elif session.role is None:
            self.send_error(session, "WRONG_PHASE", "send CONNECT first")
        elif msg_type == "PLACE_SHIPS":
            self.handle_place_ships(session, message)
        elif msg_type == "MOVE":
            self.handle_move(session, message)
        elif msg_type == "DISCONNECT":
            self.drop(session, "QUIT")
        else:
            self.send_error(session, "UNKNOWN_MESSAGE_TYPE",
                            f"clients may not send {msg_type}")

    def handle_connect(self, session, message):
        alias = message["player_id"]
        if session.role is not None:
            self.send_error(session, "WRONG_PHASE", "already connected")
            return
        if not isinstance(alias, str) or not ALIAS_PATTERN.match(alias):
            self.send_error(session, "MALFORMED_MESSAGE",
                            "alias must be 1-16 letters, digits, or _")
            return
        if self.state != "WAITING_FOR_PLAYERS" or len(self.players) >= 2:
            self.send_error(session, "GAME_FULL", "two players already joined")
            session.closing = True
            return
        session.alias = alias
        if "PLAYER_1" not in self.players:
            session.role = "PLAYER_1"
        else:
            session.role = "PLAYER_2"
        self.players[session.role] = session
        print(f"[SERVER] {alias} joined as {session.role}")
        if len(self.players) == 1:
            self.send(session, make_message("LOBBY_WAIT", {
                "players_connected": 1,
                "message": "Waiting for Player 2"}))
            return
        self.set_state("GAME_START")
        for role, player in self.players.items():
            self.send(player, make_message("GAME_START", {
                "role": role,
                "opponent": self.players[self.opponent(role)].alias,
                "board_size": BOARD_SIZE,
                "fleet": FLEET}))
        self.set_state("SHIP_PLACEMENT")

    def handle_place_ships(self, session, message):
        if self.state != "SHIP_PLACEMENT":
            self.send_error(session, "WRONG_PHASE",
                            "ships can only be placed during placement")
            return
        if session.role in self.fleets:
            self.send_error(session, "WRONG_PHASE", "ships already placed")
            return
        fleet, problem = validate_placement(message["payload"].get("ships"))
        if problem:
            self.send_error(session, "INVALID_PLACEMENT", problem)
            return
        self.fleets[session.role] = fleet
        ready = {}
        for role in ("PLAYER_1", "PLAYER_2"):
            ready[role] = role in self.fleets
        self.broadcast(make_message("STATE_UPDATE",
                                    {"phase": "PLACEMENT", "ready": ready}))
        if len(self.fleets) == 2:
            for role in ("PLAYER_1", "PLAYER_2"):
                self.shots[role] = []
                for r in range(BOARD_SIZE):
                    self.shots[role].append(["."] * BOARD_SIZE)
            self.turn = random.choice(["PLAYER_1", "PLAYER_2"])
            self.turn_started = time.time()
            self.set_state("PLAYER_TURN")
            self.broadcast_battle_state(None)

    def handle_move(self, session, message):
        if self.state != "PLAYER_TURN":
            self.send_error(session, "WRONG_PHASE",
                            "moves are only allowed during battle")
            return
        if session.role != self.turn:
            self.send_error(session, "NOT_YOUR_TURN",
                            f"It is {self.turn}'s turn")
            return
        row = message["payload"].get("row")
        col = message["payload"].get("col")
        if type(row) is not int or type(col) is not int or \
                row < 0 or row >= BOARD_SIZE or col < 0 or col >= BOARD_SIZE:
            self.send_error(session, "INVALID_COORDINATES",
                            "row and col must be integers from 0 to 9")
            return
        target = self.opponent(session.role)
        if self.shots[target][row][col] != ".":
            self.send_error(session, "ALREADY_FIRED",
                            f"({row}, {col}) was already fired at")
            return
        self.set_state("EVALUATE_MOVE")
        result, sunk_name = self.fire(target, row, col)
        if result == "MISS":
            self.turn = target
        self.turn_started = time.time()
        last_shot = {"by": session.role, "row": row, "col": col,
                     "result": result, "ship_sunk": sunk_name}
        if self.ships_left(target) == 0:
            self.broadcast_battle_state(last_shot)
            self.finish_game("WIN", session.role,
                             f"All {target} ships sunk")
            return
        self.set_state("PLAYER_TURN")
        self.broadcast_battle_state(last_shot)

    def fire(self, target, row, col):
        """Mark a shot on the target board. Return (result, sunk_name)."""
        for ship in self.fleets[target]:
            if (row, col) in ship["cells"]:
                ship["hits"].add((row, col))
                self.shots[target][row][col] = "H"
                self.scores[self.opponent(target)] += 1
                if ship["hits"] == ship["cells"]:
                    return "SUNK", ship["name"]
                return "HIT", None
        self.shots[target][row][col] = "M"
        return "MISS", None

    def ships_left(self, role):
        count = 0
        for ship in self.fleets[role]:
            if ship["hits"] != ship["cells"]:
                count += 1
        return count

    def broadcast_battle_state(self, last_shot):
        boards = {}
        for role in ("PLAYER_1", "PLAYER_2"):
            rows = []
            for row in self.shots[role]:
                rows.append("".join(row))
            boards[role] = rows
        self.broadcast(make_message("STATE_UPDATE", {
            "phase": "BATTLE",
            "last_shot": last_shot,
            "turn": self.turn,
            "scores": dict(self.scores),
            "ships_remaining": {
                "PLAYER_1": self.ships_left("PLAYER_1"),
                "PLAYER_2": self.ships_left("PLAYER_2")},
            "boards": boards}))

    def check_turn_timeout(self):
        """Forfeit a player who takes longer than 120 seconds."""
        if self.state == "PLAYER_TURN" and \
                time.time() - self.turn_started > TURN_TIMEOUT:
            slow = self.turn
            print(f"[SERVER] {slow} timed out")
            self.finish_game("FORFEIT", self.opponent(slow),
                             f"{slow} took longer than {TURN_TIMEOUT}s")

    # ---------- ending ----------

    def finish_game(self, outcome, winner, reason):
        """Send GAME_OVER, update the leaderboard, and start over."""
        self.set_state("GAME_OVER")
        if winner in self.players:
            alias = self.players[winner].alias
            self.leaderboard[alias] = self.leaderboard.get(alias, 0) + 1
        self.broadcast(make_message("GAME_OVER", {
            "outcome": outcome,
            "winner": winner,
            "reason": reason,
            "final_scores": dict(self.scores),
            "leaderboard": dict(self.leaderboard)}))
        for session in self.players.values():
            session.closing = True
        self.set_state("CLEANUP")
        self.reset_game()
        self.set_state("WAITING_FOR_PLAYERS")

    def drop(self, session, reason):
        """Handle a DISCONNECT, EOF, or socket error from one client."""
        print(f"[SERVER] {session.alias or session.address} left ({reason})")
        self.close_socket(session)
        role = session.role
        if role is None or self.players.get(role) is not session:
            return
        del self.players[role]
        if self.state == "WAITING_FOR_PLAYERS":
            self.set_state("CLEANUP")
            self.reset_game()
            self.set_state("WAITING_FOR_PLAYERS")
            return
        self.finish_game("FORFEIT", self.opponent(role),
                         f"{role} disconnected")


def main():
    args = parse_args()
    GameServer(args.port).start()


if __name__ == "__main__":
    main()
