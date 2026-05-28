# Flow 1: Browser Access, Nginx Routing, Frontend And Gateway

Purpose: Generate a flowchart for the first stage only: opening `http://localhost:2026` and reaching the UI/auth state.

## Main Actors

- Browser
- Nginx reverse proxy, public port 2026
- Frontend Next.js service, internal port 3000
- Gateway API FastAPI service, internal port 8001
- Auth middleware
- Auth router

## Flow

```text
User enters http://localhost:2026 in Browser
  -> Browser sends GET /
  -> Nginx receives request on port 2026
  -> Nginx checks request path
  -> Path is "/" or frontend asset path
  -> Nginx forwards request to Frontend service on port 3000
  -> Frontend renders DeerFlow page
  -> Frontend needs session state
  -> Frontend sends auth check request such as /api/v1/auth/me or /api/v1/auth/setup-status
  -> Nginx receives /api/* request on port 2026
  -> Nginx checks request path
  -> Path starts with /api/
  -> Nginx forwards request to Gateway API on port 8001
  -> Gateway AuthMiddleware checks whether path is public or requires session
  -> Auth router or middleware returns auth/setup status
  -> Nginx forwards response back to Browser
  -> Browser shows one of: setup page, login page, or workspace
```

## Routing Rules To Show

```text
Nginx :2026
  "/" and frontend assets -> Frontend :3000
  "/api/*"                -> Gateway API :8001
```

## Important Clarification

Nginx does not create threads, runs, users, or agent executions. It only receives network traffic and forwards it to the correct internal service.

## Visual Layout Recommendation

Use a branching diagram from Nginx:

```text
Browser
  -> Nginx :2026
       -> Frontend :3000 for page requests
       -> Gateway API :8001 for /api/* requests
```

Add a return arrow from Gateway/Frontend back through Nginx to Browser.

## Key Labels

- Browser: "User entry point"
- Nginx: "Unified public port and reverse proxy"
- Frontend: "Render UI"
- Gateway API: "Auth and backend API"
- AuthMiddleware: "Validate session cookie/JWT"
- Result: "Setup, Login, or Workspace"
