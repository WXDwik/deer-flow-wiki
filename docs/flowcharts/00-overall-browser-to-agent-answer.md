# Overall Flow: Browser To Agent Answer

Purpose: Generate one high-level architecture flowchart showing the full path from opening DeerFlow in a browser to receiving an agent answer.

## Main Actors

- Browser
- Nginx reverse proxy, port 2026
- Frontend Next.js service, port 3000
- Gateway API FastAPI service, port 8001
- Gateway runtime components
- Lead Agent
- LangGraph execution loop
- Model
- Tools
- Subagents
- SSE stream back to Browser

## Flow

```text
Browser opens http://localhost:2026
  -> Nginx receives request on port 2026
  -> Nginx routes page request "/" to Frontend service on port 3000
  -> Frontend renders login/setup/workspace UI
  -> Frontend calls /api/v1/auth/* to check session
  -> Nginx routes /api/* request to Gateway API on port 8001
  -> Gateway authenticates user and returns session state
  -> Browser shows workspace
  -> User submits a question in the chat box
  -> Frontend sends POST /api/threads/{thread_id}/runs/stream
  -> Nginx routes /api/* request to Gateway API
  -> Gateway validates auth and permissions
  -> Gateway creates or updates thread metadata
  -> Gateway creates a run record
  -> Gateway starts run_agent() in the background
  -> run_agent() creates Lead Agent with model, tools, middleware, and system prompt
  -> LangGraph runs the agent loop
  -> Model may answer directly or call tools/subagents
  -> Tool and subagent results return to the agent context
  -> Agent produces final answer
  -> Gateway streams events through SSE
  -> Nginx forwards SSE stream to Browser
  -> Browser displays streaming answer and final result
```

## Visual Layout Recommendation

Use a left-to-right diagram with four horizontal zones:

```text
Client Zone
  Browser

Proxy Zone
  Nginx :2026

Application Zone
  Frontend :3000
  Gateway API :8001
  Gateway runtime components

Agent Zone
  Lead Agent
  LangGraph loop
  Model
  Tools
  Subagents
```

Show Nginx as the single public entry point. Show Gateway API as the owner of thread/run lifecycle. Show Lead Agent as the owner of reasoning and tool selection. Show SSE as a return arrow from Gateway to Browser.

## Key Labels

- Nginx: "Reverse proxy and path router"
- Frontend: "UI rendering and chat submission"
- Gateway API: "Auth, thread/run lifecycle, SSE"
- Lead Agent: "Model + tools + middleware + prompt"
- LangGraph: "Stateful agent execution loop"
- Tools: "Search, file, sandbox, MCP, artifacts"
- Subagents: "Parallel delegated tasks"
- SSE: "Streaming events back to UI"
