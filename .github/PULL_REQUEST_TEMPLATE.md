## Summary

One or two sentences: what does this PR do and why?

## Related issues

Closes #<issue-number> (if applicable).

## Changes

- ...

## Safety review (required)

- [ ] No writes to watched logs (read-only `rb` access only)
- [ ] Path/host inputs validated against whitelists (`_check_path`, probe host allowlist)
- [ ] No dangerous commands executed; suggestions only in conclusions
- [ ] No credential literals introduced (secrets come from environment variables)
- [ ] SQL stays fully parameterized
- [ ] Outbound URLs validated (scheme + host allowlist; no loopback/private/reserved for webhooks)

## Testing

- [ ] `ruff check .` passes with 0 warnings
- [ ] `pytest -q` fully green (new behavior covered by tests)
- [ ] Updated README / docs where relevant
