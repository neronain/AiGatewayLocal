"""Tool-calling remediation must match the backend's engine.

The trap this locks down: a llama.cpp backend that emits no tool_calls needs
--jinja, but the advice used to hardcode vLLM's --tool-parser (and a stray
"vLLM" in a probe note even made the engine detector misread llama.cpp as vLLM).
"""

from __future__ import annotations

from app.core.modeltest import (
    PARSER_HINT_PREFIX,
    ProbeResult,
    _normalize_kind,
    build_advice,
    detect_leaked_tool_syntax,
    suggest_tool_parser,
)


def test_qwen_tool_parser_mapping():
    # Measured mapping (Qwen3.5-122B-A10B research): Qwen3/3.5 use qwen3_xml,
    # Qwen3-Coder uses qwen3_coder; older bare "qwen" falls back to hermes.
    assert suggest_tool_parser("Qwen3.5-122B-A10B-int4-AutoRound") == ("qwen3_xml", True)
    assert suggest_tool_parser("qwen3-6-35b-a3b") == ("qwen3_xml", True)
    assert suggest_tool_parser("Qwen3-Coder-30B-A3B") == ("qwen3_coder", True)
    assert suggest_tool_parser("Qwen2.5-7B") == ("hermes", False)  # bare qwen fallback


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


def test_gemma4_tool_parser_mapping():
    # Measured on a live gemma-4-31B backend: started with hermes it answers 200
    # and returns the call as text; with gemma4 it returns real tool_calls. Before
    # this mapping existed Gemma fell through to the hermes default, so the tool
    # suggested exactly the setting that was already broken.
    assert suggest_tool_parser("google/gemma-4-31B-it") == ("gemma4", True)
    assert suggest_tool_parser("gemma4-26b-uncensored") == ("gemma4", True)


def test_leaked_tool_syntax_is_recognised_per_family():
    assert detect_leaked_tool_syntax(
        '<|tool_call>call:get_weather{city:<|"|>Bangkok<|"|>}<tool_call|>'
    ) == ("Gemma 4 <|tool_call>", "gemma4")
    assert detect_leaked_tool_syntax('<tool_call>{"name": "x"}</tool_call>') == (
        "Hermes-style <tool_call>",
        "hermes",
    )
    # A plain answer is the "model has no tool template" case, not a mismatch.
    assert detect_leaked_tool_syntax("It is sunny in Bangkok.") is None
    assert detect_leaked_tool_syntax(None) is None


def test_parser_mismatch_is_a_warning_naming_the_right_parser():
    # The probe saw the call come back as text: this is the right model behind the
    # wrong parser, which is worse than no tools at all because callers receive
    # the raw call as their answer. It must not be filed as informational.
    result = ProbeResult(
        reachable=True,
        capabilities={"tools": False},
        server_kind="vllm",
        notes=[
            "tools: backend accepted the request but returned no tool_calls",
            "tools: the reply carried Gemma 4 <|tool_call> syntax as text, so a "
            f"parser is running but does not match this model ({PARSER_HINT_PREFIX}gemma4)",
        ],
    )
    issues = _issues(result)
    assert "tool_parser_mismatch" in issues
    assert "tools_unavailable" not in issues
    found = next(a for a in build_advice(result) if a.issue == "tool_parser_mismatch")
    assert found.severity == "warning"
    assert "gemma4" in found.command
    assert "hermes" not in found.command


def test_no_leaked_syntax_stays_informational():
    # Nothing recognisable came back, so there is no parser to name and no command
    # to offer - saying "declare tools=false" is still the honest answer.
    result = ProbeResult(
        reachable=True,
        capabilities={"tools": False},
        server_kind="vllm",
        notes=["tools: backend accepted the request but returned no tool_calls"],
    )
    issues = _issues(result)
    assert "tools_unavailable" in issues
    assert "tool_parser_mismatch" not in issues
