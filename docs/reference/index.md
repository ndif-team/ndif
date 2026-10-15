---
title: Reference
one_liner: Lookup tables — every endpoint, schema field, Redis key, environment variable, and port, sourced from the code.
tags: [reference]
related: [docs/reference/error-messages.md, docs/reference/env-vars.md, docs/reference/http-api.md, docs/reference/schemas.md, docs/reference/glossary.md]
sources: []
---

# Reference

## What this covers

The lookup tree. These pages are exhaustive rather than narrative, and each row
cites the file and symbol it comes from. When a reference page and prose elsewhere
disagree, the reference page is the one built to be checked — but the **code** is
always the authority.

## The pages

| Page | Answers |
|---|---|
| [HTTP API Reference](http-api.md) | What endpoints exist, who may call them, and what they return. |
| [Wire Schemas](schemas.md) | Every field of the request/response/controller models, and the Status enum. |
| [Redis Keys Reference](redis-keys.md) | Every key, channel and stream — type, writer, reader, TTL. |
| [Environment Variables](env-vars.md) | Every `NDIF_*` variable — default, reader, effect. |
| [Ports](ports.md) | What binds where, what compose publishes, what belongs public. |
| [Error Messages](error-messages.md) | What an exact error text means, whose fault it is, and whether to retry. |
| [Glossary](glossary.md) | What a term means, and which page covers it. |
| [External Resources](external-resources.md) | Where to go when the answer isn't in this repo. |

## Related

- [Concepts](../concepts/index.md) — what these fields and endpoints are *for*.
- [Developing](../developing/index.md) — the subsystems that read them.
