# Synthetic test fixtures

Runtime update and rollback tests stage the current checkout into temporary
installations with synthetic settings and tokens. No historical release archives
are required; migration from 0.4.0 and 0.5.0 is no longer supported.

`tools/legacy-v0.4.0.json` is production managed-file ownership metadata, not a
test archive. It remains because deployment code uses it to recognize owned
files; keeping it does not promise support for historical upgrades.

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
