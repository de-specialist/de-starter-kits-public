# Commerce Source Selection Worksheet

Do not choose a source connector from assumptions. Complete this worksheet from the first five interviews, then confirm the strongest repeated source pattern with follow-up buyers before building a connector.

| Candidate source | Recent projects mentioning it | Buyer can choose connector? | Existing workaround | API/export availability | Update/delete semantics | Cost and access limits | Sample data safe to use? |
|---|---:|---|---|---|---|---|---|
| Shopify | | | | | | | |
| Point-of-sale export | | | | | | | |
| Other source (name only, no client details) | | | | | | | |

## Selection gate

Prioritize a connector only if at least three qualified buyers independently cite the same source and describe material setup work around it. Prefer a documented export/API with synthetic fixtures, stable identifiers, a reliable update timestamp, and clear deletion/late-arrival behavior. Record licensing, API rate limits, regional handling, and expected ongoing maintenance.

Before implementation, write down the source table grain, field contract, pagination/backfill approach, deduplication key, update/delete policy, and a local fixture. Do not collect client credentials or data during discovery. The connector's first integration should run only in the customer's own environment with least-privilege credentials.

No commerce source beyond the existing public BigQuery TheLook example has been selected. Shopify and POS are research prompts, not product commitments.
