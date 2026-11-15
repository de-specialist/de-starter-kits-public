# Security policy

## Reporting a vulnerability

Report suspected security problems privately. Do not open a public issue, pull request, or discussion.

- **Preferred channel:** GitHub private vulnerability reporting on this repository (**Security → Report a vulnerability**). It is enabled for this repository.
- Include the affected edition, file or component, steps to reproduce, and the impact you expect.
- Never include real credentials, `profiles.yml`, `.env` files, or customer data. Redact secrets and use placeholder values.

## What to expect

Reports are handled on a best-effort basis, with no response or fix-time commitment (see [SUPPORT.md](SUPPORT.md) and [LICENSE](LICENSE)). Please give the owner a reasonable chance to fix a problem before disclosing it publicly.

## Scope

In scope: code, SQL, scripts, CI workflows, and Terraform in this repository and in edition bundles built from it. Out of scope: vulnerabilities in third-party dependencies (report them upstream), your own warehouse or cloud configuration, and Google's public datasets.

## Public execution and access boundaries

The repository's public documentation is generated as a static site. The repository does not provide a browser feature that executes commands on a visitor's computer or connects to the visitor's cloud account. Terraform examples require an operator to run Terraform locally with their own credentials; the repository does not run `apply` or `destroy` from a web request or CI job.

GitHub Actions workflows use GitHub-hosted runners. Pull request workflows use the `pull_request` event, grant only read access to repository contents, and do not reference repository secrets or request an OIDC token. Checkout credentials are not retained in Git configuration. No self-hosted runner or remote-control workflow is configured for this repository. As of 2026-11-15, the repository has no configured Actions secrets, variables, or self-hosted runners. GitHub Actions is restricted to the six action publishers and repositories currently used by checked-in workflows; other publishers and unlisted actions are blocked. These controls reduce exposure from untrusted pull request code; changes to workflows, permissions, runners, or cloud credentials require a fresh review.

The BigQuery Terraform example contains an optional Workload Identity Federation configuration, restricted to a manual workflow dispatch on `main` with immutable repository and owner IDs. It provisions no service-account key. The current CI workflows do not request `id-token: write` or authenticate to Google Cloud, so the example does not currently provide a cloud execution path from CI. Applying the Terraform module is an explicit operator action and can create cloud resources in the operator-selected project; inspect the Terraform plan and scope before applying it.
