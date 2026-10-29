# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue for security problems.

Report them privately through GitHub: go to the **Security** tab of this repository and choose **Report a vulnerability**. Include what you found, how to reproduce it, and the impact you expect.

You can expect an acknowledgement within a few days. We'll keep you updated while we work on a fix and credit you in the release notes unless you'd rather stay anonymous.

## Supported versions

Thymble Server is in early development and has no releases yet. Once releases begin, security fixes will go to the latest release.

## Scope notes for self-hosters

- Never commit real secrets. Configuration comes from environment variables; `.env.example` lists them with placeholder values only.
- Connector credentials (Home Assistant tokens, retailer tokens) are encrypted at rest by the server.
