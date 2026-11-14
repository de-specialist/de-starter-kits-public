# Public CI security

All jobs run on GitHub-hosted Ubuntu runners. Default workflow permissions are
read-only. CI runs synthetic migration tests, static checks, hash-lock checks,
credential-free dbt parsing and documentation generation. Cloud credentials,
warehouse builds, automatic merge logic and hosting deployment are not included.

Use a separate private repository for real deployment. Do not connect workstation
runners to public pull requests, use `pull_request_target` to execute pull-request
code, or make privileged deployment credentials available to contributed code.

Review full action commit pins and lockfile changes. Require passing checks and
review on main. Secret scanners supplement review; they do not prove that a file
is safe to publish. Keep all customer and local configuration outside Git.
