# Flow 3: Agent Execution, LangGraph Loop, Tools, MCP, Skills, Subagents

Purpose: Generate a flowchart for what happens inside one agent run after Gateway starts `run_agent()`.

## Main Actors

- run_agent()
- make_lead_agent()
- create_agent()
- Lead Agent
- LangGraph execution loop
- Middleware chain
- Model
- System prompt
- Tools
- MCP tools
- Skills
- Sandbox
- Subagents
- Checkpointer
- StreamBridge

## Agent Creation Flow

```text
run_agent() starts in background
  -> Builds runtime context with thread_id, run_id, app_config
  -> Adds runtime context to RunnableConfig
  -> Calls agent factory: make_lead_agent(config)
  -> make_lead_agent() calls _make_lead_agent()
  -> _make_lead_agent() resolves model_name and runtime options
  -> create_chat_model() creates model instance from config.yaml
  -> get_available_tools() loads available tools
  -> _build_middlewares() creates middleware chain
  -> apply_prompt_template() creates system prompt
  -> create_agent(model, tools, middleware, system_prompt, state_schema=ThreadState)
  -> run_agent() attaches checkpointer and store
  -> run_agent() calls agent.astream(...)
```

## Agent Runtime Loop

```text
LangGraph starts with ThreadState
  -> Middleware prepares thread data, uploads, sandbox, memory, title, summarization
  -> Model receives system prompt, history, current user message, and tool schemas
  -> Model chooses next action
       -> Option A: direct final answer
       -> Option B: call a tool
       -> Option C: ask clarification
       -> Option D: delegate to subagent using task tool
  -> If tool call:
       -> LangGraph executes selected tool
       -> Tool result becomes ToolMessage
       -> ToolMessage returns to conversation state
       -> Model is called again with updated state
  -> Loop repeats until final answer or interruption
  -> Checkpointer saves thread state
  -> StreamBridge publishes messages, values, updates, errors, and end event
```

## Tool Sources

```text
Config tools from config.yaml
  -> web_search
  -> web_fetch
  -> image_search
  -> ls
  -> glob
  -> grep
  -> read_file
  -> write_file
  -> str_replace
  -> bash if allowed by sandbox security

Built-in tools
  -> present_files
  -> ask_clarification
  -> view_image if model supports vision
  -> task if subagent_enabled is true

MCP tools
  -> Loaded from enabled MCP servers in extensions_config.json
  -> Exposed to the model as normal tools

ACP tools
  -> invoke_acp_agent if ACP agents are configured

Skill management tool
  -> skill_manage if skill evolution is enabled
```

## MCP Role

```text
MCP means Model Context Protocol.
MCP servers expose external capabilities as tools.
DeerFlow loads enabled MCP tools and adds them to the agent tool list.
The model can call MCP tools like local tools.
```

Examples:

```text
GitHub MCP -> issue/repo tools
Database MCP -> query/schema tools
Browser MCP -> browser automation tools
Drive MCP -> document/file tools
```

## Skill Role

```text
Skills are not ordinary executable tools.
Skills are workflow instructions and domain knowledge, usually stored in SKILL.md files.
Skills are injected into the system prompt.
They tell the model how to approach specialized tasks.
```

Difference:

```text
Tool/MCP
  Gives the agent executable actions.

Skill
  Gives the agent methods, workflows, and expert guidance.
```

## Main Agent Possibilities

```text
Direct answer
  -> Model answers without tool calls.

Search and fetch
  -> Model calls web_search or web_fetch, then summarizes.

File work
  -> Model calls ls/glob/grep/read_file/write_file/str_replace.

Artifact generation
  -> Model writes files, then calls present_files.

Clarification
  -> Model calls ask_clarification, middleware interrupts and asks user.

Vision
  -> Model calls view_image when vision is supported.

MCP action
  -> Model calls an external MCP-provided tool.

Subagent delegation
  -> Model calls task to run a specialized subagent.
  -> Subagent returns result.
  -> Lead Agent synthesizes final answer.
```

## Visual Layout Recommendation

Use a central loop diagram:

```text
ThreadState
  -> Middleware
  -> Model
  -> Decision diamond
       -> Final answer
       -> Tool call -> Tool result -> back to Model
       -> Subagent task -> Subagent result -> back to Model
       -> Clarification -> User input needed
  -> Checkpointer
  -> StreamBridge
  -> SSE to Browser
```

Place Skills beside System Prompt, not beside Tools. Place MCP under Tools as one tool source. Place Subagents as a special tool-driven branch.

## Key Labels

- Lead Agent: "Main orchestrator"
- LangGraph: "State machine and execution loop"
- Middleware: "Prepare context and side effects"
- Model: "Chooses answer or tool calls"
- Tools: "Executable capabilities"
- MCP: "External tools via protocol"
- Skills: "Prompt-injected workflows"
- Subagents: "Delegated parallel workers"
- Checkpointer: "Save thread state"
- StreamBridge: "Publish run events"
