# Thymble Server

The open-source, self-hostable server behind **Thymble**, a native iOS cooking app: import and store recipes, plan meals, and keep a smart shopping list that syncs with the lists you already use.

> **Status: early development.** Nothing here is ready to run yet. Watch the repo to follow along.

## What it will do
- **Recipe import** from the web page you're viewing, pasted text, or a photo of a recipe card
- **Meal planning** for a household, shared across devices
- **A structured shopping list** that merges quantities, groups by store aisle, and works offline
- **List connectors** that mirror the list to Home Assistant (and through it, Alexa)
- **Retailer connectors** that send the list to grocery carts
- **Learning** from your corrections and choices, so it gets better with use
- **Bring your own LLM:** a local model through any OpenAI-compatible endpoint (e.g. Ollama), Anthropic's Claude, or both, routed per task

## Planned stack
Python / FastAPI · PostgreSQL · PowerSync (offline sync to the app) · Redis · Docker Compose

## Self-hosting
Thymble is designed to run on a home server. Docker Compose files and setup docs will land here as the server takes shape.

## Contributing
See [CONTRIBUTING.md](CONTRIBUTING.md). Contributions are signed off under the [Developer Certificate of Origin](https://developercertificate.org/).

## Security
Please report vulnerabilities privately; see [SECURITY.md](SECURITY.md).

## License
[GNU Affero General Public License v3.0](LICENSE). If you run a modified version of this server as a network service, you must make your modified source available to its users.
