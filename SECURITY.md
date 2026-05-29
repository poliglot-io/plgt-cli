# Security Policy

## Supported versions

Only the latest minor release of plgt receives security updates while the project is in alpha. Older versions should be considered unsupported.

## Reporting a vulnerability

**Please do not file public GitHub issues for security vulnerabilities.** Instead, use GitHub's [private security advisories](https://github.com/poliglot-io/plgt-cli/security/advisories/new) to report a vulnerability directly to the maintainers.

If you cannot use GitHub Security Advisories, email **security@poliglot.io** with:
- A description of the vulnerability and its impact
- Steps to reproduce (proof-of-concept code if applicable)
- The affected version(s)
- Your name / handle for credit (or note if you wish to remain anonymous)

We will acknowledge receipt within 2 business days and aim to provide an initial assessment within 5 business days.

## Disclosure process

1. You report the issue privately (see above).
2. We confirm the vulnerability and determine its severity.
3. We prepare a fix in a private branch and coordinate a release timeline with you.
4. We publish the fix as a patch release, credit the reporter in release notes (unless requested otherwise), and publish a GitHub Security Advisory.

## Scope

In-scope:
- This repository's published artifacts.
- Vulnerabilities that affect users of the published package.

Out of scope:
- Issues in upstream dependencies (please report those to the dependency maintainers; we will pick up patched versions promptly).
- Vulnerabilities in the broader Poliglot platform — those go to <https://poliglot.io/security>.
