# Security policy

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on this repository, with steps to reproduce and the version or commit you
tested. You should get an answer within a week.

## Supported versions

Only the latest release on `main` receives fixes.

## What this project does not do

This is a matching engine and its gateway, not an exchange. Before putting it
anywhere reachable from the internet, know that the gateway has:

- **No authentication.** The `account` in a request is trusted as given; any
  client can place or cancel orders for any account. A real deployment puts
  an authenticating proxy or an auth layer in front and derives the account
  from the credential, never from the request body.
- **No risk checks or balances.** Nothing stops an account from buying what
  it cannot pay for; that belongs to a ledger/risk service in front of the
  sequencer.
- **No rate limiting** beyond a bounded queue per market, which returns 503
  when full rather than growing without bound.
- **No TLS.** Terminate it at a proxy.

The WAL and snapshot files are checksummed against corruption, not signed
against tampering: anyone who can write to the data directory can rewrite
history. Protect it with filesystem permissions.
