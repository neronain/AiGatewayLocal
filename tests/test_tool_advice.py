"""Tool-calling remediation must match the backend's engine.

The trap this locks down: a llama.cpp backend that emits no tool_calls needs
--jinja, but the advice used to hardcode vLLM's --tool-parser (and a stray
"vLLM" in a probe note even made the engine detector misread llama.cpp as vLLM).
"""

from __future__ import annotations

from app.core.modeltest import ProbeResult, _normalize_kind, build_advice


def _issues(result: ProbeResult) -> set[str]:
    return {a.issue for a in build_advice(result)}


def test_llamacpp_tool_failure_advises_jinja_not_vllm():
    result = ProbeResult(
        reachable=True,
        capabilities={"tools": False},
        server_kind="llamacpp",
        notes=["tools: backend accepted the request but returned no tool_calls"],
    )
    issues = _issues(result)
    assert "jinja_missing" in issues
    assert "tools_flag_missing" not in issues
    jinja = next(a for a in build_advice(result) if a.issue == "jinja_missing")
    assert "--jinja" in (jinja.fix + jinja.command)
    assert "tool-parser" not in jinja.command  # not vLLM advice


def test_vllm_tool_failure_still_advises_parser():
    result = ProbeResult(
        reachable=True,
        capabilities={"tools": False},
        server_kind="vllm",
        notes=["tools -> HTTP 400: tool choice requires --enable-auto-tool-choice"],
    )
    issues = _issues(result)
    assert "tools_flag_missing" in issues
    assert "jinja_missing" not in issues


def test_registry_server_type_wins_over_a_misleading_note():
    # Even if a note mentions vLLM, an endpoint the registry calls llama.cpp must
    # be treated as llama.cpp (this is what probe_backend now does with server_type).
    assert _normalize_kind("llama.cpp") == "llamacpp"
    assert _normalize_kind("vLLM") == "vllm"
    assert _normalize_kind("sglang") == "sglang"
    assert _normalize_kind("") == ""
