# DISCLAIMER

## ⚠️ WARNING: EXPERIMENTAL PROOF-OF-CONCEPT

**moray-aio** is an experimental proof-of-concept of an event-driven, local-LLM
powered AIOps agent. It is published for research, education and evaluation
purposes only.

**DO NOT USE THIS SOFTWARE DIRECTLY IN CORE PRODUCTION ENVIRONMENTS.**

## 1. No warranty

This software is provided "AS IS", without warranty of any kind, express or
implied, including but not limited to the warranties of merchantability, fitness
for a particular purpose and noninfringement. In no event shall the authors or
copyright holders be liable for any claim, damages or other liability, whether
in an action of contract, tort or otherwise, arising from, out of or in
connection with the software or the use or other dealings in the software.
(This disclaimer supplements, and does not replace, the Apache License 2.0
with Commons Clause shipped with this project. See the LICENSE file for the
full terms, including the commercial-use restrictions of the Commons Clause.)

## 2. Safety principle: physically read-only, never execute

The system is designed around a hard "physically read-only, never execute"
principle:

- log files are opened strictly in read-only binary mode (`rb`); the engine
  never writes, truncates, renames or deletes watched logs;
- all tool inputs (paths, hosts, service names) are validated against
  operator-configured whitelists (fail-closed);
- dangerous operations (kill / reboot / restart / shutdown / rm / del / format
  and similar) are **never executed** — they may only ever appear as suggestion
  text inside analysis conclusions;
- LLM-proposed actions pass an approval gate; token budgets, rate limits and
  API-key authentication are enforced by default.

Despite these defenses, no software is bug-free. Safety mechanisms may fail,
be misconfigured, or be bypassed. **The operators deploying this software remain
fully responsible for verifying that the deployment is safe for their
environment.**

## 3. No liability for failures

The authors assume **no responsibility or liability for any failures, outages,
data loss, security incidents or other damage — direct or indirect — caused by
or related to the use of this software**, including but not limited to:

- misconfiguration of whitelists, API keys, rate limits or watch rules;
- analysis conclusions followed by humans or automation (all conclusions are
  advisory only and must be reviewed by qualified personnel);
- interactions with monitored systems, log pipelines or third-party services;
- behavior of the local LLM (Ollama) or any plugin loaded from plugin
  directories.

## 4. Data sensitivity

moray-aio reads log files that may contain secrets, personal data or other
sensitive information, and may forward excerpts of those logs to a local LLM,
webhooks or alert files. **Operators are responsible for ensuring they are
allowed to process the monitored logs and for redacting or protecting sensitive
data before forwarding it anywhere.**

## 5. Plugins and third-party code

Plugins loaded via `MORAY_AIO_PLUGIN_DIRS` are arbitrary Python code executed
inside the engine process. Only load plugins from trusted sources. The authors
take no responsibility for third-party plugins.

---

By using this software you acknowledge that you have read, understood and agreed
to this disclaimer.
