# UI Component Generator — Integration Plan

## Overview

Add a new **UI Component Generator** feature to the existing chatbot project. Users describe a component in plain English; the fine-tuned Qwen2.5-Coder model generates React + Tailwind code; the result renders live in a preview pane. Everything plugs into the existing auth, routing, and API service.

---

## Architecture

```
Browser (UIGenerator page)
  │  POST /generate-component  { prompt }
  ▼
Chatbot Backend (FastAPI)
  │  forwards prompt to ──▶  HF Space (Gradio)
  │                          sk1502/ui-component-gen
  │                          POST /api/predict { data: [prompt] }
  │  returns { code: "..." }
  ▼
Browser — renders code in sandboxed <iframe>
```

### Model Serving — Decision

| Option | Cost | Latency | Setup | Status |
|--------|------|---------|-------|--------|
| **HuggingFace Inference API** (free tier) | Free / pay-per-use | ~3–8s | Upload adapter → call REST API | ❌ Does not support PEFT/LoRA adapters |
| **vLLM on Colab/RunPod** | ~$0.30/hr | ~1–2s | Run server, expose public URL | — |
| **HuggingFace Spaces (Gradio)** | ~$0.60/hr (T4 small) | ~5–10s | Deploy Space, call via API | ✅ **Selected** |

> **Why not HF Inference API**: `sk1502/qwen-ui-adapter` is a raw LoRA adapter (PEFT) on top of `Qwen/Qwen2.5-Coder-7B-Instruct`. The free-tier Inference API cannot serve PEFT adapters — it returned 404 even after `pipeline_tag: text-generation` was added to the model card.
>
> **Selected**: HF Space loads base model + LoRA adapter with 4-bit quantization (fits T4 16GB VRAM), exposes a Gradio API.

---

## What Changes Where

### HF Space (`/home/saurav/Projects/chatbot/hf_space`) — **NEW**
- `app.py` — Gradio app: loads `Qwen/Qwen2.5-Coder-7B-Instruct` in 4-bit + applies LoRA adapter `sk1502/qwen-ui-adapter`, exposes `gr.Interface` with `api_name="generate"`
- `requirements.txt` — `transformers`, `peft`, `bitsandbytes`, `accelerate`, `torch`, `gradio`
- Deploy to: `https://huggingface.co/spaces/sk1502/ui-component-gen`
- Hardware: **T4 small** (GPU required for 7B model)

### Backend (`/home/saurav/Projects/chatbot/backend`) — **DONE**
- `modules/config.py`: added `hf_token: str` and `generate_model_url: str` (default: `https://sk1502-ui-component-gen.hf.space`)
- `app/main.py`: added `POST /generate-component` endpoint — auth-protected, calls Space via `POST {GENERATE_MODEL_URL}/api/predict` with `{"data": [prompt]}`, strips markdown fences, returns `{"code": "..."}`, 503 on failure
- `.env`: set `GENERATE_MODEL_URL=https://sk1502-ui-component-gen.hf.space`

### Frontend (`/home/saurav/Projects/chatbot/frontend`) — **PENDING**
- Add `src/pages/UIGenerator.jsx` — split pane: left = prompt + history, right = live preview + code tab
- Add `generateComponent()` method to `src/services/api.js`
- Add `/generate` route in `App.jsx`
- Add sidebar nav link in `Chat.jsx` to switch between Chat ↔ Generator

---

## Data Flow

```
1. User types: "A pricing card with gradient border and a CTA button"
2. Frontend POST /generate-component { prompt }
3. Backend POST {GENERATE_MODEL_URL}/api/predict { data: [prompt] }
4. HF Space (Gradio): formats ChatML → runs model → strips fences → returns { data: ["<code>"] }
5. Backend returns { code: "<cleaned TSX string>" }
6. Frontend receives code → injects into <iframe srcdoc> for live preview
7. User can copy code or regenerate with tweaks
```

---

## Space Deployment Steps

```bash
cd /home/saurav/Projects/chatbot/hf_space
git init
git remote add origin https://huggingface.co/spaces/sk1502/ui-component-gen
git add .
git commit -m "initial space"
git push origin main
```

Space settings on HF:
- SDK: Gradio
- Hardware: T4 small (~$0.60/hr — pause when not in use)

---

## Gradio API Contract

The Space exposes:

**Request**
```
POST https://sk1502-ui-component-gen.hf.space/api/predict
Authorization: Bearer $HF_TOKEN
Content-Type: application/json

{ "data": ["a pricing card with gradient border"] }
```

**Response**
```json
{ "data": ["function PricingCard() { ... }"] }
```

---

## Frontend Agent Prompt

> Copy this exactly and give it to your chatbot frontend coding agent.

---

```
You are working on a React + Vite frontend at /home/saurav/Projects/chatbot/frontend.

The project uses React Router v6, Framer Motion, Lucide React icons, and has an
existing design system in src/index.css with CSS variables like --bg-color,
--text-primary, --cyan-primary, --border-color, etc.

The auth system is in src/context/AuthContext.jsx. The API service is in
src/services/api.js. Existing pages: Login.jsx, Signup.jsx, Chat.jsx.

TASK: Add a UI Component Generator page to the app.

--- Step 1: Add generateComponent to src/services/api.js ---

Add this method to the existing `api` object (same API_URL):

  async generateComponent(prompt, token) {
    const response = await fetch(`${API_URL}/generate-component`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${token}`,
      },
      body: JSON.stringify({ prompt }),
    });
    if (!response.ok) throw new Error('Generation failed');
    return response.json(); // { code: string }
  },

--- Step 2: Create src/pages/UIGenerator.jsx ---

Build a full-page split-pane layout:

LEFT PANE (40% width):
- Header: "UI Generator" with a Wand2 icon (lucide-react)
- A <textarea> for the user's prompt (placeholder: "Describe your component...")
- A "Generate" button (use the same send-btn style as Chat.jsx)
- Below: a scrollable list of the last 5 generated prompts (history), each
  clickable to restore that generation. Store history in useState.
- Show a loading spinner (Loader2 from lucide-react, animate-spin) while generating.

RIGHT PANE (60% width), two tabs: "Preview" and "Code":
- PREVIEW tab: render the generated code inside a sandboxed <iframe srcdoc>.
  The iframe srcdoc should be a full HTML page that:
    - Loads React 18 + ReactDOM from CDN (unpkg)
    - Loads Tailwind CSS from CDN
    - Injects the generated code as a script
    - Renders <App /> as the root component
  Here is the srcdoc template:
  ```
  <!DOCTYPE html>
  <html>
  <head>
    <script src="https://unpkg.com/react@18/umd/react.development.js"></script>
    <script src="https://unpkg.com/react-dom@18/umd/react-dom.development.js"></script>
    <script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
    <script src="https://cdn.tailwindcss.com"></script>
  </head>
  <body>
    <div id="root"></div>
    <script type="text/babel">
      ${code}
      ReactDOM.createRoot(document.getElementById('root')).render(<App />);
    </script>
  </body>
  </html>
  ```
- CODE tab: show the raw code in a <pre><code> block with a "Copy" button
  (use the Clipboard icon from lucide-react). On copy, show a brief "Copied!"
  confirmation using useState for 2 seconds.

STATE to manage: prompt, generatedCode, isLoading, activeTab ('preview'|'code'),
history (array of {prompt, code}), error.

STYLING: Match the dark theme. Use the existing CSS variables. The split pane
should have a vertical divider (1px solid var(--border-color)). On mobile
(<768px), stack vertically: input on top, output below.

Use Framer Motion for the tab switch animation (opacity 0→1, y 8→0).
Use the existing `token = localStorage.getItem('token')` pattern for auth.

--- Step 3: Update src/App.jsx ---

Import UIGenerator and add a route:
  <Route path="/generate" element={<ProtectedRoute><UIGenerator /></ProtectedRoute>} />

--- Step 4: Update src/pages/Chat.jsx sidebar ---

In the sidebar section (after the "New Chat" button, before the History list),
add a navigation button to switch to /generate:

  import { useNavigate } from 'react-router-dom';  // already imported
  import { Wand2 } from 'lucide-react';

  <button onClick={() => navigate('/generate')} className="menu-item" style={{marginBottom: '0.5rem'}}>
    <Wand2 className="w-4 h-4" style={{color: 'var(--cyan-primary)'}} />
    <span>UI Generator</span>
  </button>

Also add a back button inside UIGenerator.jsx header to go back to /chat.

Do NOT modify any existing functionality in Chat.jsx beyond adding the nav button.
```

---

## Open Questions

> [!IMPORTANT]
> **Space cold start**: First request after idle takes ~3–5 min (loading 7B in 4-bit). Pause the Space when not in use to save cost; resume before testing.

> [!NOTE]
> **Cost**: T4 small on HF Spaces is ~$0.60/hr. Pause the Space when not actively demoing.
