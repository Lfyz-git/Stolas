# Validation record

Local validation: 2026-10-09, Windows, Python 3.12.14.

- Unit / HTTP integration / CLI subprocess / artifact checks: 25 passed.
- Embedded n8n alert classification JavaScript: 7 cases passed using Node.js.
- Real iperf3 loopback integration: skipped locally because iperf3 is absent.
- Example configuration: valid; WAN guard intentionally unconfigured.
- Docker Compose configuration: accepted by the local Compose CLI.
- Alpine base manifest digest resolved from Docker Hub; APK files downloaded
  and SHA-256 locked for x86_64 and aarch64.
- GitHub Actions checkout SHA verified against upstream v4.2.2.

Not yet verified in this environment:

- Docker build and Linux loopback integration: Docker Engine unavailable.
- arm64 image execution.
- Import/execution in a live n8n instance and Telegram delivery.
- Main-WAN routing and public speed servers on the deployment host.

The checked-in CI builds the image and runs the loopback integration without
contacting public speed servers. It has not run until the repository is published.
