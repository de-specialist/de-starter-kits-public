# BigQuery architecture

This diagram shows how the BigQuery edition fits together, based on the files in `editions/Bigquery/`, `migration/`, `scripts/`, and the edition's CI workflows. Dashed lines are optional or manually triggered paths. Terms are defined in the [glossary](../../../docs/glossary.md).

```mermaid
flowchart LR
  subgraph SRC[Sources]
    TL["TheLook public dataset<br/>order_items, products, users"]
  end

  subgraph DBT["dbt project retail_analytics"]
    subgraph STG["Staging (views)"]
      S1[stg_order_items]
      S2[stg_products]
      S3[stg_users]
    end
    subgraph MRT["Marts (tables)"]
      M1["mart_daily_product_sales<br/>partitioned by date, clustered by product"]
      M2[mart_customer_value]
      M3["mart_daily_product_sales_incremental<br/>off by default"]
    end
    T["Schema, grain, and unit tests"]
  end

  TL --> S1 & S2 & S3
  S1 & S2 --> M1
  S1 & S3 --> M2
  S1 & S2 -.-> M3
  MRT --- T
  MRT -.-> LS["Looker Studio report<br/>(guide only)"]

  subgraph MIG["Optional migration (target migration)"]
    IN["Local CSV, SQLite, or PostgreSQL"] --> LD["migration/run_migration.py<br/>dlt full replace"]
    LD --> BR["Bronze dataset<br/>MIGRATION_BRONZE_NAMESPACE"]
    BR --> SV["Silver views<br/>silver_customers, silver_orders,<br/>silver_products, silver_order_items"]
    SV --> GD["Gold tables<br/>mart_migration_customer_value,<br/>mart_migration_daily_product_sales"]
  end

  subgraph CI["CI: dbt-bigquery-ci.yml"]
    PR["Pull request: dbt parse<br/>placeholder .ci profile, no credentials"]
    WD["Manual dispatch on main:<br/>dbt build --target ci"]
  end
  PR -.-> DBT
  WD -.->|"workload identity federation"| DBT
  TF["infra/terraform<br/>dataset, CI service account, WIF"] -.-> WD

  subgraph DOCS["Docs: docs-site.yml"]
    DG["dbt docs generate<br/>--empty-catalog --static"] --> MK["MkDocs build --strict"]
    MK --> PG[Public docs site]
  end
  DBT -.-> DG
```

## How to read it

- **Sources to marts.** Staging models read the three TheLook tables in place and standardize names, types, and statuses. Marts aggregate staged rows; see the [mart contract](../../../docs/mart-contract.md) for each grain and metric.
- **Incremental example.** `mart_daily_product_sales_incremental` builds only when `enable_incremental_example` is true. It rebuilds a configurable lookback window of date partitions with `insert_overwrite`; see the [overview](../README.md).
- **Tests.** `models/schema.yml` declares column tests, and `tests/` holds singular grain tests for the daily marts.
- **Migration.** The Silver and Gold models are enabled only for the `migration` target with `enable_migration_models: true`. They read the Bronze dataset that the [migration loader](../../../migration/README.md) writes. The full warehouse migration flow has not been validated end to end.
- **CI.** Pull requests run a credential-free `dbt parse`, including the migration models. A warehouse build runs only from a manual dispatch on `main`, against a `starterkit_`-prefixed dataset that the [Terraform module](../infra/terraform/README.md) provisions.
- **Docs.** `scripts/build_docs_site.sh` generates dbt docs without warehouse access and builds this site. Column metadata is empty in published builds.
