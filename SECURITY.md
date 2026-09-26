# Security policy

## Supported versions

Security fixes are released for the latest minor version of HighhX.

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Report them privately to
the maintainers (use the private vulnerability reporting of the hosting platform where
you obtained HighhX, or contact a maintainer directly) and include:

- the HighhX version (`highhx --version`) and operating system,
- steps to reproduce, and
- the impact you believe it has.

You should receive an acknowledgement within 5 working days. We will keep you
informed while a fix is prepared and credit you in the release notes unless you
prefer otherwise.

## Scope

Of particular interest:

- approval or policy bypasses (an action running without the approval its risk requires),
- secret values reaching terminal output, logs, history or reports,
- plugin code running without `plugins.allow_code`, without a per-user trust record, or after its files changed,
- path traversal or deletion outside the project in `clean`, `deps clean` or repairs.

See [docs/security.md](docs/security.md) for the security model.
