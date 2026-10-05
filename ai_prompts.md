# AI Prompting and Constraint Strategy

**Author:** Brady Langerman
**Game:** 2 player Battleship over TCP
**Date:** 10-4-2026

## Strategy

I would prompt AI coding tools to not guess at message formats, add random fields, or assume 'recv()' returns entire messages. To avoid this, I will include every prompt with

1. **Paste the exact schema from** 'protocol_blueprint.md', instead of trying to describe it myself
2. **State the explicit framing rule:** newline delimited, a 4096 byte limit, and UTF-8 encoded.
3. **Disallow any extra fields or unnecessary libraries:** No unnecessary keys outside of the schema and only use the standard Python library.
4. **List exact error codes:** That way, each failure is mapped to a specific protocol 'ERROR' message.
5. **Require validation and testing:** To verify the code, reduce hallucinations, and not blindly trust any invalid input.

## Prompt 1: System prompt
```
You are writing Python 3 networking code for a CLI Battleship two-player game. Follow these rules explicitly:
- Use only the Python standard library (with socket, json, and select).
- Messages are UTF-8 encoded compact JSON objects, with one per line, and ending with a single byte of a newline at the end (\n). 
- Every message is sent with these exact keys: msg_type (String), player_id (String), timestamp (Integer, Unix seconds), payload (Object).
- Valid msg_type values: CONNECT, GAME_START, PLACE_SHIPS, MOVE, LOBBY_WAIT, ERROR, STATE_UPDATE, DISCONNECT, and GAME_OVER. Do not add any others.
- Buffer through bytes and split when a newline character is seen. Never assume a recv() call returns just one message.
- recv() that returns 'b""' means that a player closed a connection, so gracefully handle it.
- Catch ConnectionResetError, BrokenPipeError, TimeoutError, and ConnectionAbortedError from any socket calls. Never let an error crash or stop the server.
- Write in simple syntax, readable code with only comments that are necessary. Do not add any features I did not mention.
```

## Prompt 2: Framing and serilization functions
```
-Using system rules, write a module framing.py with these two functions:
1. send_message(sock, message: dict) -> None
   Serialize with json.dumps(message, separators=(",", ":")), append "\n", encode as UTF-8, and finally send with sock.sendall().
2. receive_messages(sock, buffer: bytes) -> tuple[list[dict], bytes, bool]
   Call sock.recv(4096) once and if it returns 'b""', return ([], buffer, True).
   If not, append to buffer, extract every complete line ending in newline (0x0A), parse each with json.loads, and return (messages, leftover_buffer, False). Skip any empty lines and if the leftover buffer exceeds 4096 bytes, raise ValueError("MESSAGE_TOO_LARGE")

Write unit tests using a fake socket that returns preset chunks, covering:
- two messages arriving in a single chunk
- one message split across three chunks
- a chunk that ends halfway through a message, then after the rest of it arrives
- recv() returning 'b""' (EOF)
```

## Prompt 3: Parser and validation functions
```
Using the mentioned system rules, write validate_message(message: dict) -> str | None.
Return None if the message is valid, if not then return exactly one of these error codes: MALFORMED_MESSAGE, UNKNOWN_MESSAGE_TYPE, INVALID_COORDINATES, or INVALID_PLACEMENT.

Rules:
- Missing msg_type, player_id, timestamp, or payload -> MALFORMED_MESSAGE
- Timestamp not an integer, or payload isn't a dict -> MALFORMED_MESSAGE
- msg_type not one of the 9 allowed values -> UNKNOWN_MESSAGE_TYPE
- MOVE: payload must contain row and column values, both Integers (not booleans), each from 0-9 inclusive -> otherwise INVALID_COORDINATES
- PLACE_SHIPS: payload.ships must contain exactly one entry for each of Carrier (5), Battleship (4), Cruiser (3), Submarine (3), and Destroyer (2). Each entry has a name (String), row and column number (integer, 0-9), and orientation ("H" extends right, "V" extends down). Every grid coordinate has to stay on the 10x10 board, with no two ships having a shared grid coordinate -> otherwise INVALID_PLACEMENT

Repeated grid coordinate shots and the player's turn order are checked by the game engine. Write unit tests for every rule, including a MOVE with row = True and PLACE_SHIPS with two overlapping ships, both of these things must be rejected.
```

## Verifying AI output

- Run the framing and validation tests above before trusting any generated code.
- Compare every generated message against the examples inside of 'protocol_blueprint.md', key by key
- Reject any code that adds message types, fields, or third-party libraries not listed inside of the blueprint.