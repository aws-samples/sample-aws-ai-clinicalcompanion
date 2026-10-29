# ClinicalCompass Frontend

Chat-based clinical decision support UI that communicates with the backend via WebSocket streaming using the AG-UI (Agent User Interaction Protocol) standard.

## How it works

The frontend is plain HTML/CSS/JS — no framework, no build step. It connects to the deployed AWS WebSocket API Gateway, which routes messages through Lambda to the AgentCore multi-agent backend. All streaming display is driven by AG-UI events.

```
Browser (localhost:8080)
  └── WebSocket connection
        └── API Gateway (wss://...)
              └── Lambda (query handler — AG-UI event router)
                    └── AgentCore (Event Emitter → Supervisor Agent → 3 specialist agents)
```

When a query is submitted:
1. User message appears as a right-aligned bubble
2. An AG-UI run starts — progress steps appear as they execute
3. Tool call badges show which knowledge bases are being consulted
4. Reasoning content appears in a collapsible section
5. The final recommendation streams in progressively, rendered as Markdown
6. Session ID is saved for follow-up queries

## File Structure

```
frontend/
├── index.html                          # Single page — header, chat area, input bar
├── config.js                           # WebSocket URL + connection settings (gitignored)
├── config.example.js                   # Config template
├── css/
│   ├── styles.css                      # Base styles, header, buttons, responsive
│   ├── panel.css                       # Chat layout, bubbles, input bar
│   ├── agui.css                        # AG-UI progress display styles
│   └── auth.css                        # Authentication screen styles
├── js/
│   ├── app.js                          # Main app logic — submit, AG-UI rendering
│   └── auth.js                         # Authentication handling
└── lib/
    ├── AGUIEventHandler.js             # AG-UI event dispatcher and run state management
    └── ClinicalCompassWebSocket.js     # WebSocket client — connect, reconnect, ping, AG-UI routing
```

## Quick Start

The frontend config is automatically created by `post_cdk_setup.py`. If you need to set it up manually:

```bash
cp config.example.js config.js
# Edit config.js — set websocketUrl to your WebSocket API URL
```

Run locally:

```bash
python3 -m http.server 8080
# Open http://localhost:8080
```

## Configuration

`config.js` (created automatically by `post_cdk_setup.py`):

```javascript
const config = {
  websocketUrl: 'wss://YOUR_API_ID.execute-api.us-east-1.amazonaws.com/production',
  useWebSocket: true,
  maxReconnectAttempts: 5,
  reconnectDelay: 1000,
  pingIntervalMs: 30000,
  autoScroll: true,
  showTimestamps: true,
  maxResponseLength: 50000
};
```

## Key Components

### ClinicalCompassWebSocket (`lib/ClinicalCompassWebSocket.js`)

WebSocket client class that handles:
- Connection lifecycle (connect, disconnect, auto-reconnect with exponential backoff)
- Keep-alive pings every 30 seconds
- AG-UI event detection and routing to AGUIEventHandler
- Callbacks: `onConnectionChange`, `onError`

### AGUIEventHandler (`lib/AGUIEventHandler.js`)

Lightweight event dispatcher that:
- Maintains local run state (steps, tool calls, text buffer, reasoning, status)
- Dispatches AG-UI events to registered UI callbacks
- Handles unknown event types gracefully (log, don't crash)

### app.js

Main application logic:
- Creates the WebSocket client on page load
- Registers AG-UI event callbacks for all event types
- Handles form submission (Ctrl/Cmd+Enter or click)
- Renders AG-UI events: steps with spinners/checkmarks, tool badges, collapsible reasoning, streamed markdown text
- Manages session ID for follow-up queries
- Implements 120s timeout warning if no RUN_FINISHED received

## AG-UI Event Types

| Event | UI Rendering |
|-------|-------------|
| `RUN_STARTED` | Create progress bubble, disable input |
| `STEP_STARTED` | Add step with spinner |
| `STEP_FINISHED` | Mark step complete (✓) or failed (✗) |
| `TOOL_CALL_START` | Show tool badge under current step |
| `TOOL_CALL_END` | Mark tool badge as complete |
| `REASONING_START` | Open collapsible reasoning section |
| `REASONING_MESSAGE_CONTENT` | Append reasoning text |
| `REASONING_END` | Collapse reasoning section |
| `TEXT_MESSAGE_START` | Begin streaming cursor |
| `TEXT_MESSAGE_CONTENT` | Render accumulated markdown |
| `TEXT_MESSAGE_END` | Finalize recommendation |
| `RUN_FINISHED` | Re-enable input, show completion |
| `RUN_ERROR` | Display error inline at failed step |

## Usage

1. Select a patient from the list (e.g., `001`, Jean-Pierre Dubois)
2. Type a clinical question, for example:
   - "68-year-old male with eGFR 32, needs MRI with gadolinium contrast"
   - "What are the safety considerations for contrast imaging?"
3. Send with the button or Ctrl/Cmd+Enter
4. Watch the AG-UI progress steps execute in real time
5. Follow-up questions maintain session context automatically

## Connection Status

The header shows a colored dot:
- Green: Connected
- Red/pulsing: Disconnected (auto-reconnecting)

## Troubleshooting

- "Disconnected" status: Check `config.js` has the correct WebSocket URL
- No response: Check browser console (F12) for errors, verify backend is deployed
- Garbled output: Clear browser cache (Ctrl+Shift+R)
- Connection drops: Check network stability, the client auto-reconnects up to 5 times

## Browser Compatibility

Works on Chrome 90+, Firefox 88+, Safari 14+, Edge 90+.

## Development

Frontend changes don't require any deployment — just edit the files and refresh the browser. The only external dependency is `marked.js` (loaded from CDN).

To modify the WebSocket protocol, update both `lib/ClinicalCompassWebSocket.js` and the corresponding Lambda handler in `backend/lambda/websocket/`.

### Running Tests

```bash
node lib/ClinicalCompassWebSocket.test.js
```
