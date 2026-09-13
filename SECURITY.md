# Security Policy

## Reporting a Vulnerability

We take the security of **Lightning Python Tools** seriously. If you discover a potential vulnerability or security issue affecting automated channel operations, key management, RPC communication, or API integrations, we encourage you to report it responsibly.

### How to Report

Please **do not** open a public issue for security vulnerabilities. Instead, report them via one of the following methods:

* **GitHub Security Advisory**: If enabled on the repository, submit a report directly via the [Security Advisories](https://github.com/TrezorHannes/Lightning-Python-Tools/security/advisories) tab.
* **Email**: Send details to `security@tunnelsats.com` or `hakuna@tunnelsats.com`.
* **Telegram**: Direct message an administrator or maintainer in our official community channel.
* **Nostr**: Send an encrypted direct message (NIP-04 / NIP-17) to `npub1n9z4y3xjramqes8fp9rl96x5e4nl0hff57ynw7vqnjpq370tq78sljsp8y`.

### What to Include in Your Report

To help us investigate and resolve the issue effectively, please include:

* A clear description of the vulnerability and its potential impact.
* The specific component, script, or endpoint affected (e.g. `Magma/magma_sale_process.py`, `LNDg/amboss_pull.py`, `Other/fee_adjuster.py`).
* Step-by-step instructions or minimal proof-of-concept to reproduce the behavior.
* The affected commit or release version.
* Any suggested mitigations or patches, if available.

### Our Commitment

* **Acknowledgment**: We will acknowledge receipt of your report within 48 hours.
* **Investigation & Triage**: We will investigate and coordinate with you on fix verification.
* **Responsible Release**: Patches will be developed, tested, and released to `main` before public disclosure.
* **Credit**: We appreciate responsible disclosure and will credit reporters in our release notes (unless anonymity is requested).

### Supported Versions

Security updates are applied to the latest code on the primary branch:

| Version / Branch | Supported |
| :--- | :--- |
| `main` | :white_check_mark: Active security support |
| Older commits | :warning: Not guaranteed; operators are advised to upgrade to `main` |

### Operator Security Guidelines

* Restrict configuration permissions: `chmod 600 config.ini`.
* Never commit secrets, bot tokens, or LND macaroons to version control.
* Use scoped LND macaroons and least-privilege credentials.
