# NOVA — General-Purpose AI Browser Agent with Human-in-the-Loop Approval

NOVA takes a natural-language instruction ("Open YouTube and play Arijit Singh songs", "Compare laptops under ₹60,000",
"Find the cheapest flight from Bengaluru to Delhi on Friday") and carries it out in a **real, visible Chromium browser**:
it navigates, clicks, types, scrolls, extracts and compares data. It **pauses for your explicit approval** before
anything sensitive or irreversible.

> *AI performs the work. Humans retain control over important decisions.*

There are **no per-website workflows**. A single generic agent loop decides every step from a live observation of the
page, built from the DOM and accessibility information, with a screenshot when needed.

### Two ways to start a task

1. **Task launcher (home screen).** The dashboard opens on a gallery of what NOVA can do: play music/video, search
   flights, hotels, shop & compare, find jobs, research papers, web search, draft an email, fill a form, find
   appointments, download a document. Pick one and NOVA asks for the details it needs (which song; from, to and date;
   product and budget; role, location and experience...). *Start task* stays disabled until the required fields are
   filled, and the backend re-validates them (e.g. no past dates). The answers reach the agent as authoritative
   `USER DATA`, so it never guesses the essentials.
2. **Free text in the chat**, e.g. "Find the cheapest flights from Bengaluru to Delhi this Friday". If something
   essential is missing, NOVA asks a clarification question before opening the browser.

### Selecting from the results

When NOVA collects options (songs, flights, hotels, products, jobs, papers), they appear in the **Results** panel,
each verified against the page it came from. Click **Select ▸** on the one you want and NOVA opens it in the same
browser window: plays that video, opens that product or flight, or opens that job listing. In "let me choose" mode
(the default for music) NOVA stops and waits until you click **Choose this**. Each result's link is taken from the
page's own element, never from text the model wrote, so a page cannot slip in a fake link. Buying, booking,
applying and sending still require your approval.

The launcher entries live in `backend/agent/capabilities.py`. Each one only turns form answers into a precise
instruction; the same generic agent executes all of them.

---

## Architecture

```
React dashboard ──REST──► FastAPI ──► LangGraph agent ──► Playwright (visible Chromium)
      ▲                                    │
      └──────── WebSocket (snapshots, live screenshots, logs) ◄──┘

LangGraph workflow (state checkpointed in memory per task):

 understand ─► clarify? (interrupt) ─► init_browser ─► observe ─► decide ─┬─► gate ─┬─► act ─► observe …
                                                         ▲                │         └─► request_approval ─► APPROVAL (interrupt)
                                                         └── ask_user (interrupt)       ─► re-validate ─► act   (rejected ─► stop)
                                                                          └─► verify ─► finalize

 request_approval creates the approval record; the approval node only calls interrupt(). LangGraph re-runs an
 interrupted node from its first line on resume, so keeping side effects out of it guarantees the decision that
 is checked is exactly the one the user made.
```

| Stage | What happens |
|---|---|
| **understand / plan** | The LLM turns the instruction into a goal, plan, success criteria, user-provided data and assumptions. It asks **one** clarification question only if essential information is missing. Harmful requests are refused. |
| **observe** | Generic JavaScript collects visible interactive elements (open shadow roots included), computes accessible names, tags each one `data-nova-id=N`, and captures visible text, media state, scroll position and CAPTCHA signals. A screenshot is attached on new pages and after failures (`VISION_MODE`). |
| **decide** | The LLM picks **one** tool call (navigate, web_search, click, type, select, press, scroll, go_back, switch_tab, wait, download, extract, request_human_help, ask_user, done). Page content is wrapped in an UNTRUSTED block. |
| **gate** | A **deterministic backend policy** classifies the action from what it would actually do: the target's accessible name, its form context, the link or URL destination, secret fields, and executable downloads. The LLM can *add* an approval requirement but never remove one. |
| **approval** | The agent pauses (LangGraph `interrupt`). The card shows the action, website, target, values, amounts next to the target, and the agent's stated details. |
| **act** | Before a sensitive action runs, NOVA re-observes the page, re-locates the target and recomputes the approval fingerprint. **Any material change (price, target, destination, text) supersedes the approval and asks again.** Approvals are single-use. |
| **verify** | A separate verifier call checks the final page and history against the success criteria. A task reports *completed* only with evidence; otherwise it reports *not verified* or *failed* with a retry option. |

### Project structure

```
backend/
  main.py, run.py, config.py
  agent/      state.py (in-memory task state) · planner.py · executor.py (nodes) · workflow.py (graph + runner)
              tools.py (modular tool registry) · prompts.py · capabilities.py (task-launcher catalog + form validation)
  browser/    browser_manager.py (Chromium, tabs, downloads, live screenshots, navigation guard)
              browser_actions.py (site-agnostic actions) · page_extractor.py (DOM/accessibility observation)
              security.py (URL policy)
  services/   llm_service.py (Gemini with model fallback / OpenAI / Ollama) · safety_policy.py (sensitive-action classifier)
              approval_service.py (fingerprinted approvals) · comparison_service.py (verified result tables)
  routes/     task_routes.py (REST + WebSocket) · approval_routes.py
  realtime/   connection_manager.py (WebSocket fan-out)
  models/     task_models.py · approval_models.py
  tests/      safety, approvals, anti-fabrication, live browser layer, launcher validation, end-to-end agent
              scenarios (order + re-approval, rejection, form filling, prompt injection, clarification,
              pause/resume/stop) · live_smoke.py (real websites, run manually)
frontend/     React + TypeScript + Vite dashboard (task launcher, chat, live preview, timeline, approval card,
              comparison tables)
```

**No database.** Tasks, approvals, results and LangGraph checkpoints (`InMemorySaver`) live in process memory and
disappear when the backend stops.

### Safety model (enforced in the backend, not the UI)

- **Approval is required for:** payments, orders, bookings and reservations, job applications, sending messages or
  posts, cancellations, deletions, account or settings changes, submitting forms that contain personal data, typing
  passwords, card numbers or OTPs, sensitive URLs (`/delete`, `/cancel`, `/pay` …), and executable downloads.
- **Payments need a second explicit confirmation** (the checkbox on the card). The backend rejects a payment approval
  without it.
- **Fingerprint binding:** the decision must echo the fingerprint of the card the user saw, so a stale card cannot
  approve a changed action. Before execution, the fingerprint is recomputed from the live page.
- **Defence in depth:** `act` refuses to execute a sensitive action without a valid, matching, unused approval, even if
  graph routing were wrong.
- **Prompt-injection resistance:** page text is passed only as delimited untrusted data. The LLM never decides what
  needs approval, and its outputs are validated against the tool schema.
- **No fabricated data:** every extracted record is checked against the page's own text and accessibility labels.
  Records with values not found on the page are discarded, and the count is shown.
- **Navigation policy:** http(s) only. `file:`, `javascript:` and localhost/LAN addresses are blocked, with an optional
  allowlist or blocklist. It is enforced on every document request through CDP, with a post-navigation backstop.
- **CAPTCHA and logins:** detected or requested, then handed to you (*"I've completed it"*). NOVA never tries to solve
  or bypass them.
- **API keys** are read only by the backend from `.env`, and the frontend talks to the backend through a proxy.
- **Stop / Pause / Resume** take effect before the next browser action. Stop also cancels in-flight LLM calls.

---

## Setup

Prerequisites: Python 3.11+ (tested with 3.13), Node.js 18+ (tested with 22), and a Gemini or OpenAI API key.

```powershell
# 1. Configure (repository root)
copy .env.example .env          # then set GEMINI_API_KEY=... (free key: https://aistudio.google.com/apikey)
                                # or LLM_PROVIDER=openai + OPENAI_API_KEY=...

# 2. Backend (the venv lives at the repository root)
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
cd backend
..\.venv\Scripts\python.exe run.py              # http://127.0.0.1:8000  (do NOT use `uvicorn --reload` on Windows)

# 3. Frontend (second terminal)
cd frontend
npm install
npm run dev                                     # open http://localhost:5173
```

### Choosing the AI model

| Setting | Effect |
|---|---|
| `LLM_PROVIDER=gemini` (default) | Gemini Flash (`GEMINI_MODEL=gemini-3.8-flash`). Older `gemini-2.5-*` models are not available to new keys. |
| `GEMINI_FALLBACK_MODELS` | Other Flash / Flash-Lite models tried when the main one is overloaded (503), over its per-minute quota (429) or retired (404). A rate-limited model is skipped until Google's `retryDelay` passes. |
| `LLM_FALLBACK_PROVIDER=ollama` | When **every** Gemini model is unavailable, use a local model instead of failing (no waiting on Gemini rate limits). |
| `LLM_PROVIDER=ollama` | Run fully locally, with no API key or quota. |
| `LLM_PROVIDER=openai` | OpenAI (`OPENAI_API_KEY`, `OPENAI_MODEL`). |

Ollama setup: install from https://ollama.com (or `winget install Ollama.Ollama`), then `ollama pull qwen3:4b`.
On a laptop without a dedicated GPU a local model needs roughly 1–2 minutes per agent step, so it works best as
the fallback. With an NVIDIA GPU, larger models such as `qwen3:8b` are practical as the main provider.
The free Gemini tier allows only a few requests per minute per model. The agent makes one model call per browser
step, so long tasks may move through the fallback models.

**Everyday start (Windows):** double-click `start-nova.bat` in the project folder. It opens a "NOVA backend" and a
"NOVA frontend" window and then the dashboard at http://localhost:5173. Close those two windows to stop NOVA.

If port 8000 is taken (for example by an older backend still running), start the backend with `$env:PORT="8001"`
and the frontend with `$env:VITE_BACKEND_URL="http://127.0.0.1:8001"`.

### Tests

```powershell
cd backend
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD="1"          # avoids LangSmith's auto-loaded pytest plugin
..\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin -q
```

The pytest suite runs headless Chromium against local fixture pages with a scripted LLM, so it needs no network or
API key. To check the browser layer against **real websites** (YouTube playback, Amazon prices, DuckDuckGo, arXiv,
Google Flights fares, Naukri jobs):

```powershell
..\.venv\Scripts\python.exe tests\live_smoke.py --headed
```

Some sites (Naukri, for example) deny headless browsers, so use `--headed`. Google Search shows automated browsers a
CAPTCHA, which is why `SEARCH_ENGINE` defaults to DuckDuckGo. NOVA detects CAPTCHAs and hands them to you; it never
tries to solve them.

### Windows: Controlled Folder Access / Smart App Control

Some Windows machines block this project from running:
- **Controlled Folder Access** (Ransomware protection) stops `python.exe` and `node.exe` from writing inside
  `Documents`. That breaks `venv` creation and `npm install` there. Either allow both apps under *Windows Security →
  Ransomware protection → Allow an app*, or keep the project, venv and `node_modules` outside protected folders.
- **Smart App Control / App Control for Business** can refuse to load some compiled Python extensions. On the
  development machine it blocked `ormsgpack` 1.13 (LangGraph's checkpointer) and `orjson` 3.13 (imported by FastAPI and
  LangSmith), while older builds were allowed; `requirements.txt` therefore pins `ormsgpack==1.12.2` and
  `orjson==3.11.5`. If you see `DLL load failed … An Application Control policy has blocked this file`, try another
  version of the named package, or allow it in the policy. This is a device security setting for you to decide on.

## Adding a capability

**A new launcher task** is one entry in `CATALOG` in `backend/agent/capabilities.py`: its form fields (with
`required=True` for the essentials) and a `compose` function that turns the answers into an instruction. The API
and the dashboard pick it up automatically.

**A new browser ability**: register a tool in `backend/agent/tools.py`:

```python
@tool("hover", "Hover over an element to reveal menus.", '{"element": int}', uses_element=True)
async def _hover(ctx, p):
    await ctx.actions.page.locator(f'[data-nova-id="{p["element"]}"]').hover()
    return ActionResult(True, "Hovered.")
```

The prompt, safety gate, approvals, timeline and UI pick it up automatically. To add a new kind of sensitive action,
extend the rules in `services/safety_policy.py`.

## Limitations

- Behaviour depends on the LLM and on live websites, which change and sometimes block automation. When a site blocks
  automation or demands a login, NOVA reports it or hands control to you rather than inventing results.
- Logged-in tasks (Gmail drafts, job applications) need you to sign in inside the NOVA browser window. Sessions are not
  persisted.
- Closed shadow roots and cross-origin iframes are not inspected through the DOM. The screenshot fallback helps the
  model reason about them, but it cannot act inside them.
