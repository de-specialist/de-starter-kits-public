# Security policy

## Reporting a vulnerability

Report suspected security problems privately. Do not open a public issue, pull request, or discussion.

- **Preferred channel:** GitHub private vulnerability reporting on this repository (**Security → Report a vulnerability**). _Owner action: enable it under Settings → Code security → Private vulnerability reporting, or replace this line with a security contact address._
- Include the affected edition, file or component, steps to reproduce, and the impact you expect.
- Never include real credentials, `profiles.yml`, `.env` files, or customer data. Redact secrets and use placeholder values.

## What to expect

Reports are handled on a best-effort basis, with no response or fix-time commitment (see [SUPPORT.md](SUPPORT.md) and [LICENSE](LICENSE)). Please give the owner a reasonable chance to fix a problem before disclosing it publicly.

## Scope

In scope: code, SQL, scripts, CI workflows, and Terraform in this repository and in edition bundles built from it. Out of scope: vulnerabilities in third-party dependencies (report them upstream), your own warehouse or cloud configuration, and Google's public datasets.
