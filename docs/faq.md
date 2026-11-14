# FAQ

## Is this open source?

No. This is publicly readable source under the evaluation terms in [LICENSE](../LICENSE).
Production use and redistribution require the rights described there.

## Does CI access my cloud account?

No. Public CI uses synthetic fixtures and placeholder profiles. Configure your
own account locally or in a separate private deployment repository.

## Does a successful parse prove the models execute?

No. `dbt parse` checks project structure without warehouse execution. Validate
execution against your own isolated target with appropriate cost controls.

## Can I include my own data?

Keep data, target identifiers, mappings and credentials outside the public
repository. Add only clearly labeled synthetic test fixtures to source control.
