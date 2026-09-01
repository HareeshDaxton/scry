# Scry

**Terminal-native software archaeology. Maps unknown codebases before you know what to ask.**

You clone a 100,000-file repository with no documentation. The person who built
it left two years ago. You open a file called
`process_payment_intent_v3_final_actual()` and you have no idea who calls it,
whether it runs in production, or why it changed 42 times in three months while
the file next to it hasn't been touched since 2023.

That is not a "help me understand this" problem. It's an *"I don't know what
exists, so I don't know what to ask"* problem — and a chatbot that waits for
questions cannot solve it.

Scry builds the map first.

## Status

Early development. Section 1.1 of 93 complete — the project scaffold.
No analysis features are implemented yet.

## Design principle

> An LLM never produces a fact that can be computed.
> It only produces language about facts that were computed.

Nine of the eleven components are pure deterministic code: git miners, static
analyzers, dependency resolvers, graph algorithms. Facts carry provenance — you
can point at the line that proves them. A language model only ever phrases what
was already computed, and only when one is configured.

The practical consequence: **Scry's full analysis runs with no model, no API
key, and no network.** Findings and their ranking are byte-identical whether or
not a model is available; only the prose differs.

## Requirements

- Python 3.11 or newer
- git 2.x on PATH
- 4 GB RAM

## Development

This project uses [uv](https://docs.astral.sh/uv/).

```bash
uv sync                 # create the environment and install, including dev deps
uv run scry             # run the CLI
uv run pytest           # run the test suite
uv run ruff check .     # lint
```

## Licence

Not yet chosen — see the note in the build plan.
