# Independent First-Run Checklist

Use one blank copy per evaluator. The goal is to see whether someone can follow the documentation from a clean checkout to a successful build without live coaching. Run against public or synthetic sample data only. Do not put names, client details, credentials, or completed interview notes in this repository.

## Before the session

- Choose one edition; record only `BigQuery` or `Snowflake`.
- Confirm the evaluator has their own development project/account and can authenticate.
- Start from a clean checkout and a new edition-local virtual environment/profile.
- Explain that the evaluator may stop, skip questions, and share no client data.

## Record the run

| Measure | Result |
|---|---|
| Edition | |
| Start time (credentials ready) | |
| Time `dbt debug` succeeds | |
| Time `dbt build` succeeds | |
| Build models passed / failed | |
| Data tests passed / failed | |
| Shared profile overwritten? | Yes / No |
| Maintainer help needed? | Yes / No; note the step, not personal details |
| Confusing or missing instruction | |
| Query/warehouse cost concern | |

## Pass criteria

The evaluator reaches a successful build and all declared tests using the docs alone, without overwriting a shared profile or receiving maintainer intervention. Target less than 30 minutes after cloud credentials are ready. Record time and failure points even when the run passes. Repeat with two independent evaluators before calling the first-run path validated.

Keep completed copies and contact information private; only commit documentation fixes that remove observed friction.
