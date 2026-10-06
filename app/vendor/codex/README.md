# Vendored from OpenAI Codex

| File | Upstream path | Changes |
|---|---|---|
| `prompt.md` | `codex-rs/models-manager/prompt.md` | none — byte for byte |
| `LICENSE` | `LICENSE` (Apache-2.0) | none |
| `NOTICE` | `NOTICE` | none |

Source: <https://github.com/openai/codex>, tag `rust-v0.149.1`
(commit `ff29a44391deccde0aba0f8390337d7f3c319ea4`).

`prompt.md` is licensed under Apache-2.0 by OpenAI, **not** under LiteGate's MIT
license. The licence and notice travel in this folder so they stay next to the
file wherever `app/` is copied.

## Why it is here

Codex takes its system prompt from the model catalogue, and a catalogue entry
without one is not an entry Codex accepts: the whole response fails to decode
with ``model `coding` is missing both `base_instructions` and
`model_messages.instructions_template` `` (`deserialize_model_infos_with_legacy_base`
in `codex-rs/protocol/src/openai_models.rs`; seen with Codex CLI 0.149.1 on
2026-10-06). So a catalogue for Codex has to carry instructions, for every
model.

Before LiteGate served a catalogue Codex could read, every alias fell back to
Codex's built-in metadata, and that fallback uses exactly this file. Serving it
as `base_instructions` keeps the prompt Codex was already sending — measured:
20,751 characters before and after — so the only things that change are the
ones the registry actually knows (context window, input modalities). See
`app/core/codexcatalog.py`.

## Updating

Copy the three files again from a newer tag and update the tag and commit above.
Do not edit `prompt.md` by hand — `tests/test_codex_catalog.py` compares what
Codex receives against this file.
