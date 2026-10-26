# MoRay AIOps — Read-Only AIOps Agent Skill (MCP Server)

An event-driven, physically-sandboxed, local-LLM powered AIOps agent: it
watches your logs in real time, triggers analysis on error bursts / critical
keywords / error spikes, runs a *strictly read-only* investigation loop
(log grep + system probes + AST sandbox) driven by a local LLM (Ollama, with
heuristic fallback), and pushes conclusions to your terminal, webhook or file.

All read-only tools (log tools, system probes, AST sandbox) are also exposed
as a standard **Model Context Protocol (MCP)** server (JSON-RPC 2.0 over
stdio), so any MCP client — Claude Desktop, Cursor, or your own agent — can
plug them in with one line of config.

- Repository: <https://github.com/aliangmo2026/moray-aio>
- Runtime: Python 3.8+, local Ollama optional (service never depends on it)
- Safety: read-only by design — it never writes, truncates, restarts or
  deletes anything; dangerous actions only ever appear as suggestion text.

## ⚖️ License & Usage Terms

**Author**: Mo Guangliang (莫光良)

This project is licensed under the **Apache 2.0 License with Commons Clause**.

We believe in open-source and knowledge sharing. You are highly encouraged to:
*   ✅ **Learn & Inspect**: Read the source code to understand the architecture.
*   ✅ **Local Testing & Personal Use**: Run, modify, and test it locally for non-commercial purposes.
*   ✅ **Free Listing**: Publish it as a free skill on platforms like Agensi.

**🚫 STRICTLY PROHIBITED (Commercial Restrictions):**
Without explicit written permission from the author, you are **NOT** allowed to:
*   ❌ Package this project or its derivatives as a product/service.
*   ❌ Sell, resell, or distribute this software for any commercial fee.
*   ❌ List it as a paid/premium skill on any marketplace (e.g., Agensi, SaaS platforms).

For commercial licensing, enterprise integration, or custom consulting, please contact the author directly.
