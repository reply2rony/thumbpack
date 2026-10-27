# Security Policy

## Supported versions

These repositories are starter / learning templates, published from the
`main` branch. Only the latest commit on `main` is supported with security
fixes. Older commits, forks, and copies are not maintained.

| Version | Supported |
| ------- | --------- |
| Latest commit on `main` | ✅ |
| Any older commit | ❌ |

## Reporting a vulnerability

**Please do not open a public issue for a security problem.** A public
issue tells everyone about the flaw before a fix exists.

Instead, use GitHub's private vulnerability reporting for the repository
(Repository → Security → Report a vulnerability), if it is enabled for
that repo. If it is not enabled, open a draft security advisory through
the same Security tab and keep the details private.

In your report, please include:

1. What the problem is, in plain words.
2. Which file(s) and line(s) are affected.
3. Steps to reproduce it, if you have them.
4. What you think the impact is (what an attacker could actually do).

You do not need to include a fix, but one is welcome.

## What happens next

- **Acknowledgement:** within 3 business days of your report.
- **Assessment:** the report is checked to confirm whether it is a real
  vulnerability in this repository's own code (not in a sample data file,
  and not in a third-party tool you run it with).
- **Fix:** if confirmed, a fix is made on `main` and the advisory is
  published once the fix is available.
- **Disclosure:** please do not disclose the vulnerability publicly until
  a fix is published, or 90 days after your report, whichever comes first.
  Coordinated disclosure protects everyone using the template.

## Scope — please read before reporting

- **In scope:** the code and templates in this repository itself
  (scripts, checklists, sample templates, configuration examples).
- **Out of scope:** sample / demo data (system names, results and dates in
  sample files are made up); vulnerabilities in Python, Git, your editor,
  or any other tool you use to run the templates; and problems in a fork
  or a copy that someone else has modified.
- **No real systems, no real credentials:** never put real employee data,
  real system names, real passwords, tokens, or API keys in a report, an
  issue, or a pull request for these repositories. If you accidentally
  do, rotate the credential first, then report.

## Security practices for contributors

- Never commit secrets, tokens, passwords, or private keys.
- Keep sample data clearly labelled as sample only.
- Do not claim production use, real-company results, or performance
  numbers that are not verified.
- Keep pull requests small and focused, so a security review is actually
  possible.

## How to reuse this template

Copy this file to `SECURITY.md` in the root of any repository under
`reply2rony`. To make it the default for every repository that does not
already have its own policy, put it in the special `reply2rony/.github`
repository instead — GitHub then uses it as the fallback policy for the
account's other repositories.

_Last updated: 2026-10-27. This template is generic on purpose: fill in
nothing account-specific before publishing, and do not add a personal
email address to a public SECURITY.md._
