# Security Policy

## Supported configuration

Security fixes are made against:

- The latest `main` branch of this repository
- Python 3.11+
- The frontend built from current source with the pinned toolchain (see the README's Quick start)

Older revisions and third-party forks are not supported.

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report vulnerabilities privately using GitHub Security Advisories: open this repository on GitHub, go to the **Security** tab, and use the **Report a vulnerability** button under Advisories. This keeps the details out of public view until a fix is available.

## Response timeline

- Acknowledgment: within a few days of your report.
- Fix cadence: best-effort, prioritized by severity. We will keep you informed of progress through the advisory thread and credit you in the release notes if you wish.

## What belongs in a report

To make reports actionable, please include:

1. **Affected component** — e.g. REST API endpoint, MCP tool, service worker, export/migration CLI.
2. **Reproduction steps** — minimal, concrete steps (requests, payloads, configuration) that trigger the issue.
3. **Impact** — what an attacker could gain or damage, and any preconditions required.
