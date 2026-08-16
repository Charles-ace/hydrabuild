# Running the local HydraDB node (verified config)

Verified 2026-08-16 against `ghcr.io/hydra-db/hydradb:latest` (v0.1.0) on
Windows + Docker Desktop. The published image now **requires** the full
environment set below; a bare `docker run` exits immediately with:

```
Error: Custom { kind: InvalidInput, error: "graph runtime requires
GRAPH_TLS_CERTIFICATE and GRAPH_TLS_PRIVATE_KEY unless
GRAPH_ALLOW_PLAINTEXT=true" }
```

and, once plaintext is enabled, with:

```
invalid environment variable CLOUD_PROVIDER value `null`
```

(`null` means *absent*, not the string — `CLOUD_PROVIDER=local` also needs
`LOCAL_PATH` pointing at a directory that already exists.)

## Working run

```powershell
# from the repo root
New-Item -ItemType Directory -Force -Path hydradb-data\store, hydradb-data\cache | Out-Null
'local-development-token-32-bytes' | Set-Content -NoNewline hydradb-data\auth-token

docker run -d --name hydra-node -p 7687:7687 -p 8443:8443 -p 9090:9090 `
  -v "$PWD/hydradb-data:/data" `
  -e CLOUD_PROVIDER=local -e LOCAL_PATH=/data/store `
  -e GRAPH_NAMESPACE=default -e GRAPH_ID=default -e GRAPH_CELL_ID=cell-0 `
  -e GRAPH_CELLS=cell-0 -e GRAPH_NODE_ID=node-0 `
  -e "GRAPH_BOLT_NODE_ADDRESSES=node-0=127.0.0.1:7687" `
  -e GRAPH_ADVERTISED_BOLT_ADDR=127.0.0.1:7687 `
  -e GRAPH_DATA_CACHE_DIR=/data/cache -e GRAPH_AUTH_TOKEN_FILE=/data/auth-token `
  -e GRAPH_ALLOW_PLAINTEXT=true -e RUST_MIN_STACK=33554432 `
  ghcr.io/hydra-db/hydradb:latest
```

Notes:

- The mount target is `/data` (not `/hydradb-data`). `LOCAL_PATH` must point
  at a directory that exists before the container starts, hence the
  `New-Item` step.
- `RUST_MIN_STACK=33554432` is required or the node builds, serves `/readyz`,
  then aborts with a stack overflow on the first query.
- Ports: Bolt `7687`, HTTP query API `8443`, admin/`readyz`/`/metrics` `9090`.
- The auth token file content must match `HYDRA_AUTH_TOKEN` used by the Python
  client (default `local-development-token-32-bytes`).
- Wait for the Bolt listener with
  `.venv\Scripts\python -c "from neo4j import GraphDatabase; d=GraphDatabase.driver('bolt://127.0.0.1:7687', auth=('neo4j','local-development-token-32-bytes')); d.verify_connectivity(); d.close()"`.

## Fresh-store reset (when `DETACH DELETE` starts timing out)

As the WAL grows, `MATCH (n:Label) DETACH DELETE n` degrades and eventually
exceeds the node's 30-second query timeout (`client_query_runtime exceeded
query timeout after 29999 ms`), and the mutation engine rejects
`WITH/LIMIT/RETURN` shapes after deletes. The reliable reset is a fresh store:

```powershell
docker stop hydra-node; docker rm hydra-node
Remove-Item -Recurse -Force hydradb-data\store, hydradb-data\cache
New-Item -ItemType Directory -Force -Path hydradb-data\store, hydradb-data\cache | Out-Null
# then re-run the docker run command above
```