# PRD v1.2 Addendum
## Multimodal & Capability-Aware Model Gateway

### 1. Model Registry ต้องเปลี่ยนจาก Model List เป็น Capability Registry

Gateway ต้องไม่สมมติว่า model ทุกตัวมีความสามารถเหมือนกัน

แต่ละ model ต้องประกาศ capability ของตัวเอง

```yaml
model:
  alias: muse-local
  display_name: Muse Glimmer Local

  backend:
    type: vllm
    url: http://dgx01:8000
    model: meta-models/Muse-Glimmer-30B

  modalities:
    input:
      - text
      - image
    output:
      - text

  capabilities:
    chat: true
    vision: true
    coding: true
    tools: true
    streaming: true
    agentic: true
    reasoning: true

  protocols:
    openai: true
    anthropic: false

  limits:
    context_tokens: 131072
```

อีกตัว:

```yaml
model:
  alias: gemma-vision
  display_name: Gemma Vision

  backend:
    type: vllm
    url: http://dgx02:8000
    model: google/gemma-4-31B-it

  modalities:
    input:
      - text
      - image
    output:
      - text

  capabilities:
    chat: true
    vision: true
    coding: true
    tools: true
    streaming: true
    agentic: true
    reasoning: true

  limits:
    context_tokens: 262144
```

Coding model:

```yaml
model:
  alias: coding
  display_name: Qwen Local Coder

  backend:
    type: vllm
    url: http://dgx03:8000
    model: ucbye/Qwen3-Coder-Next-NVFP4-GB10

  modalities:
    input:
      - text
    output:
      - text

  capabilities:
    chat: true
    vision: false
    coding: true
    tools: true
    streaming: true
    agentic: true

  protocols:
    openai: true
    anthropic: true

  limits:
    context_tokens: 262144
```

---

# 2. Model Type ไม่ควรเป็น Enum เดียว

ไม่ใช้:

```text
type = chat
type = vision
type = coding
```

เพราะ model หนึ่งตัวสามารถมีหลาย capability พร้อมกันได้

ให้ใช้:

```text
capabilities

chat        ✓
vision      ✓
coding      ✓
tools       ✓
reasoning   ✓
agentic     ✓
embedding   ✗
audio       ✗
```

ทำให้ในอนาคตเพิ่ม model ใหม่ได้โดยไม่ต้องแก้ Gateway core

---

# 3. Multimodal Request Support

เพิ่ม P0:

```http
POST /v1/chat/completions
```

ให้รองรับ content blocks:

```json
{
  "model": "gemma-vision",
  "messages": [
    {
      "role": "user",
      "content": [
        {
          "type": "text",
          "text": "อธิบายภาพนี้"
        },
        {
          "type": "image_url",
          "image_url": {
            "url": "..."
          }
        }
      ]
    }
  ]
}
```

รองรับ:

```text
Text
Image URL
Base64 Image
```

P1 ค่อยเพิ่ม:

```text
Uploaded Image
PDF
Audio
Video
```

Gateway ไม่ควรทำ OCR หรือ image understanding เอง

หน้าที่ Gateway คือ:

```text
Receive multimodal request
        │
        ▼
Validate capability
        │
        ▼
Validate size/policy
        │
        ▼
Forward to model server
```

---

# 4. Capability Validation

ถ้านักเรียนเรียก:

```text
model = coding
```

แล้วส่ง image

Gateway ตรวจพบ:

```text
vision = false
```

ให้ตอบ:

```http
HTTP 400
```

```json
{
  "error": {
    "code": "MODEL_CAPABILITY_NOT_SUPPORTED",
    "message": "Model 'coding' does not support image input."
  }
}
```

ไม่ควรส่ง request ไป backend แล้วรอให้ vLLM/Ollama error

---

# 5. Model Purpose

เพิ่ม field:

```text
purpose
```

ค่าตัวอย่าง:

```text
general
coding
vision
reasoning
agent
fast
```

หนึ่ง model มีได้หลาย purpose

ตัวอย่าง:

```yaml
purpose:
  - general
  - vision
  - agent
```

หรือ:

```yaml
purpose:
  - coding
  - agent
```

---

# 6. Member Model UX

นักเรียนไม่ควรต้องเห็น model repository name

หน้า model:

```text
Available AI Models

General AI
────────────────────────
Muse Local
Text • Image • Tools
128K Context


Vision AI
────────────────────────
Gemma Vision
Text • Image • Reasoning
256K Context


Coding AI
────────────────────────
Local Coder
Code • Tools • Agent
256K Context
Claude Code Ready
```

ชื่อจริง:

```text
meta-models/Muse-Glimmer-30B
google/gemma-4-31B-it
ucbye/Qwen3-Coder-Next-NVFP4-GB10
```

ให้ Admin เห็น

นักเรียนเห็นเพียง:

```text
muse-local
gemma-vision
coding
```

---

# 7. Recommended Initial Model Roles

สำหรับ infrastructure ชุดแรก:

```text
                  LiteGate
                         │
          ┌──────────────┼───────────────┐
          │              │               │
          ▼              ▼               ▼
   general-agent      vision          coding
          │              │               │
          ▼              ▼               ▼
 Muse Glimmer      Gemma 4 31B      Qwen Coder
      30B               IT             Next
```

แต่ไม่บังคับ mapping นี้

Admin สามารถเปลี่ยนได้ภายหลัง

ตัวอย่าง:

```text
coding
   ↓
Qwen3-Coder-Next
```

วันหนึ่งอาจเปลี่ยนเป็น:

```text
coding
   ↓
Model ใหม่
```

โดยนักเรียนไม่ต้องแก้ configuration

---

# 8. Model Selection สำหรับ Claude Code

Coding model ควรมี compatibility profile เพิ่มอีกชุด:

```yaml
agent_clients:

  claude_code:
    enabled: true
    tested: true
    tools: true
    streaming: true
    long_context: true

  qwen_code:
    enabled: true

  cline:
    enabled: false
```

Gateway ไม่ควรตัดสินจากชื่อ model

เช่น:

```text
Qwen = coding
Gemma = general
```

แต่ตัดสินจาก:

```text
model capability
+
compatibility test
```

---

# 9. Vision + Agent

ต้องรองรับกรณี:

```text
Coding Agent
     │
     ├── source code
     ├── screenshot
     ├── architecture diagram
     └── error screenshot
            │
            ▼
       Vision Agent Model
```

ทำให้ในอนาคตนักเรียนสามารถใช้ AI ช่วย:

```text
อ่าน screenshot ของ application
วิเคราะห์ UI
อ่าน diagram
ตรวจ error จาก screenshot
อธิบายกราฟ
อ่านรูปวงจร
อ่าน worksheet
วิเคราะห์ภาพจาก laboratory
```

โดยไม่ต้องเปลี่ยน Gateway architecture

---

# 10. Multimodal Quota

Token quota เดิมต้องรองรับ:

```text
text_input_tokens
image_input_tokens
output_tokens
```

Usage record:

```text
request_id

member_id
workspace_id

model_id

text_input_tokens
visual_input_tokens
output_tokens

image_count

latency_ms
ttft_ms

status
```

ไม่ควรเก็บ image ใน usage log

---

# 11. Image Privacy

Default:

```text
Store prompt     NO
Store response   NO
Store images     NO
```

Gateway สามารถใช้ request streaming/pass-through

ถ้ารับ Base64 image:

```text
Client
   │
   ▼
Gateway memory
   │
   ▼
Model Server
```

เมื่อ request จบ:

```text
discard
```

ไม่เขียนลง disk โดย default

---

# 12. Image Policy

Admin กำหนดได้:

```yaml
vision_policy:

  max_images_per_request: 4

  max_image_size_mb: 10

  allowed_types:
    - image/jpeg
    - image/png
    - image/webp

  remote_image_url:
    enabled: false
```

แนะนำให้ `remote_image_url = false` เป็น default สำหรับระบบภายใน

เพื่อลดความซับซ้อนและไม่ให้ Gateway กลายเป็นเครื่อง fetch URL จาก Internet

นักเรียนใช้:

```text
Base64
หรือ
local upload
```

แทน

---

# 13. Gateway ต้องไม่ Process Image

ไม่ทำ:

```text
Resize
OCR
Vision Encoding
Image conversion
Object detection
```

ใน MVP

ให้ model server เป็นคนจัดการ

```text
Gateway
   │
   │ original multimodal request
   ▼
vLLM / Ollama
   │
   ▼
Vision Model
```

หลักนี้ช่วยให้ Gateway ยังเล็กเหมือนเดิม

---

# 14. Backend Capability

เพิ่มใน `model_endpoints`

```text
server_type

vllm
ollama
sglang
llama.cpp
```

พร้อม:

```text
supported_protocols
supported_modalities
```

ตัวอย่าง:

```yaml
endpoint:

  server_type: vllm

  protocol:
    openai: true
    anthropic: true

  modality:
    text: true
    image: true
```

Model capability และ server capability ต้องผ่านทั้งสองเงื่อนไข

```text
Request
   │
   ▼
Model supports Vision?
   │ yes
   ▼
Backend supports Vision?
   │ yes
   ▼
Route
```

---

# 15. Capability-Aware Routing

Routing เปลี่ยนจาก:

```text
model alias
   ↓
endpoint
```

เป็น:

```text
Request
   │
   ▼
Member Permission
   │
   ▼
Model Alias
   │
   ▼
Required Capability
   │
   ├── Text
   ├── Vision
   ├── Tool
   └── Agent
   │
   ▼
Healthy Compatible Endpoint
   │
   ▼
Inference
```

---

# 16. Do Not Add AI Router Yet

MVP ไม่ต้องสร้าง:

```text
Prompt
 ↓
AI classifier
 ↓
Choose best model
```

เพราะเพิ่ม complexity

ให้นักเรียนเลือก alias:

```text
general
vision
coding
agent
```

หรือเลือก model ที่ Admin อนุญาต

P2 ค่อยพิจารณา:

```text
model = auto
```

---

# 17. Model Registration Wizard

หน้า Add Model ควรเป็น:

```text
Add AI Model

Name
[_____________________]

Alias
[_____________________]

Server
[vLLM ▼]

Base URL
[http://10.0.0.21:8000]

Upstream Model
[_____________________]


Capabilities

☑ Text
☐ Vision
☐ Audio

☑ Streaming
☐ Tools
☐ Reasoning
☐ Agentic Coding

Context
[131072]


Compatibility

☑ OpenAI API
☐ Anthropic API
☐ Claude Code
```

แล้วมี:

```text
[ Detect Capabilities ]
```

แต่ detection เป็นเพียง suggestion

Admin ต้อง confirm ก่อน Save

---

# 18. Model Test Suite

จากเดิมมี:

```text
Test Chat
Test Streaming
Test Tools
Test Claude Code
```

เพิ่ม:

```text
Test Vision
```

Full:

```text
MODEL-001 Basic Chat
MODEL-002 Streaming
MODEL-003 Long Context
MODEL-004 Tool Calling
MODEL-005 Multi Tool
MODEL-006 Vision
MODEL-007 Vision + Text
MODEL-008 Agent Loop
MODEL-009 Claude Code
MODEL-010 Concurrent Load
```

Model status ตัวอย่าง:

```text
Gemma Vision

Chat              PASS
Streaming         PASS
Vision            PASS
Tools             PASS
Claude Code       NOT TESTED

Status            READY
```

---

# 19. Additional Database Fields

`models`

```text
id
alias
display_name
upstream_model

context_length

supports_text
supports_image
supports_audio
supports_video

supports_streaming
supports_tools
supports_reasoning
supports_agentic

supports_openai
supports_anthropic

claude_code_compatible
enabled
```

`usage_logs`

เพิ่ม:

```text
image_count
visual_input_tokens
request_modality
```

`model_compatibility`

```text
model_id
feature
status
tested_at
test_version
notes
```

---

# 20. New Functional Requirements

| ID | Requirement | Priority |
|---|---|---|
| FR-30 | Text + Image request | P0 |
| FR-31 | Model capability registry | P0 |
| FR-32 | Capability validation | P0 |
| FR-33 | Multimodal streaming | P0 |
| FR-34 | Image size limit | P0 |
| FR-35 | Image type validation | P0 |
| FR-36 | Vision model test | P1 |
| FR-37 | Visual token accounting | P1 |
| FR-38 | Multimodal usage dashboard | P1 |
| FR-39 | Capability auto-detection | P2 |
| FR-40 | Automatic model selection | P3 |

---

# 21. Additional TODO

```text
[ ] Add model modality schema
[ ] Add capability schema
[ ] Add text/image content parser
[ ] Preserve multimodal content blocks
[ ] Validate image capability
[ ] Validate image MIME
[ ] Validate image size
[ ] Support Base64 image
[ ] Support OpenAI image_url format
[ ] Add visual usage fields
[ ] Add Vision compatibility test
[ ] Add capability badges in Admin UI
[ ] Add capability badges in Member UI
[ ] Add backend capability validation
```

---

# 22. Additional Tasks

```text
GW-100 Model Capability Schema
GW-101 Model Modality Schema
GW-102 Multimodal Request Parser
GW-103 Image Capability Validation
GW-104 Image MIME Validation
GW-105 Image Size Limiter
GW-106 Base64 Image Passthrough
GW-107 OpenAI Vision Passthrough
GW-108 Vision Compatibility Test
GW-109 Visual Token Usage
GW-110 Capability UI
GW-111 Backend Capability Matrix
GW-112 Multimodal Load Test
```

---

# 23. Revised Core Architecture

```text
                         STUDENTS

             ┌──────────────┼───────────────┐
             │              │               │
             ▼              ▼               ▼
          Python        Web / App       Claude Code
             │              │               │
             └──────────────┼───────────────┘
                            │
                            ▼
              ┌─────────────────────────┐
              │     LiteGate      │
              │                         │
              │ Authentication          │
              │ Workspace Policy           │
              │ Capability Registry     │
              │ Modality Validation     │
              │ Quota                   │
              │ Agent Sessions          │
              │ Routing                 │
              │ Streaming               │
              │ Usage                   │
              └────────────┬────────────┘
                           │
          ┌────────────────┼────────────────┐
          │                │                │
          ▼                ▼                ▼
       DGX #1           DGX #2           DGX #3
        vLLM             vLLM             vLLM
          │                │                │
          ▼                ▼                ▼
   Muse Glimmer       Gemma 4         Qwen Coder
      30B              31B              Next
          │                │                │
     Text/Image        Text/Image       Text/Code
       Tools           Reasoning         Tools
       Agent             Tools           Agent
```

หลักสำคัญยังเหมือนเดิม:

```text
Gateway จัดการ:
Identity
Permission
Capability
Quota
Routing
Usage
Protocol

Model Server จัดการ:
Inference
Tokenizer
Vision Encoder
Tool Parser
KV Cache
GPU
```

จึงยังรักษา Gateway ให้เล็กได้ แม้จะเพิ่ม Vision และ Agentic AI เข้ามา