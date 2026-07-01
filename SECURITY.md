# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in `duckduckgo-mcp-server`, please report
it **privately** so it can be fixed before public disclosure:

- Use GitHub's [private vulnerability reporting](https://github.com/nickclyde/duckduckgo-mcp-server/security/advisories/new)
  (**Security → Report a vulnerability**), or
- Email the maintainer at **nick@clyde.tech** with details and, ideally, a
  minimal reproduction.

Please do not open a public issue for undisclosed vulnerabilities.

We aim to acknowledge reports within a few days and to ship a fix or mitigation
as promptly as the severity warrants.

## Supported Versions

Security fixes are applied to the latest released version on
[PyPI](https://pypi.org/project/duckduckgo-mcp-server/). Please upgrade before
reporting to confirm the issue still reproduces.

## Security Model & Hardening Notes

This server fetches arbitrary web content on behalf of an MCP client, which
carries inherent risk. Operators should be aware of the following:

### Server-Side Request Forgery (SSRF)

The `fetch_content` tool takes a caller-supplied URL. By default the server
**refuses** requests whose host resolves to a non-public address — loopback
(`127.0.0.0/8`, `::1`), private/RFC1918 ranges, link-local (including the
`169.254.169.254` cloud metadata endpoint), reserved, multicast, and unspecified
addresses. Only `http` and `https` URLs are allowed, and **every redirect hop is
re-validated** so a public URL cannot bounce a request into the internal network.

For trusted local deployments that intentionally need to fetch internal hosts,
this guard can be disabled with either:

- the environment variable `DDG_ALLOW_PRIVATE_URLS=1`, or
- the CLI flag `--allow-private-urls`.

Leave the guard enabled if the MCP client is driven by untrusted input (e.g. an
LLM acting on web content).

> Note: validation resolves the host and then the HTTP client resolves it again
> to connect, leaving a small DNS-rebinding (TOCTOU) window. Default-deny plus
> per-hop validation blocks the practical SSRF vectors; do not expose this server
> to fully untrusted callers on a network where that residual risk matters.

### Untrusted Content

Search results and fetched page text come from external websites and are
**untrusted input**. Do not treat instructions embedded in that content as
commands (prompt injection). The tool descriptions note this explicitly.
