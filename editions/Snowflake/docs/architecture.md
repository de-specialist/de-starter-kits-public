# Snowflake architecture

This diagram shows how the Snowflake edition fits together, based on the files in `editions/Snowflake/`, `migration/`, `scripts/`, and the edition's CI workflows. Dashed lines are optional or conditional paths. Terms are defined in the [glossary](../../../docs/glossary.md).

```mermaid
flowchart LR
  subgraph SRC["Sources (synthetic seeds)"]
    R1[raw_customers.csv]
    R2[raw_orders.csv]
  end

  subgraph DBT["dbt project reliable_pipeline (kit/)"]
    subgraph STG["Staging (views)"]
      S1[stg_customers]
      S2[stg_orders]
    end
    subgraph MRT["Marts (tables)"]
      M1[fct_customer_orders]
      M2["fct_orders_incremental<br/>(incremental, off by default)"]
    end
    T[Schema and singular tests]
  end

  R1 -->|dbt seed| S1
  R2 -->|dbt seed| S2
  S1 & S2 --> M1
  S2 -.->|"enable_incremental_example: true"| M2
  MRT --- T

  subgraph MIG["Optional migration (target migration)"]
    IN["Local CSV, SQLite, or PostgreSQL"] --> LD["migration/run_migration.py<br/>dlt full replace"]
    LD --> BR["Bronze schema<br/>MIGRATION_BRONZE_NAMESPACE"]
    BR --> SV["Silver views<br/>silver_customers, silver_orders,<br/>silver_products, silver_order_items"]
    SV --> GD["Gold tables<br/>mart_migration_customer_value,<br/>mart_migration_daily_product_sales"]
  end

  subgraph CI["CI: dbt-snowflake-ci.yml"]
    PR["Pull request: dbt parse<br/>placeholder .ci profile, no credentials"]
    PM["Push to main: dbt build --target ci<br/>fails until repository secrets and<br/>a DBT_KIT_* target are configured"]
  end
  PR -.-> DBT
  PM -.->|"DBT_KIT_DEV or DBT_KIT_CI names only"| DBT
  TF["infra/terraform<br/>disposable database, schema,<br/>X-Small warehouse"] -.-> PM

  subgraph DOCS["Docs: docs-site.yml"]
    DG["dbt docs generate<br/>--empty-catalog --static"] --> MK["MkDocs build --strict"]
    MK --> PG[Public docs site]
  end
  DBT -.-> DG
```

## How to read it

- **Sources to marts.** `dbt seed` loads two synthetic CSV files: three customers and six orders, with no product or item rows. Staging standardizes them and `fct_customer_orders` aggregates one row per customer; see the [mart contract](../../../docs/mart-contract.md).
- **Incremental example.** `fct_orders_incremental` reads `stg_orders` and is disabled unless you set the `enable_incremental_example: true` variable, so it is drawn with a dashed line. It reprocesses a short lookback window on each run; the [operations guide](operations.md) describes how to run and full-refresh it.
- **Migration.** The Silver and Gold models are enabled only for the `migration` target with `enable_migration_models: true`. They read the Bronze schema that the [migration loader](../../../migration/README.md) writes; see [Snowflake migration](migration.md). The full warehouse migration flow has not been validated end to end.
- **CI.** Pull requests run a credential-free `dbt parse`, including the migration models. Pushes to `main` that touch the edition run `dbt build`; the job fails until the repository secrets are configured and the database and schema match the Terraform naming rule. The [Terraform module](../infra/terraform/README.md) creates that disposable target.
- **Docs.** `scripts/build_docs_site.sh` generates dbt docs without warehouse access and builds this site. Column metadata is empty in published builds.
