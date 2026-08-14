"""Does LiteLLM handle everything this gateway is asked to handle?

Kept in the repo because the answer changed once already and the reason it
changed was a fault in the measurement, not in LiteLLM. Re-measure rather than
re-remember.

    docker run -d --name litellm-probe -p 4010:4000 \
      -v "$PWD/docs/litellm-probe.yaml:/app/config.yaml" \
      -e LITELLM_MASTER_KEY=sk-probe \
      ghcr.io/berriai/litellm:main-stable --config /app/config.yaml --port 4000
    python scripts/litellm_probe.py

Use the official image. A pip install pulls a FastAPI the proxy cannot import
and a CLI that does not start, and judging LiteLLM on that is measuring the
install.

`max_tokens` is set generously on purpose: a reasoning model spends its budget
thinking, and too small a number returns an empty answer that looks exactly
like a broken translation layer. That mistake is what made the first run of
this probe report a bug that was not there.
"""
import base64
import json
import struct
import sys
import urllib.request
import zlib

BASE = "http://localhost:4010"
KEY = "sk-probe"
results = []


def call(path, payload, stream=False, timeout=600):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}",
                 "anthropic-version": "2023-06-01"},
    )
    r = urllib.request.urlopen(req, timeout=timeout)
    if stream:
        return r
    return json.load(r)


def record(name, ok, detail):
    results.append((name, ok, detail))
    print(f"{'ผ่าน' if ok else 'ไม่ผ่าน':7} · {name:38} · {detail}")


# 1 · OpenAI chat
try:
    d = call("/v1/chat/completions", {"model": "coder-next",
             "messages": [{"role": "user", "content": "say hi"}], "max_tokens": 16})
    record("/v1/chat/completions", bool(d["choices"][0]["message"]["content"]),
           repr(d["choices"][0]["message"]["content"])[:50])
except Exception as e:
    record("/v1/chat/completions", False, f"{type(e).__name__}: {e}")

# 2 · Anthropic non-streaming
try:
    d = call("/v1/messages", {"model": "coder-next", "max_tokens": 16,
             "messages": [{"role": "user", "content": "say hi"}]})
    record("/v1/messages (ไม่สตรีม)", d.get("type") == "message", f"type={d.get('type')}")
except Exception as e:
    record("/v1/messages (ไม่สตรีม)", False, f"{type(e).__name__}: {e}")

# 3 · tool_use
TOOL = [{"name": "read_file", "description": "Read a file",
         "input_schema": {"type": "object", "properties": {"path": {"type": "string"}},
                          "required": ["path"]}}]
try:
    d = call("/v1/messages", {"model": "coder-next", "max_tokens": 200, "tools": TOOL,
             "messages": [{"role": "user", "content": "Read config.yaml"}]})
    use = next((b for b in d.get("content", []) if b.get("type") == "tool_use"), None)
    record("tool_use", use is not None, str(use and use.get("input"))[:50])
except Exception as e:
    record("tool_use", False, f"{type(e).__name__}: {e}")

# 4 · tool_result round trip
try:
    d = call("/v1/messages", {"model": "coder-next", "max_tokens": 200, "tools": TOOL,
             "messages": [
                 {"role": "user", "content": "Read config.yaml"},
                 {"role": "assistant", "content": [
                     {"type": "tool_use", "id": "t1", "name": "read_file",
                      "input": {"path": "config.yaml"}}]},
                 {"role": "user", "content": [
                     {"type": "tool_result", "tool_use_id": "t1", "content": "port: 8080"}]},
             ]})
    record("tool_result round trip", d.get("stop_reason") == "end_turn",
           f"stop_reason={d.get('stop_reason')}")
except Exception as e:
    record("tool_result round trip", False, f"{type(e).__name__}: {e}")

# 5 · vision — a 224x224 red square, big enough to survive patching
try:
    w = h = 224
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))
    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    d = call("/v1/messages", {"model": "muse", "max_tokens": 512, "messages": [
        {"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": base64.b64encode(png).decode()}},
            {"type": "text", "text": "One word: what colour is this square?"}]}]})
    text = "".join(b.get("text", "") for b in d.get("content", []))
    record("vision (PNG 224x224)", "red" in text.lower(), repr(text)[:60])
except Exception as e:
    record("vision (PNG 224x224)", False, f"{type(e).__name__}: {e}")

# 6 · Claude-Code-shaped: long system prompt + tools
try:
    d = call("/v1/messages", {"model": "coder-next", "max_tokens": 200, "tools": TOOL,
             "system": "You are a coding assistant. " * 800,
             "messages": [{"role": "user", "content": "Read config.yaml"}]})
    use = next((b for b in d.get("content", []) if b.get("type") == "tool_use"), None)
    record("system 21k chars + tools", use is not None, f"stop={d.get('stop_reason')}")
except Exception as e:
    record("system 21k chars + tools", False, f"{type(e).__name__}: {e}")


# 7 · the one that failed before: streaming a reasoning model
def stream_messages(model):
    r = call("/v1/messages", {"model": model, "max_tokens": 512, "stream": True,
             "messages": [{"role": "user", "content": "Count 1 to 3, one per line."}]},
             stream=True)
    deltas, text = 0, []
    for line in r:
        line = line.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        body = line[5:].strip()
        if body in ("", "[DONE]"):
            continue
        try:
            ev = json.loads(body)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "content_block_delta":
            deltas += 1
            text.append(ev.get("delta", {}).get("text", ""))
    return deltas, "".join(text)

for model, kind in (("coder-next", "ไม่ใช่ reasoning"), ("muse", "reasoning")):
    try:
        n, text = stream_messages(model)
        record(f"stream /v1/messages · {model} ({kind})", n > 0 and text.strip() != "",
               f"content_block_delta={n} · text={text.strip()[:40]!r}")
    except Exception as e:
        record(f"stream /v1/messages · {model} ({kind})", False, f"{type(e).__name__}: {e}")

passed = sum(1 for _, ok, _ in results if ok)
print(f"\nสรุป {passed}/{len(results)}")
sys.exit(0 if passed == len(results) else 1)
