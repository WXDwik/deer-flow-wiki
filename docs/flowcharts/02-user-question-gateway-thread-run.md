# Flow 2: User Question, Gateway Thread And Run Lifecycle

Purpose: Generate a flowchart for what happens after the user is logged in and submits a question in the chat box.

## Main Actors

- Browser chat UI
- Nginx reverse proxy, port 2026
- Gateway API, port 8001
- AuthMiddleware and permission checks
- Thread metadata store
- RunManager
- StreamBridge
- start_run()
- run_agent() background task
- SSE response

## Flow

```text
User submits question in chat box
  -> Frontend builds chat payload with user message
  -> Frontend sends POST /api/threads/{thread_id}/runs/stream
  -> Nginx receives request on port 2026
  -> Nginx forwards /api/* request to Gateway API on port 8001
  -> Gateway AuthMiddleware validates access_token cookie/JWT
  -> Gateway permission layer checks user can access this thread
  -> thread_runs.stream_run() handles the request
  -> stream_run() gets StreamBridge and RunManager from app.state
  -> stream_run() calls start_run(body, thread_id, request)
  -> start_run() creates a RunRecord with new run_id
  -> start_run() binds run_id to thread_id
  -> start_run() creates or updates thread metadata
  -> start_run() normalizes user input into LangChain messages
  -> start_run() builds RunnableConfig with thread_id, model options, and context
  -> start_run() resolves agent factory, usually make_lead_agent
  -> start_run() creates background asyncio task: run_agent(...)
  -> stream_run() immediately returns StreamingResponse
  -> sse_consumer() subscribes to StreamBridge for this run_id
  -> Browser receives streaming SSE events while run_agent() works
```

## Thread And Run Meaning

```text
thread_id
  One conversation/session.
  Holds history, ownership, checkpoint state, uploads, outputs, workspace.

run_id
  One execution inside a thread.
  Created each time the user submits a question or continues the conversation.
```

## Gateway Responsibilities

- Authenticate the user.
- Enforce permissions and thread ownership.
- Create or update thread metadata.
- Create a run record.
- Prevent conflicting runs according to multitask strategy.
- Start agent execution in the background.
- Stream events back to the frontend using SSE.

## Visual Layout Recommendation

Use a sequence diagram style:

```text
Browser
  -> Nginx
  -> Gateway stream_run()
  -> start_run()
  -> RunManager creates RunRecord
  -> ThreadStore updates thread metadata
  -> asyncio task starts run_agent()
  -> StreamBridge publishes events
  -> SSE back to Browser
```

## Key Labels

- `POST /api/threads/{thread_id}/runs/stream`: "User question enters backend"
- `thread_id`: "Conversation container"
- `run_id`: "One agent execution"
- `RunManager`: "Run lifecycle and status"
- `StreamBridge`: "Event channel for SSE"
- `run_agent()`: "Background agent execution"
- `SSE`: "Live updates to browser"
