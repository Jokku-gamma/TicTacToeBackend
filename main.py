# main.py
import asyncio
from typing import Dict, List, Optional
import json
import uuid
import os
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import firebase_admin
from firebase_admin import credentials, firestore
try:
    service_account_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_KEY_JSON")
    if service_account_json:
        cred = credentials.Certificate(json.loads(service_account_json))
        print("Firebase credentials loaded from environment variable.")
    else:

        cred = credentials.Certificate("firebase-key.json")
        print("Firebase credentials loaded from 'serviceAccountKey.json' file.")

    firebase_admin.initialize_app(cred)
    db = firestore.client()
    print("Firebase Admin SDK initialized successfully.")
except Exception as e:
    print(f"Error initializing Firebase Admin SDK: {e}")
    print("Please ensure 'serviceAccountKey.json' is correctly placed or FIREBASE_SERVICE_ACCOUNT_KEY_JSON env var is set.")
active_connections: Dict[str, Dict[str, WebSocket]] = {}
app = FastAPI()
def check_win(board: List[str], player_symbol: str) -> bool:
    win_conditions = [

        [0, 1, 2], [3, 4, 5], [6, 7, 8],

        [0, 3, 6], [1, 4, 7], [2, 5, 8],

        [0, 4, 8], [2, 4, 6]
    ]
    for condition in win_conditions:
        if all(board[i] == player_symbol for i in condition):
            return True
    return False

def check_draw(board: List[str]) -> bool:
    return all(square != "" for square in board)
@app.websocket("/ws/{room_id}/{player_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str, player_id: str):
    """
    WebSocket endpoint for game communication.
    Handles player connections, disconnections, and game messages.
    """
    print(f"Attempting to connect: Room ID: {room_id}, Player ID: {player_id}")
    await websocket.accept()
    print(f"WebSocket accepted for Player ID: {player_id} in Room ID: {room_id}")
    if room_id not in active_connections:
        active_connections[room_id] = {}
    active_connections[room_id][player_id] = websocket
    print(f"Player {player_id} added to active connections for room {room_id}.")
    room_ref = db.collection("tic_tac_toe_rooms").document(room_id)
    room_doc = await asyncio.to_thread(room_ref.get)

    room_data = {}
    if not room_doc.exists:
        room_data = {
            "board": [""] * 9,
            "player_symbols": {},
            "current_turn": "X",
            "status": "waiting",
            "players_in_room": []
        }
        await asyncio.to_thread(room_ref.set, room_data)
        print(f"Created new room in Firestore: {room_id}")
    else:
        room_data = room_doc.to_dict()
        print(f"Loaded existing room from Firestore: {room_id}")

    player_symbols = room_data.get("player_symbols", {})
    players_in_room = room_data.get("players_in_room", [])

    if player_id not in players_in_room:
        players_in_room.append(player_id)

    if not player_symbols:
        player_symbols[player_id] = "X"
        print(f"Player {player_id} assigned symbol X in room {room_id}")
    elif len(player_symbols) == 1 and player_id not in player_symbols:
        player_symbols[player_id] = "O"
        print(f"Player {player_id} assigned symbol O in room {room_id}")
        room_data["status"] = "playing" 
        print(f"Room {room_id} status changed to 'playing'")
    elif player_id not in player_symbols:
        await websocket.send_json({"type": "error", "message": "Room is full or game in progress."})
        await websocket.close()
        print(f"Player {player_id} denied access to room {room_id}: Room full.")
        if player_id in active_connections[room_id]:
            del active_connections[room_id][player_id]
        if not active_connections[room_id]:
            del active_connections[room_id]
        return
    room_data["player_symbols"] = player_symbols
    room_data["players_in_room"] = players_in_room
    await asyncio.to_thread(room_ref.update, {
        "player_symbols": player_symbols,
        "status": room_data["status"],
        "players_in_room": players_in_room
    })

    await websocket.send_json({
        "type": "game_state",
        "board": room_data["board"],
        "current_turn": room_data["current_turn"],
        "status": room_data["status"],
        "your_symbol": player_symbols.get(player_id),
        "player_count": len(players_in_room), 
        "players_symbols": player_symbols
    })
    await broadcast_game_state(room_id)

    try:
        while True:
            message = await websocket.receive_json()
            print(f"Received message from {player_id} in room {room_id}: {message}")

            message_type = message.get("type")
            room_doc = await asyncio.to_thread(room_ref.get)
            if not room_doc.exists:
                await websocket.send_json({"type": "error", "message": "Game room not found."})
                continue
            room_data = room_doc.to_dict()

            if message_type == "make_move":
                position = message.get("position")
                if not isinstance(position, int) or not (0 <= position < 9):
                    await websocket.send_json({"type": "error", "message": "Invalid move position."})
                    continue
                player_symbol = room_data["player_symbols"].get(player_id)
                if player_symbol != room_data["current_turn"]:
                    await websocket.send_json({"type": "error", "message": "It's not your turn."})
                    continue
                if room_data["status"] != "playing":
                    await websocket.send_json({"type": "error", "message": "Game is not in playing state."})
                    continue
                if room_data["board"][position] != "":
                    await websocket.send_json({"type": "error", "message": "Position already taken."})
                    continue
                room_data["board"][position] = player_symbol

                if check_win(room_data["board"], player_symbol):
                    room_data["status"] = f"{player_symbol}_wins"
                    print(f"Room {room_id}: {player_symbol} wins!")
                elif check_draw(room_data["board"]):
                    room_data["status"] = "draw"
                    print(f"Room {room_id}: Draw!")
                else:
                    room_data["current_turn"] = "O" if room_data["current_turn"] == "X" else "X"

                await asyncio.to_thread(room_ref.update, {
                    "board": room_data["board"],
                    "current_turn": room_data["current_turn"],
                    "status": room_data["status"]
                })
                await broadcast_game_state(room_id)

            elif message_type == "reset_game":
                if room_data["status"] in ["X_wins", "O_wins", "draw"]:
                    room_data["board"] = [""] * 9
                    room_data["current_turn"] = "X"
                    room_data["status"] = "playing" 
                    print(f"Room {room_id}: Game reset initiated by {player_id}")
                    await asyncio.to_thread(room_ref.update, {
                        "board": room_data["board"],
                        "current_turn": room_data["current_turn"],
                        "status": room_data["status"]
                    })
                    await broadcast_game_state(room_id)
                else:
                    await websocket.send_json({"type": "error", "message": "Game must be finished to reset."})

            else:
                await websocket.send_json({"type": "error", "message": "Unknown message type."})

    except WebSocketDisconnect:
        print(f"Player {player_id} disconnected from Room ID: {room_id}")
        if room_id in active_connections and player_id in active_connections[room_id]:
            del active_connections[room_id][player_id]

        room_doc = await asyncio.to_thread(room_ref.get)
        if room_doc.exists:
            room_data = room_doc.to_dict()
            players_in_room = room_data.get("players_in_room", [])
            player_symbols = room_data.get("player_symbols", {})

            if player_id in players_in_room:
                players_in_room.remove(player_id)
            if player_id in player_symbols:
                del player_symbols[player_id]

            if len(players_in_room) < 2:
                room_data["status"] = "waiting"
                room_data["board"] = [""] * 9 
                room_data["current_turn"] = "X" 
                print(f"Room {room_id} now waiting for players due to disconnection.")
            await asyncio.to_thread(room_ref.update({
                "players_in_room": players_in_room,
                "player_symbols": player_symbols,
                "status": room_data["status"],
                "board": room_data["board"],
                "current_turn": room_data["current_turn"]
            }))
        if room_id in active_connections and not active_connections[room_id]:
            del active_connections[room_id]
        else:
            await broadcast_game_state(room_id)

    except Exception as e:
        print(f"An error occurred with player {player_id} in room {room_id}: {e}")
        try:
            await websocket.close()
        except RuntimeError:
            pass 

async def broadcast_game_state(room_id: str):
    room_ref = db.collection("tic_tac_toe_rooms").document(room_id)
    room_doc = await asyncio.to_thread(room_ref.get)

    if not room_doc.exists:
        print(f"Attempted to broadcast to non-existent room in Firestore: {room_id}")
        return

    room_data = room_doc.to_dict()
    message = {
        "type": "game_state",
        "board": room_data["board"],
        "current_turn": room_data["current_turn"],
        "status": room_data["status"],
        "player_count": len(room_data.get("players_in_room", [])), # Get count from Firestore
        "players_symbols": room_data["player_symbols"]
    }
    print(f"Broadcasting state for room {room_id} (from Firestore): {message}")
    if room_id in active_connections:
        for player_id, ws in list(active_connections[room_id].items()):
            try:
                player_specific_message = message.copy()
                player_specific_message["your_symbol"] = room_data["player_symbols"].get(player_id)
                await ws.send_json(player_specific_message)
            except RuntimeError as e:
                print(f"Failed to send to {player_id} in room {room_id}: {e}. Removing player from active connections.")
                if player_id in active_connections[room_id]:
                    del active_connections[room_id][player_id]
                if not active_connections[room_id]:
                    del active_connections[room_id]
@app.get("/")
async def get_root():
    return HTMLResponse("<h1>Tic-Tac-Toe Backend with Firestore</h1><p>WebSocket endpoint: /ws/{room_id}/{player_id}</p>")

@app.get("/rooms")
async def get_rooms():
    rooms_collection = db.collection("tic_tac_toe_rooms")
    docs = await asyncio.to_thread(rooms_collection.stream)
    simplified_rooms = {}
    for doc in docs:
        room_data = doc.to_dict()
        simplified_rooms[doc.id] = {
            "board": room_data.get("board", [""] * 9),
            "current_turn": room_data.get("current_turn", "X"),
            "status": room_data.get("status", "waiting"),
            "player_count": len(room_data.get("players_in_room", [])),
            "player_symbols": room_data.get("player_symbols", {})
        }
    return simplified_rooms