---
name: Feature request
about: Suggest an idea for moray-aio
labels: enhancement
---

## Problem statement

What developer/ops pain point does this feature solve? (required — moray-aio only
ships features that reduce real operational pain.)

## Proposed solution

Describe the behavior you want. If it involves a new tool/probe, note that it must
be strictly read-only (see README "Safety by Design").

## Alternatives considered

Any workarounds or other designs you thought about.

## Scope check

- [ ] Does not violate the read-only / whitelist / approval-gate invariants
- [ ] Does not require executing dangerous commands (kill/restart/rm/del/format)
- [ ] Keeps Python 3.8 compatibility
