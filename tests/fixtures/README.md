# Synthetic test fixtures

`v0.4.0.tar.gz` and `v0.5.0.tar.gz` contain sanitized historical source code
for migration and rollback tests. They are not byte-for-byte release downloads.
Operational validation notes and old release notes were removed or replaced;
host names and sample addresses were anonymized, including nested archives.
Archive ownership and timestamps are normalized. Historical runtime versions
and migration behavior remain intact. These archives contain no deployment state.

`tools/legacy-v0.4.0.json` retains the original source-file hashes so the installer
can recognize files owned by genuine old installations. These hashes are not
credentials. Sanitized fixtures use an explicit managed-file manifest.

`localhost-test-cert.pem` and `localhost-test-key.pem` are a public, test-only TLS
pair for an ephemeral loopback HTTPS server. The key is intentionally public;
never install it on a real server. The publication guard exempts only its exact
SHA-256 fingerprint. No other private key is permitted.

LAN, Docker and VPN addresses in unit tests are invented protocol fixtures.
They exercise address classification and network discovery with mocked commands.
Public DNS is used only as a negative address-validation case. Speed samples,
node names, credentials, IDs and timestamps are synthetic. URL credentials in
two negative validation tests are exact placeholder strings on example domains.
Never replace fixtures with copied production output, settings or tokens.
