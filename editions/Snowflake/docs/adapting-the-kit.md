# Adapting the kit

1. Replace `raw_customers.csv` and `raw_orders.csv` with synthetic fixtures that reflect your domain, or replace the seed references with declared warehouse sources.
2. Keep source cleanup and type casting in `models/staging/`; keep business logic and aggregates in `models/marts/`.
3. Add tests for key constraints and business rules in `models/schema.yml`. Add relationships tests when you have a defined orphan-record policy.
4. Set model owners, freshness expectations, and alert routing to match your team's operational needs.
5. Review the generated SQL and warehouse cost before deploying. Set sensible warehouse sizing and job schedules in your orchestrator.

## Troubleshooting

- **Profile not found:** ensure `DBT_PROFILES_DIR` points to the directory containing your local `profiles.yml` and that it has a `reliable_pipeline` profile key. If you keep profiles in an existing file such as `~/.dbt/profiles.yml`, merge the `reliable_pipeline` key from `kit/profiles.example.yml` into it; do not overwrite or copy over the existing file, which may hold other projects' profiles.
- **Missing environment variable:** export the required `SNOWFLAKE_*` value in the same shell that runs dbt.
- **Permission denied:** confirm the role can use the warehouse and create objects in the selected database and schema.
- **Seed parse/type issues:** verify CSV headers match the model column names and that dates and amounts use valid formats.
