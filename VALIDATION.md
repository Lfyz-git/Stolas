# Validation record

Installer UX revision v0.3.0 (2026-10-09):

- Full local Windows/Python 3.11 suite: 123 scenarios, 104 passed and 19 explicit
  Linux/real-Docker/iperf3 skips. Shell syntax and example configuration checked.
- Added read-only Docker discovery tests for image/Compose identity, worker
  exclusion, multiple instances/networks, missing/remote/inaccessible Docker,
  missing/nonlocal/conflicting gateways, macvlan, host mode and occupied ports.
- Wizard tests cover automatically computed endpoints, seven-answer ordinary
  setup without gateway/URL input, confirmation, cancellation/back, interrupted
  draft recovery including secrets, repeat setup, stale topology and port races.
- Partial v0.2.0 checkout is distinguished from configured installation; n8n
  pending requests are not repeated and known credential IDs are reused.
- CI additionally creates two uniquely named isolated Docker networks and one
  disposable n8n-image Node container, validates actual Linux gateway discovery
  and authenticated HTTP from both namespaces, rejects wrong Bearer, and removes
  only these test resources. Existing containers/networks are not modified.
- Measurement agent code is unchanged. No access to Azazel, no public iperf load.
- Live deployment routing, reverse proxy and Telegram delivery remain checks for
  the deployment environment.

Pre-deployment audit update (2026-10-09):

- Windows / Python 3.11: full unittest suite passes; Linux-only bootstrap,
  POSIX permissions/lock and real iperf3 cases are explicitly skipped locally.
- Added regression coverage for empty/foreign directories, update and rollback,
  interrupted transactions, concurrent installation, topology validation,
  bounded WAN witnesses including stalled DNS, busy-port budgets, authenticated
  history summaries and all notification policies.
- Shell syntax and example configuration validated locally; diff whitespace checked.
- CI runs the entire Linux suite, embedded JavaScript, production Docker build,
  real loopback iperf3 in the nonroot production image, Compose validation and
  imports all three workflow policies in pinned n8n 2.42.6 (network disabled).
- Public iperf servers are never used by tests. A live fresh-host apt install,
  routing/firewall on the deployment host, live remote n8n execution, arm64
  execution and Telegram delivery still require deployment-environment checks.
- Azazel was not accessed or deployed. Source release v0.2.0 uses a release
  archive and SHA256SUMS; bootstrap checks integrity before extraction/execution.
- GitHub Actions run 37928662174 passed: 94 Linux tests (one iperf3 skip on
  the runner), all three workflows successfully imported by n8n 2.42.6,
  production Docker build and 94 image tests (41 installer-only skips;
  real iperf3 loopback passed), followed by Compose validation.
  https://github.com/Lfyz-git/Stolas/actions/runs/37928662174

Earlier validation records follow for provenance.

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

The initial GitHub Actions run succeeded after publication, including the Docker
build, production-image loopback test and Compose validation:
https://github.com/Lfyz-git/Stolas/actions/runs/37916667252

Interactive installer update (2026-10-09, Windows, Python 3.11):

- 11 installer tests passed locally: parameter prompts, configuration validation,
  atomic backups, secret handling, deferred n8n, first CLI cycle orchestration,
  unsuccessful measurements, and a real local HTTP test of n8n API provisioning.
- Linux shell-to-wizard-to-Compose integration uses a fake Docker executable in
  CI; skipped on Windows. It performs no package installation or public test.
- Shell syntax checked with Git Bash; CI repeats both shell syntax checks.
- Fresh-host apt/Docker installation, live n8n credentials/Telegram delivery and
  the initial speed test over a real deployment WAN still require a Linux host.

Server group update (2026-10-09):

- Added primary/additional/emergency groups with independently configured random
  or sequential selection and no configured limit on server count.
- Group tests cover 100 servers per group, legacy configuration migration,
  persistent round-robin across restarts/pruning, random fallback without repeats,
  group escalation, independent confirmation, periodic group sampling and deadline.
- Installer tests cover separate group prompts, random selection, 17 primary
  servers and empty secondary groups. Local suite: 54 passed, two Linux/iperf3
  integration tests skipped on Windows.

Archive bootstrap update (2026-10-09):

- Added bootstrap.sh for downloading GitHub source archives without Git, choosing
  installation path/ref and handing off to the existing interactive installer.
- Shell syntax checked locally with Git Bash. Existing 54 tests passed on Windows;
  eight bootstrap tests and two Linux/iperf3 tests require Linux CI.
- Offline bootstrap tests use a real pseudo-terminal and a local download stub:
  curl|sh input, paths with spaces, directory prompts, configure-only arguments,
  existing-install preservation, failed/corrupt downloads, unsafe archive members,
  missing TTY and propagation of the wizard's exit status.
- Anonymous raw/archive downloading requires the GitHub repository to be public.
