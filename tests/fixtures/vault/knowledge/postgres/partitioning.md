---
title: Postgres Partitioning
tags: [knowledge/postgres, scaling]
---
# Postgres Partitioning
Range-partition by created_at for time-series ingest. Detach-and-archive beats DELETE for retention. Shard by tenant once tables grow.
