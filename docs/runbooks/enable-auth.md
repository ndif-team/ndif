---
title: Enable API-Key Auth
one_liner: Turn on Postgres-backed API-key verification on the compose stack, keep the keys across `just down`, and lock the dashboard — until you do, every caller's Python runs in-process next to the weights.
tags: [runbook, operating, auth, api, sandbox, dashboard]
related: [docs/concepts/auth-and-limits.md, docs/concepts/sandbox-execution.md, docs/operating/production.md, docs/operating/dashboard.md, docs/operating/compose-stack.md, docs/developing/api-service.md, docs/reference/env-vars.md, docs/reference/http-api.md, docs/runbooks/trace-a-users-failed-job.md]
sources: [src/ndif/services/api/auth.py, src/ndif/common/providers/postgres.py, src/ndif/services/api/app.py, src/ndif/services/ray/sandbox/model.py, src/ndif/services/ray/sandbox/nns.py, src/ndif/services/api/queue/processor.py, src/ndif/services/ray/deployments/controller/cluster/deployment.py, src/ndif/services/dashboard/backend/auth.py, src/ndif/services/dashboard/backend/config.py, src/ndif/services/dashboard/backend/routers/deployments.py, docker/postgres/init.sql, docker/docker-compose.yml, justfile]
---

# Enable API-Key Auth

## What this covers

Switching an NDIF from "anyone who can reach port 8001" to "a key in a database",
keeping those keys across a stack teardown, and the dashboard's separate login.
Commands are for the compose stack from a checkout; `just` wraps
`docker compose -f docker/docker-compose.yml`.

## Why this is not optional

`NDIF_POSTGRES_URL` decides how user code runs, not only who may submit. With it
unset, `verify_api_key` returns `None` and `validate_request` stamps
`trusted=True` on every request whose envelope doesn't set `trusted` itself
(`src/ndif/services/api/auth.py` `validate_request`). A trusted request runs its
Python **inside the model actor process**, next to the weights, and a model it
auto-deploys loads with `trust_remote_code=True`. The full fork is in
[Auth and limits](../concepts/auth-and-limits.md).

## 1. Bring up Postgres

The compose file already defines the service:

```bash
just up postgres
docker compose -f docker/docker-compose.yml exec postgres pg_isready -U admin -d ndif
docker compose -f docker/docker-compose.yml exec postgres psql -U admin -d ndif -c '\dt'
```

`docker/postgres/init.sql` is mounted into `/docker-entrypoint-initdb.d/`, so it
runs **once, on an empty data directory**. Expect nine tables: `users`,
`profiles`, `user_tags`, `keys`, `key_user_tag_assignments`, `models`,
`model_tags`, `model_tag_assignments`, `audit_logs`.

### Where the keys live, and what destroys them

The `postgres` service mounts no volume at `/var/lib/postgresql/data`, so the
data sits in an **anonymous** Docker volume tied to the container:

| Command | Keys |
|---|---|
| `just restart`, `just up` (including a recreate after a config change) | kept — compose carries an anonymous volume across a recreate |
| `just down` | **lost** — the container goes, and the next `up` creates a fresh empty volume, so `init.sql` runs again |
| `just ta` | **lost** — it runs `just down` first |

Pick one of these before you issue keys you care about:

- **Give Postgres a named volume.** In `docker/docker-compose.yml`, add
  `- pgdata:/var/lib/postgresql/data` to the `postgres` service's `volumes:` and
  `pgdata:` under the top-level `volumes:` (next to `dashboard_data`). Data then
  survives `just down` and `just ta`; only `just down -v` removes it.
- **Dump and restore around every teardown:**

  ```bash
  # before `just down` / `just ta`
  docker compose -f docker/docker-compose.yml exec -T postgres \
    pg_dump -U admin -d ndif --data-only > ndif-auth.sql
  # after the next `just up`, once pg_isready passes (init.sql has rebuilt the schema)
  docker compose -f docker/docker-compose.yml exec -T postgres \
    psql -U admin -d ndif < ndif-auth.sql
  ```

- **Rebuild without tearing down.** After a code change, `just build` then
  `just up` replaces the changed service containers and leaves `postgres` alone.

## 2. Create the two tags the server acts on

`init.sql` seeds no users, keys or tags. The API runs one query per key
(`src/ndif/services/api/auth.py` `_KEY_QUERY`): a key is valid if its row exists in
`keys`, and its `user_tags` come back through `key_user_tag_assignments`. Key
issuance is normally the account portal's job; here you do it by hand.

| Tag | Constant | Effect |
|---|---|---|
| `trusted` | `TRUSTED_TAG` | the request runs in-process instead of in a runner, and any model it triggers a deploy for loads with `trust_remote_code` |
| `priority` | `PRIORITY_TAG` | the request is served ahead of untagged traffic for that model, FIFO among other priority requests (`src/ndif/services/api/queue/request_queue.py` `RequestQueue.rank`) |

Every other tag name is inert.

```bash
docker compose -f docker/docker-compose.yml exec postgres psql -U admin -d ndif -c "
INSERT INTO user_tags (name, description)
VALUES ('trusted',  'code runs in the model process'),
       ('priority', 'served ahead of untagged traffic');"
```

## 3. Point the API at it

Uncomment `NDIF_POSTGRES_URL` in the `api` service's `environment:` in
`docker/docker-compose.yml`:

```yaml
      NDIF_POSTGRES_URL: postgresql://ndifapi:admin@postgres:5432/ndif
```

`ndifapi` is a read-only role; `login_page` is the account-portal role with
writes on `keys` and `users`. Both passwords in `init.sql` are `admin` — change
them for anything real.

Then **recreate** the container — an environment edit only takes effect on a new
container:

```bash
just up api
docker compose -f docker/docker-compose.yml exec api env | grep NDIF_POSTGRES_URL
```

`just up api` runs `docker compose up -d api`, which sees the changed config and
replaces the container. **`just restart api` does not**: `docker compose restart`
restarts the existing container with its old environment, so auth stays off with
no error. The second command must print the URL; if it prints nothing, auth is
off.

The connection pool is created on the first keyed request, not at boot, so
`Postgres connected` appears in `docker compose -f docker/docker-compose.yml logs api`
only after step 5 (`src/ndif/common/providers/postgres.py` `PostgresProvider.connect`).

> **Gotcha:** with `NDIF_POSTGRES_URL` set but `asyncpg` missing, `connect()`
> raises, `verify_api_key` treats that like a database outage, and every keyed
> request gets **503** `Auth backend unavailable.` with
> `API-key verification failed: Postgres unavailable` in the API log. The image
> installs the `postgres` extra; a hand-rolled install needs
> `pip install '.[postgres]'`.

## 4. Create a key

```bash
docker compose -f docker/docker-compose.yml exec postgres psql -U admin -d ndif
```

```sql
INSERT INTO users (email)
VALUES ('researcher@example.edu') RETURNING user_id;

INSERT INTO keys (user_id)
VALUES ('<user_id from above>') RETURNING key_id;   -- this UUID is the API key

-- Optional: grant a tag.
INSERT INTO key_user_tag_assignments (key_id, user_tag_id)
SELECT '<key_id>', user_tag_id FROM user_tags WHERE name = 'trusted';
```

Grant `trusted` only to keys you would hand a shell on the model node.

### What an untagged key can do

An untagged key's request runs in a separate runner process that holds a
**weightless (meta) copy** of the model and drives the real one over a socket
(`src/ndif/services/ray/sandbox/model.py` `SandboxModelDeployment.execute`). Reading
and editing activations, `.save()`, `.source`, and generation all work. These do
not:

- reading or editing parameters directly (`module.weight`) — the runner's
  parameters are on the meta device, so reads fail and edits change nothing;
- gradients and `backward()`;
- training loops in a session — optimizer steps update nothing.

Users who need those need a `trusted` key. What the runner's process separation
covers, and what it does not, is in [Sandbox execution](../concepts/sandbox-execution.md).

## 5. Verify

Every failure is a distinct status (`src/ndif/services/api/auth.py` module
docstring):

| Condition | Status |
|---|---|
| no `ndif-api-key` header | 401 |
| header present but not a UUID | 400 |
| well-formed UUID, no row in `keys` | 403 |
| Postgres unreachable or erroring | 503 (fail closed) |

`/request` is multipart: a `data` form field holding the JSON envelope
(`model_key` is required — a malformed envelope 422s before the key is checked)
plus a `blob` file. An empty blob is fine:

```bash
probe() {  # $1 = key, or empty for none
  curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8001/request \
    ${1:+-H "ndif-api-key: $1"} \
    -F data='{"model_key":"probe"}' -F blob=@/dev/null
}
probe                                          # 401
probe not-a-uuid                               # 400
probe 00000000-0000-0000-0000-000000000000     # 403
```

`503` from all three means `require_ray_connection` rejected the request before
auth ran: Ray isn't connected (`src/ndif/services/api/app.py`
`require_ray_connection`). `200` from all three means auth is off — go back to
step 3.

`/whoami` resolves a key to its email and tags. A missing or unknown key returns
`{"email": null, "tags": []}`; a malformed one returns 400
(`src/ndif/services/api/app.py` `whoami`):

```bash
curl -s -H "ndif-api-key: <key_id>" localhost:8001/whoami
# {"email":"researcher@example.edu","tags":["trusted"]}
```

End to end from the client, which sends the key in the `ndif-api-key` header:

```python
import nnsight
nnsight.CONFIG.API.HOST = "http://localhost:8001"
nnsight.CONFIG.set_default_api_key("<key_id>")   # persists to the user config

from nnsight.modeling.transformers import TransformersModel
model = TransformersModel("openai-community/gpt2")
with model.trace("hello", remote=True):
    h = model.transformer.h[-1].output.save()
```

Without the key the same script fails on the HTTP 401 before any job exists.

## 6. Lock the dashboard

The dashboard has its own single-admin login and ignores `NDIF_POSTGRES_URL`.
The compose file ships it with `NDIF_DASHBOARD_DEV_MODE: "true"`, which makes
`require_auth` return the configured username unconditionally
(`src/ndif/services/dashboard/backend/auth.py` `require_auth`). The dashboard can
deploy, evict and restart models, and its deploy endpoint defaults to
`trusted: true` (`src/ndif/services/dashboard/backend/routers/deployments.py`
`DeployRequest`), so an open dashboard is remote code execution.

Generate a bcrypt hash:

```bash
docker compose -f docker/docker-compose.yml exec dashboard \
  python -m ndif.services.dashboard.backend.auth hash 'your-password'
```

In the `dashboard` service's `environment:`, **remove** `NDIF_DASHBOARD_DEV_MODE`
and set:

| Variable | Value |
|---|---|
| `NDIF_DASHBOARD_USERNAME` | the admin username (default `admin`) |
| `NDIF_DASHBOARD_PASSWORD_HASH` | the hash printed above. In the YAML, write each `$` as `$$` — compose interpolates `$` |
| `NDIF_DASHBOARD_SESSION_SECRET` | 32+ random bytes, e.g. `openssl rand -hex 32` |
| `NDIF_DASHBOARD_SESSION_TTL_DAYS` | cookie lifetime, default 7 |

Both defaults are unsafe in different directions
(`src/ndif/services/dashboard/backend/config.py` `Settings`): an empty
`password_hash` means nobody can log in, and the default `session_secret` is a
public string anyone can sign a cookie with.

Recreate and check, as in step 3:

```bash
just up dashboard
curl -s -o /dev/null -w '%{http_code}\n' localhost:8081/api/schedule   # 401
```

## Gotchas

- **Auth is checked at ingress only.** Anyone who can reach Redis, the Ray client
  port (10001) or the Ray dashboard (8265) bypasses it. See
  [Ports](../reference/ports.md).
- **A database outage rejects every keyed request with 503**, by design: a
  Postgres blip must never re-open the trusted path.
- **Models deployed while auth was off keep their trusted deployment.** The flag
  is fixed on the `Deployment` when it is created
  (`src/ndif/services/ray/deployments/controller/cluster/deployment.py`
  `Deployment`), and the queue reuses it for re-provisions
  (`src/ndif/services/api/queue/processor.py` `Processor.ensure_started`). Evict
  and redeploy after turning auth on if a model should stop loading with
  `trust_remote_code`.
- **Enabling auth is what makes per-user attribution work.** `email` is resolved
  at ingress and rides on every log record and metric.

## Related

- [Auth and limits](../concepts/auth-and-limits.md) — the trusted/untrusted fork
  and client version gating.
- [Sandbox execution](../concepts/sandbox-execution.md) — what the untrusted path's
  process separation covers.
- [Production](../operating/production.md) — the rest of hardening a deployment.
- [HTTP API](../reference/http-api.md) — every endpoint and its auth behavior.
