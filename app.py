import os
import json
import base64
import asyncio
import websockets
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
from dotenv import load_dotenv
from load_menu import load_menu_from_all_endpoint
from tools import OrderManager
import time


load_dotenv()

MENU = load_menu_from_all_endpoint()
order_manager = OrderManager(MENU)


# Configuration
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
PORT = int(os.getenv('PORT', 5050))
TEMPERATURE = float(os.getenv('TEMPERATURE', 0.8))
SYSTEM_MESSAGE = (
    "You are a restaurant ordering assistant. "
    "Never invent menu items, prices, modifiers or details. "
    "Always use tools to fetch or validate menu information. "
    "The menu contains 15 categories: 'Bagels & Cream Cheese', 'Breakfast', 'All American Sandwiches', 'Specialty Sandwiches', "
    "'Chicken Sandwiches', 'Panini & Wraps', 'Salads & Veggie', 'Soups, Sweets, Chips & Dog Bones', 'Bottled & Canned Drinks', "
    "'Sides', 'Catering', 'Bulk items', 'Hot Drinks', 'Cold Drinks', 'Pastry'. "
    "If the user request clearly maps to a single category, call get_items_by_category once with that category. "
    "Call get_items_by_category with multiple categories when the request is ambiguous or spans more than one category. "
    "If asked about a drink, check all three drinks categories: 'Hot Drinks', 'Cold Drinks' and 'Bottled & Canned Drinks'. "
    "Only say three options for items or modifiers."
)
VOICE = 'marin'
LOG_EVENT_TYPES = [
    'error', 'response.content.done', 'rate_limits.updated',
    'response.done', 'input_audio_buffer.committed',
    'input_audio_buffer.speech_stopped', 'input_audio_buffer.speech_started',
    'session.created', 'session.updated'
]
SHOW_TIMING_MATH = False

TOOL_REGISTRY = {
    "get_items_by_category": order_manager.get_items_by_category,
    "get_item_details_by_id": order_manager.get_item_details_by_id,
    "get_modifier_groups": order_manager.get_modifier_groups,
    "add_item_to_order": order_manager.add_item_to_order,
    "get_current_order": order_manager.get_current_order,
    "remove_item_from_order": order_manager.remove_item_from_order,
    "collect_client_info": order_manager.collect_client_info,
    "confirm_order": order_manager.confirm_order
}

app = FastAPI()

class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            await connection.send_json(message)

# Instantiate one global manager
manager = ConnectionManager()

if not OPENAI_API_KEY:
    raise ValueError('Missing the OpenAI API key. Please set it in the .env file.')

@app.get("/", response_class=JSONResponse)
async def index_page():
    return {"message": "Twilio Media Stream Server is running!"}

@app.get("/dashboard", response_class=HTMLResponse)
async def get_dashboard():
    return FileResponse("dashboard.html")

@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    """Handle incoming call and return TwiML response to connect to Media Stream."""
    form_data = await request.form()
    caller_number = form_data.get("From")

    response = VoiceResponse()
    host = request.url.hostname

    connect = Connect()
    stream = Stream(url=f'wss://{host}/media-stream')
    stream.parameter(name="phone_number", value=caller_number)

    connect.append(stream)
    response.append(connect)
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.websocket("/dashboard")
async def dashboard_ws(websocket: WebSocket):
    """WebSocket endpoint for dashboard clients to receive live chat."""
    await manager.connect(websocket)
    try:
        while True:
            # keep the connection alive
            await websocket.receive_text()
    except:
        manager.disconnect(websocket)

@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    """Handle WebSocket connections between Twilio and OpenAI."""
    await websocket.accept()
    print("Client connected")

    async with websockets.connect(
        f"wss://api.openai.com/v1/realtime?model=gpt-realtime-mini-2025-12-15&temperature={TEMPERATURE}",
        additional_headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}"
        }
    ) as openai_ws:
        await initialize_session(openai_ws)

        # Connection specific state
        stream_sid = None
        latest_media_timestamp = 0
        last_assistant_item = None
        mark_queue = []
        response_start_timestamp_twilio = None
        
        async def receive_from_twilio():
            """Receive audio data from Twilio and send it to the OpenAI Realtime API."""
            nonlocal stream_sid, latest_media_timestamp
            try:
                async for message in websocket.iter_text():
                    data = json.loads(message)
                    if data['event'] == 'media' and openai_ws.state.name == 'OPEN':
                        latest_media_timestamp = int(data['media']['timestamp'])
                        audio_append = {
                            "type": "input_audio_buffer.append",
                            "audio": data['media']['payload']
                        }
                        await openai_ws.send(json.dumps(audio_append))
                    elif data['event'] == 'start':

                        stream_sid = data['start']['streamSid']
                        phone_number = data["start"]["customParameters"].get("phone_number")
                        #print(f"Incoming stream has started {stream_sid}")
                        order_manager.create_new_order(stream_sid, phone_number)
                        response_start_timestamp_twilio = None
                        latest_media_timestamp = 0
                        last_assistant_item = None
                    elif data['event'] == 'mark':
                        if mark_queue:
                            mark_queue.pop(0)
            except WebSocketDisconnect:
                print("Client disconnected.")

                if stream_sid:
                    order_manager.delete_order(stream_sid)

                if openai_ws.state.name == 'OPEN':
                    await openai_ws.close()

        async def send_to_twilio():
            """Receive events from the OpenAI Realtime API, send audio back to Twilio."""
            nonlocal stream_sid, last_assistant_item, response_start_timestamp_twilio
            try:
                async for openai_message in openai_ws:
                    response = json.loads(openai_message)
                    if response['type'] in LOG_EVENT_TYPES:
                        print(f"Received event: {response['type']}", response)

                    if response.get("type") == "conversation.item.input_audio_transcription.completed":
                        user_text = response.get("transcript", "").strip()

                        if user_text:
                            await manager.broadcast({"role": "user", "text": user_text})


                    if response.get("type") == "response.output_audio_transcript.done":
                        assistant_text = response.get("transcript", "").strip()

                        if assistant_text:
                            await manager.broadcast({"role": "assistant", "text": assistant_text})


                    if response["type"] == "response.done":
                        outputs = response["response"]["output"]

                        if outputs and outputs[0]["type"] == "function_call":
                            tool_call = outputs[0]
                            tool_name = tool_call["name"]
                            call_id = tool_call["call_id"]

                            try:
                                tool_args = json.loads(tool_call.get("arguments", "{}"))
                            except json.JSONDecodeError:
                                tool_args = {}

                            tool_args["stream_sid"] = stream_sid

                            print(f"🛠 Tool call: {tool_name} {tool_args}")

                            if tool_name in TOOL_REGISTRY:
                                try:
                                    result = TOOL_REGISTRY[tool_name](**tool_args)
                                except Exception as e:
                                    result = {"error": str(e)}
                            else:
                                result = {"error": f"Unknown tool: {tool_name}"}

                            print(f"Result: {result}\n")

                            # Send tool result back to OpenAI
                            await openai_ws.send(json.dumps({
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "function_call_output",
                                    "call_id": call_id,
                                    "output": json.dumps(result)
                                }
                            }))

                            # Ask model to continue (this time with audio)
                            await openai_ws.send(json.dumps({
                                "type": "response.create",
                                "response": {
                                    "output_modalities": ["audio"]
                                }
                            }))


                    if response.get('type') == 'response.output_audio.delta' and 'delta' in response:
                        audio_payload = base64.b64encode(base64.b64decode(response['delta'])).decode('utf-8')
                        audio_delta = {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {
                                "payload": audio_payload
                            }
                        }
                        await websocket.send_json(audio_delta)


                        if response.get("item_id") and response["item_id"] != last_assistant_item:
                            response_start_timestamp_twilio = latest_media_timestamp
                            last_assistant_item = response["item_id"]
                            #if SHOW_TIMING_MATH:
                                #print(f"Setting start timestamp for new response: {response_start_timestamp_twilio}ms")

                        await send_mark(websocket, stream_sid)

                    # Trigger an interruption. Your use case might work better using `input_audio_buffer.speech_stopped`, or combining the two.
                    if response.get('type') == 'input_audio_buffer.speech_started':
                        #print("Speech started detected.")
                        if last_assistant_item:
                            #print(f"Interrupting response with id: {last_assistant_item}")
                            await handle_speech_started_event()
            except Exception as e:
                print(f"Error in send_to_twilio: {e}")

        async def handle_speech_started_event():
            """Handle interruption when the caller's speech starts."""
            nonlocal response_start_timestamp_twilio, last_assistant_item
            #print("Handling speech started event.")
            if mark_queue and response_start_timestamp_twilio is not None:
                elapsed_time = latest_media_timestamp - response_start_timestamp_twilio
                #if SHOW_TIMING_MATH:
                    #print(f"Calculating elapsed time for truncation: {latest_media_timestamp} - {response_start_timestamp_twilio} = {elapsed_time}ms")

                if last_assistant_item:
                    #if SHOW_TIMING_MATH:
                        #print(f"Truncating item with ID: {last_assistant_item}, Truncated at: {elapsed_time}ms")

                    truncate_event = {
                        "type": "conversation.item.truncate",
                        "item_id": last_assistant_item,
                        "content_index": 0,
                        "audio_end_ms": elapsed_time
                    }
                    await openai_ws.send(json.dumps(truncate_event))

                await websocket.send_json({
                    "event": "clear",
                    "streamSid": stream_sid
                })

                mark_queue.clear()
                last_assistant_item = None
                response_start_timestamp_twilio = None

        async def send_mark(connection, stream_sid):
            if stream_sid:
                mark_event = {
                    "event": "mark",
                    "streamSid": stream_sid,
                    "mark": {"name": "responsePart"}
                }
                await connection.send_json(mark_event)
                mark_queue.append('responsePart')

        await asyncio.gather(receive_from_twilio(), send_to_twilio())

async def send_initial_conversation_item(openai_ws):
    """Send initial conversation item if AI talks first."""
    initial_conversation_item = {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": "Greet the user with 'Welcome to the great american bagel, How can i help you ?'"
                }
            ]
        }
    }
    await openai_ws.send(json.dumps(initial_conversation_item))
    await openai_ws.send(json.dumps({"type": "response.create"}))


async def initialize_session(openai_ws):
    """Control initial session with OpenAI."""
    session_update = {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcmu"},
                    "turn_detection": {"type": "server_vad"},
                    "transcription": {    
                        "model": "gpt-4o-mini-transcribe-2025-12-15",
                        "language": "en"
                    }
                },
                "output": {
                    "format": {"type": "audio/pcmu"},
                    "voice": VOICE
                }
            },
            "instructions": SYSTEM_MESSAGE,
            "tools": [
                {
                    "type": "function",
                    "name": "get_items_by_category",
                    "description": "Get items for up to three categories",
                    "parameters": {
                        "type": "object",
                        "properties": {
                        "category1": { "type": "string" },
                        "category2": { "type": "string" },
                        "category3": { "type": "string" }
                        },
                        "required": ["category1"]
                    }
                },
                {
                    "type": "function",
                    "name": "get_item_details_by_id",
                    "description": "Get details for a specific item id",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "id": { 
                                "type": "string",
                                "description": "Exact item id"
                            }
                        },
                        "required": ["id"]
                    }
                },
                {
                    "type": "function",
                    "name": "get_modifier_groups",
                    "description": "Get modifiers groups for a specific item id",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "item_id": { 
                                "type": "string",
                                "description": "Exact item id"
                            }
                        },
                        "required": ["item_id"]
                    }
                },
                {
                    "type": "function",
                    "name": "add_item_to_order",
                    "description": "Add an item with optional modifiers to the current order",
                    "parameters": {
                        "type": "object",
                        "properties": {
                        "item_id": {
                            "type": "string",
                            "description": "Menu item ID"
                        },
                        "quantity": {
                            "type": "integer",
                            "minimum": 1
                        },
                        "modifiers": {
                            "type": "array",
                            "items": {
                            "type": "object",
                            "properties": {
                                "modifier_group_id": { "type": "string" },
                                "modifier_id": { "type": "string" }
                            },
                            "required": ["modifier_group_id", "modifier_id"]
                            }
                        }
                        },
                        "required": ["item_id", "quantity"]
                    }
                },
                {
                    "type": "function",
                    "name": "get_current_order",
                    "description": (
                        "Retrieve the customer's current order. "
                        "Use this whenever you need to confirm items, summarize the order, "
                        "identify items to modify or remove. "
                        "Always call this tool instead of relying on memory."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": []
                    }
                },
                {
                    "type": "function",
                    "name": "remove_item_from_order",
                    "description": (
                        "Remove a specific item from the customer's order using its line_id. "
                        "You MUST call get_current_order first to obtain the correct line_id "
                        "before calling this tool. Never guess or invent line_ids."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "line_id": {
                                "type": "string",
                                "description": "The unique identifier of the order line to remove."
                            }
                        },
                        "required": ["line_id"]
                    }
                },
                {
                    "type": "function",
                    "name": "collect_client_info",
                    "description": (
                        "Collect the customer’s information for the order. "
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "full_name": {
                                "type": "string",
                                "description": "The full name of the customer for the order."
                            }
                        },
                        "required": ["full_name"]
                    }
                },
                {
                    "type": "function",
                    "name": "confirm_order",
                    "description": (
                        "Finalize the order and freeze it. "
                        "Before calling this, ensure the order has at least one item and "
                        "client information has been collected via collect_client_info. "
                        "The function takes no arguments."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": []
                    }
                }
            ]
        }
    }
    #print('Sending session update:', json.dumps(session_update))
    await openai_ws.send(json.dumps(session_update))

    # Uncomment the next line to have the AI speak first
    await send_initial_conversation_item(openai_ws)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)