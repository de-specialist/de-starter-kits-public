## Change summary

<!-- Describe the user-facing or maintainer-facing change. -->

## Scope

- [ ] BigQuery edition
- [ ] Snowflake edition
- [ ] Shared scripts or documentation
- [ ] CI or release process

## Validation

- [ ] Repository script tests (`python -m unittest discover -s scripts/tests -v`)
- [ ] BigQuery credential-free parse or warehouse build (state which)
- [ ] Snowflake credential-free parse or warehouse build (state which)
- [ ] `git diff --check`
- [ ] Not run (explain below)

**Commands and outcomes:**

## Data, access, and cost impact

- [ ] No credentials, local profiles, private customer data, or account identifiers added
- [ ] Warehouse target and expected cost reviewed for any warehouse-run change
- [ ] Source data terms and attribution reviewed if data sources changed

## Notes for reviewers

<!-- Link issue/evidence; include screenshots only when relevant and scrub account details. -->
