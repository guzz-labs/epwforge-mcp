#!/usr/bin/env node
// This npm entry is a placeholder name-squat — the functional EPWForge MCP
// server is the Python package on PyPI. Exit non-zero so automation that
// invokes `npx epwforge-mcp` (e.g. a misconfigured MCP client) treats this
// as the failure it is, rather than a silent no-op (QC review 2026-06-09 P2-6).
console.error(
  "epwforge-mcp (npm): this package is a placeholder.\n" +
  "The functional EPWForge MCP server is a Python package.\n" +
  "Run it with:    uvx epwforge-mcp\n" +
  "Or install via: pip install epwforge-mcp\n" +
  "Docs: https://epwforge.com  ·  https://pypi.org/project/epwforge-mcp/"
);
process.exit(1);
