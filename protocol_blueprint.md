# Application Protocol Blueprint for Battleship
**Author:** Brady Langerman
**Game:** 2 player Battleship over TCP
**Date:** 10-4-2026

## 1. Transport Protocol: TCP/Serialization Format

| Item | Choice |
|-|-|
| Transport Protocol | TCP |
| Serialization format | UTF-8 encoded JSON |
| Framing rule | Newline delimited JSON: every message is terminated by '\n' (byte 0x0A) |
| Max message size | 4096 bytes, including the newline |
| Server identity | Every message sent by the server uses '"player_id":"SERVER"' |

## 2. Framing Rule

### Rule
1. The sender's message is a compact JSON on one line and appends a newline byte at the end ('\n', '0x0A')
2. The raw '0x0A' byte only ever appears at the end of the message, since 'json.dumps' turns any newline inside of a String value into the two character '\' and 'n'. 
3. The receiver keeps a byte buffer for each connection. Every 'recv()' result gets added to the buffer, and whenever the buffer contains '\n', everything before the first '\n' is made as one complete message.
4. Anything after the last '\n' is considered an incomplete message, and therefore stays in the buffer until more bytes arrive.

### Wire Stream Example (three messages that are sent)
```
{"msg_type":"CONNECT","player_id":"Brady","timestamp":1791100000,"payload":{}}\n{"msg_type":"MOVE","player_id":"Brady","timestamp":1791100045,"payload":{"row":3,"col":4}}\n{"msg_type":"DISCONNECT","player_id":"Brady","timestamp":1791100090,"payload":{"reason":"QUIT"}}\n

```
Each '\n' above is a single byte with the value of '0x0A'

### Example of Receiver Extraction (fragmentation and coalescing)

| Step | Bytes returned by 'recv()' | Buffer afterwards | Messages extracted |
|-|-|-|-|
| 1 | '{"msg_type":"MOVE","player_id":"Brady","timest' | '{"msg_type":"MOVE","player_id":"Brady","timest' | no '\n' yet |
| 2 | 'amp":1791100045,"payload":{"row":3,"col":4}}\n{"msg_type":"DISC' | '{"msg_type":"DISC' | the complete MOVE |
| 3 | 'ONNECT","player_id":"Brady","timestamp":1791100090,"payload":{"reason":"QUIT"}}\n' | empty | the complete DISCONNECT |

### Sender and Receiver Logic
```python
import json

def send_message(sock, message):
    line = json.dumps(message, separators=(",", ":")) + "\n"
    sock.sendall(line.encode("utf-8"))

def receive_messages(sock, buffer):
    data = sock.recv(4096)
    if not data:
        return [], buffer, True
    buffer += data
    messages = []
    while b"\n" in buffer:
        line, buffer = buffer.split(b"\n", 1)
        if len(line) > 0:
            messages.append(json.loads(line.decode("utf-8")))
    if len(buffer) > 4096:
        raise ValueError("MESSAGE_TOO_LARGE")
    return messages, buffer, False
```

## 3. JSON keys and data types

Each message contains these 4 keys.

| Key | Type | Definition | Required |
|-|-|-|-|
| 'msg_type' | String | One of 9 possible message types in section 4| yes |
| 'player_id' | String| Sender's alias (1-16 letters, digits, or underscores) or '"SERVER"' | yes |
| 'timestamp' | Integer | Unix time in seconds when message was sent | yes |
| 'payload' | Object | Different fields that are specific to each different message type. '{}' if there are none | yes |

**Server side rules for Validation:**

1. A message isn't in valid JSON format, or is missing a required key, gets 'ERROR' with code 'MALFORMED_MESSAGE', and the connection stays open.
2. An unknown 'msg_type' gets 'ERROR' with code 'UNKNOWN_MESSAGE_TYPE'.
3. A buffer that exceeds the maximum 4096 byte limit without a newline gets 'ERROR' with code 'MESSAGE_TOO_LARGE' , and the buffer is then cleared.
4. Extra keys are ignored and keys are case sensitive.

## 4. Message Types

| # | Message| Purpose | Direction |
|-|-|-|-|
| 1 | 'CONNECT' | Joins the game with an alias | Client -> Server |
| 2 | 'LOBBY_WAIT' | Tells the first player the server is currently waiting for the second player | Server -> Client |
| 3 | 'GAME_START' | Starts the match, assigns roles, and lists all ships to be placed | Server -> Clients |
| 4 | 'PLACE_SHIPS' | Allows players to submit chosen ship positions on their boards | Client -> Server |
| 5 | 'MOVE' | Fires at one grid coordinate on the opponents board | Client -> Server |
| 6 | 'STATE_UPDATE' | Broadcast the scores, boards, and the player who's turn it currently is | Server -> Clients |
| 7 | 'ERROR' | Rejects any malformed, invalid, or out of turn messages | Server -> Client|
| 8 | 'DISCONNECT' | Forfeit/Quit game on purpose | Client -> Server |
| 9 | 'GAME_OVER' | Reveal and announce the winner and final scores | Server -> Clients |

### Connect

**Direction:** Client -> Server
**Purpose:** A client asks to join the room for the game. The alias is sent in 'player_id'.

| Payload field | Type | Meaning |
|-|-|-|
| (none) | | Payload is '{}' |

```json
{"msg_type":"CONNECT","player_id":"Brady","timestamp":1791100000,"payload":{}}
```

If there are already currently two players in the game room, then the server replies 'ERROR' with code 'GAME_FULL' and closes that 3rd connection

### LOBBY_WAIT

**Direction:** Server -> Client (first player only)
**Purpose:** Tells the first player the server is waiting for the second player.

| Payload field | Type | Meaning |
|-|-|-|
| 'players_connected' | Integer | Players currently connected (always 1) |
| 'message' | String | Status text |

```json
{"msg_type":"LOBBY_WAIT","player_id":"SERVER","timestamp":1791100001,"payload":{"players_connected":1,"message":"Waiting for Player 2"}}
```
### GAME_START

**Direction:** Server -> Clients (sent separately to each player)
**Purpose:** Announces that both players are connected, assigns each player a role, and lists the ships for each of the players to place.

| Payload field | Type | Meaning |
|-|-|-|
| 'role' | String | '"PLAYER_1"' or '"PLAYER_2"' |
| 'opponent' | String | Opponent's alias |
| 'board_size' | Integer | Board width and height (10) |
| 'fleet' | array of Objects | Battleships to place down |
| 'fleet[].name' | String | Ship name |
| 'fleet[].size' | Integer | Number of grid coordinate's the ship covers |

```json
{"msg_type":"GAME_START","player_id":"SERVER","timestamp":1791100030,"payload":{"role":"PLAYER_1","opponent":"Alex","board_size":10,"fleet":[{"name":"Carrier","size":5},{"name":"Battleship","size":4},{"name":"Cruiser","size":3},{"name":"Submarine","size":3},{"name":"Destroyer","size":2}]}}
```
### PLACE_SHIPS

**Direction:** Client -> Server
**Purpose:** Player submits where they want all of their 5 battleships to be placed. There is no time limit on placement.

| Payload field | Type | Meaning |
|-|-|-|
| 'ships' | array of Objects | Only 1 entry per ship |
| 'ships[].name' | String | Ship name from 'GAME_START' |
| 'ships[].row' | Integer (0-9) | Row of ship's first grid coordinate |
| 'ships[].col' | Integer (0-9) | Column of ship's first grid coordinate |
| 'ships[].orientation' | String | '"H"' extends right or '"V"' extends down |

```json
{"msg_type":"PLACE_SHIPS","player_id":"Brady","timestamp":1791100040,"payload":{"ships":[{"name":"Carrier","row":0,"col":0,"orientation":"H"},{"name":"Battleship","row":2,"col":1,"orientation":"V"},{"name":"Cruiser","row":7,"col":3,"orientation":"H"},{"name":"Submarine","row":4,"col":8,"orientation":"V"},{"name":"Destroyer","row":9,"col":6,"orientation":"H"}]}}
```

'ERROR' with code 'INVALID_PLACEMENT' is returned if a player places a missing/repeated ship, grid coordinate off the board, or overlapping ships. 

### MOVE

**Direction:** Client -> Server
**Purpose:** The current active player fires at one grid coordinate at the opponent's board.

| Payload field | Type | Meaning |
|-|-|-|
| 'row' | Integer (0-9) | Target row |
| 'col' | Integer (0-9) | Target column |

```json
{"msg_type":"MOVE","player_id":"Brady","timestamp":1791100045,"payload":{"row":3,"col":4}}
```

If it isn't a players turn and they MOVE, 'NOT_YOUR_TURN' is returned. Any grid coordinates outside 0-9 get 'INVALID_COORDINATES'. If a grid coordinate selected was already fired upon, 'ALREADY_FIRED' is returned. The turn doesn't change after an error. 

### STATE_UPDATE

**Direction:** Server -> Clients (broadcasted to both)
**Purpose:** Broadcasts the board state, whose turn it is, and scores to both players.

| Payload field | Type | Meaning |
|-|-|-|
| 'phase' | String | '"PLACEMENT"' |
| 'ready' | Object | Whether each player has placed all of their battleships |

```json
{"msg_type":"STATE_UPDATE","player_id":"SERVER","timestamp":1791100041,"payload":{"phase":"PLACEMENT","ready":{"PLAYER_1":true,"PLAYER_2":false}}}
```

Battle phase (sent after both players place down all battleships and also after every valid shot/move)

| Payload field | Type | Meaning |
|-|-|-|
| 'phase' | String | '"BATTLE"' |
| 'last_shot' | Object or null | 'null' in the first battle, otherwise the shot just fired |
| 'last_shot.by' | String | Role that fired |
| 'last_shot.row' | Integer | Row fired at |
| 'last_shot.col' | Integer | Column fired at |
| 'last_shot.result' | String | '"HIT"', '"MISS"', OR '"SUNK"' |
| 'last_shot.ship_sunk' | String or null | Name of the ship that was sunk, otherwise 'null' |
| 'turn' | String | Role of whose turn is next |
| 'scores' | Object | Hit landed by each role |
| 'ships_remaining' | Object | ships still alive for each role |
| 'boards' | Object | For each role, 10 row strings of shots fired at board: '.' not fired, 'M' miss, 'H' hit. Battleship positions are never exposed |

```json
{"msg_type":"STATE_UPDATE","player_id":"SERVER","timestamp":1791100046,"payload":{"phase":"BATTLE","last_shot":{"by":"PLAYER_1","row":3,"col":4,"result":"HIT","ship_sunk":null},"turn":"PLAYER_1","scores":{"PLAYER_1":1,"PLAYER_2":0},"ships_remaining":{"PLAYER_1":5,"PLAYER_2":5},"boards":{"PLAYER_1":["..........","..........","..........","..........","..........","..........","..........","..........","..........",".........."],"PLAYER_2":["..........","..........","..........","....H.....","..........","..........","..........","..........","..........",".........."]}}}
```

After a 'HIT' or 'SUNK', the same player that just fired, gets to fire again. After a 'MISS', the turn passes to the opponent/next player.

### ERROR

**Direction:** Server -> Client (Client that caused it)
**Purpose:** Rejects any bad message without crashing the entire server or changing the state of the game.

| Payload field | Type | Meaning |
|-|-|-|
| 'code' | String | One code that is below |
| 'detail' | String | Explanation text |

| Code | Cause |
|-|-|
| 'NOT_YOUR_TURN' | MOVE sent by the player who isn't active | 
| 'INVALID_COORDINATES' | 'row' or 'col' missing, not an Integer, or outside 0-9 inclusive |
| 'ALREADY_FIRED' | MOVE targets a grid coordinate already fired at |
| 'INVALID_PLACEMENT' | Battleships missing, repeated, off the grid board, or overlapping |
| 'WRONG_PHASE' | MOVE during placement, or PLACE_SHIPS during battle phase |
| 'MALFORMED_MESSAGE' | not valid JSON format, or a required key is missing |
| 'UNKNOWN_MESSAGE_TYPE' | 'msg_type' isn't one of the 9 specified types |
| 'MESSAGE_TOO_LARGE' | More than the maximum 4096 bytes arrived without a newline '0x0A' |
| 'GAME_FULL' | CONNECT received while two players are already connected to the game room |

```json
{"msg_type":"ERROR","player_id":"SERVER","timestamp":1791100050,"payload":{"code":"NOT_YOUR_TURN","detail":"It is PLAYER_2's turn"}}
```

### DISCONNECT

**Direction:** Client -> Server
**Purpose:** A player quits on purpose. This results in a forfeit if the game is running.

| Payload field | Type | Meaning |
|-|-|-|
| 'reason' | String | 'QUIT' |

```json
{"msg_type":"DISCONNECT","player_id":"Brady","timestamp":1791100090,"payload":{"reason":"QUIT"}}
```

### GAME_OVER

**Direction:** Server -> Clients (both players that are still connected)
**Purpose:** Announces the result of the game and final scores.

| Payload field | Type | Meaning |
|-|-|-|
| 'outcome' | String | '"WIN"' or '"FORFEIT"' |
| 'winner' | String | Winning player |
| 'reason' | String | Explanation text |
| 'final_scores' | Object | Hits landed by each player |

```json
{"msg_type":"GAME_OVER","player_id":"SERVER","timestamp":1791100300,"payload":{"outcome":"WIN","winner":"PLAYER_1","reason":"ALL PLAYER_2 ships sunk","final_scores":{"PLAYER_1":17,"PLAYER_2":9}}}
```

'WIN' means that one of the players hit all 17 enemy battleships and sunk them. 'FORFEIT' means a player disconnected after the game started and since battleship can't end in a draw, 'DRAW' is never sent. 

## 5. Connection Termination

### Application disconnect that's graceful

1. The client sends 'DISCONNECT' to the server and closes their socket with 'sock.close()'
2. Closing the socket results in starting the TCP four-way FIN handshake. 
3. Server sends 'GAME_OVER' with outcome 'FORFEIT' to the opponent. Closes the forfeiting player's socket and frees any resources that were being used.

### Transport Layer is closed without a DISCONNECT message

If the client process exits without sending a 'DISCONNECT' message, then the operating system still sends a TCP FIN. For the server, 'recv()' returns 'b""' (0 bytes) instead of raising an exception. 'b""' indicates a closed connection (EOF) from the other side. The receive loop checks for 'b""'. 'recv()' continues to return 'b""' immediately on every call if it doesn't and the loop will run forever at maximum CPU capacity.

```python
data = sock.recv(4096)
if not data:
    sock.close()
    handle_client_disconnect(player_id)
```


### Random termination (crashes, network drops)

Every send and receive message is wrapped so that the server can detect any dead connections and trigger a state change, instead of crashing the entire program.

| Exception | What triggers it| 
|-|-| 
| 'ConnectionResetError' | Client forcibly closed the connection | 
| 'BrokenPipeError' | A server's 'send()' fails because a socket's end is already closed | 
| 'ConnectionAbortedError' | Operating system aborted the connection | 
| 'TimeoutError' | Ship placement at the start of the game has no timeout. Any other message that doesn't arrive within 120 second from a turn window results in this exception | 

```python
try:
    messages,buffer,closed = receive_messages(sock, buffer)
    if closed:
        trigger_state_transition("CLIENT_DISCONNECTED", player_id)
except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, TimeoutError) as e:
    logger.warning(f'Connection lost abruptly: {e}')
    trigger_state_transition("CLIENT_DISCONNECTED", player_id)
```