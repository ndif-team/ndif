---
title: Enable API-Key Auth
one_liner: Turn on Postgres-backed API-key verification — and understand that until you do, every caller's Python runs in-process next to the weights.
tags: [runbook, operating, auth, api, sandbox, dashboard]
related: [docs/concepts/auth-and-limits.md, docs/concepts/sandbox-execution.md, docs/operating/production.md, docs/operating/dashboard.md, docs/developing/api-service.md, docs/developing/providers.md, docs/reference/env-vars.md, docs/reference/http-api.md, docs/runbooks/trace-a-users-failed-job.md]
sources: [src/ndif/services/api/auth.py, src/ndif/common/providers/postgres.py, src/ndif/common/schema/request.py, src/ndif/services/ray/sandbox/model.py, src/ndif/services/api/queue/processor.py, src/ndif/services/api/queue/replica.py, src/ndif/services/ray/deployments/controller/cluster/cluster.py, src/ndif/services/dashboard/backend/auth.py, src/ndif/services/dashboard/backend/config.py, docker/postgres/init.sql, docker/docker-compose.yml]
---

# Enable API-Key Auth

## What this covers

Switching an NDIF from "anyone who can reach port 8001" to "a key in a database",
plus the dashboard's entirely separate login. Read the first section before
deciding this is optional.

## Why this matters more than it looks

`NDIF_POSTGRES_URL` does not only gate *who* may submit. It decides *how their
code runs* and *how models are loaded*, through one boolean.

```python
    client_set_trusted = "trusted" in request.model_fields_set
    ...
    identity = await verify_api_key(request.api_key)
    if identity is not None:
        request.email = identity.email
        request.trusted = identity.trusted
        request.priority = identity.priority
    elif not client_set_trusted:
        # Auth is off (NDIF_POSTGRES_URL unset): a trusted-network / dev mode.
        # Default to trusted when the client didn't ask.
        request.trusted = True
```

— `src/ndif/services/api/auth.py:170-184`. With no Postgres configured,
`verify_api_key` returns `None` (`auth.py:93-95`), so **a request the client
didn't tag defaults to `trusted=True`** (an explicit `trusted` in the envelope,
True or False, is honored). A default request on an unauthenticated server is
therefore trusted. Follow that flag:

1. **User code runs in-process, in the model actor.** `SandboxModelDeployment.execute`
   short-circuits on `request.trusted` and calls the base implementation, which
   deserializes and runs the traced block on a worker thread inside the actor —
   the same process that holds the weights (`sandbox/model.py:242-243` →
   `modeling/base.py:379`). No runner subprocess, no socket. The isolation the
   sandbox path provides is skipped entirely.
2. **The model loads with `trust_remote_code=True`.** The flag rides from the
   request into the Processor (`queue/processor.py:114`, `:153-154`), into the
   `DeploymentConfig` the Processor's Replica sends the controller
   (`queue/replica.py:103`), into the evaluator and the actor's load
   (`cluster/cluster.py:169`, `controller.py:276-281`). A model auto-deployed by
   the first request on an unauthenticated server executes whatever Python its
   HuggingFace repo ships.

So: **no Postgres ⇒ no auth ⇒ a default request's Python runs next to the
weights, and models load their own repo code.** That is a reasonable default for a
laptop and an unreasonable one for anything reachable by another person.
Sandboxing is process-based and still in progress — see
[docs/concepts/sandbox-execution.md](../concepts/sandbox-execution.md) for what it
does and does not isolate — but a default request with auth off doesn't get even
that.

The escape hatch for dev: because an explicit `trusted` is honored with auth off,
a client can send `trusted: false` to force the sandbox path with **no Postgres at
all** — the only way to exercise untrusted execution on an auth-off stack. A plain
request without the flag still runs trusted.

## 1. Bring up Postgres

The compose stack already defines the service (`docker-compose.yml:98-116`):

```bash
just up postgres
docker compose -f docker/docker-compose.yml exec postgres pg_isready -U admin -d ndif
```

`docker/postgres/init.sql` is bind-mounted into `/docker-entrypoint-initdb.d/`,
so it runs **once, on an empty data directory**, as the superuser inside the
`ndif` database. Confirm the schema landed:

```bash
docker compose -f docker/docker-compose.yml exec postgres \
  psql -U admin -d ndif -c '\dt'
```

Nine tables: `users`, `profiles`, `user_tags`, `keys`,
`key_user_tag_assignments`, `models`, `model_tags`, `model_tag_assignments`,
`audit_logs`.

> **Gotcha:** the compose `postgres` service declares no named volume for
> `/var/lib/postgresql/data`, so its data lives in an anonymous Docker volume. A
> plain `just down` keeps it (and `init.sql` will **not** re-run on the next
> `up`); `just down -v` destroys it. If you edit `init.sql`, you must drop the
> volume for the change to take.

## 2. Understand the schema

`init.sql` seeds **no users and no keys** — that is deliberate. Four tables carry
everything the API reads:

| Table | Role in auth |
|---|---|
| `users` | `user_id` (UUID PK), `email` |
| `keys` | `key_id` (UUID PK — **this is the API key**), `user_id` |
| `user_tags` | `name` (unique), `description` |
| `key_user_tag_assignments` | many-to-many between `keys` and `user_tags` |

The API runs exactly one query (`auth.py:55-62`):

```sql
SELECT k.user_id, u.email, ut.name AS user_tag
FROM keys k
LEFT JOIN users u ON u.user_id = k.user_id
LEFT JOIN key_user_tag_assignments kuta ON kuta.key_id = k.key_id
LEFT JOIN user_tags ut ON ut.user_tag_id = kuta.user_tag_id
WHERE k.key_id = $1
```

**Validity is "a row exists in `keys`".** Key issuance is the account portal's
job — a separate repo — so the API only asks whether the key is known
(`auth.py` module docstring). The `LEFT JOIN`s mean a key with no tags still
returns one row (with `user_tag` NULL), which is how "known key, no tags" is
distinguished from "unknown key".

### The two user_tags the server acts on

| Tag | Constant | Effect |
|---|---|---|
| `trusted` | `TRUSTED_TAG` (`auth.py:44`) | the request runs in-process instead of in a runner, and any model it triggers a deploy for loads with `trust_remote_code` |
| `priority` | `PRIORITY_TAG` (`auth.py:48`) | the request is served ahead of all normal traffic for that model, FIFO against other priority requests (`queue/request_queue.py`) |

Every other tag name is inert — only `trusted` and `priority` change server
behavior. With auth off, `priority` is left at the request's value (`False` unless
the client sets it); with no keys there is nothing to jump ahead of. Create the
two tags:

```sql
INSERT INTO user_tags (name, description)
VALUES ('trusted',  'may run without sandbox isolation'),
       ('priority', 'jumps the model queue');
```

## 3. Point the API at it

Uncomment the line in `docker-compose.yml`'s `api` service
(`docker-compose.yml:156`):

```yaml
      NDIF_POSTGRES_URL: postgresql://ndifapi:admin@postgres:5432/ndif
```

`ndifapi` is a **read-only** role (`init.sql:161-166`), which is why the API can
only `SELECT`. `login_page` is the account-portal role: readonly plus
`INSERT/UPDATE/DELETE` on `keys` and `INSERT` on `users` (`init.sql:168-171`).
Both passwords in `init.sql` are `admin` — change them for anything real.

```bash
just up -d api        # or: just restart api
just logs api | grep -i postgres
```

The pool is created lazily on first use, so you'll see `Postgres connected` on
the first authenticated request, not at boot (`providers/postgres.py:82-111`).

> **Gotcha:** if `NDIF_POSTGRES_URL` is set but `asyncpg` isn't installed,
> `connect()` **raises** rather than silently disabling auth
> (`providers/postgres.py:91-95`) — the opposite of the fail-open telemetry
> providers. The compose image installs the `postgres` extra
> (`docker/Dockerfile:45`), so this only bites a hand-rolled install:
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

Grant `trusted` sparingly, and only to keys you would hand a shell on the model
node.

## 5. Verify

Every failure mode is a distinct status code (`auth.py:12-18`):

| Condition | Status |
|---|---|
| no `ndif-api-key` header | 401 |
| header present but not a UUID | 400 |
| well-formed UUID, no row in `keys` | 403 |
| Postgres unreachable or erroring | 503 (fail **closed**) |

Reproduce each against the running API. `/request` is multipart: a `data` form
field holding the JSON envelope (`model_key` is required — a malformed envelope
422s *before* the key is checked) plus a `blob` file. An empty blob is fine, since
auth short-circuits long before deserialization:

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

A `503` from all three means the route's `require_ray_connection` dependency
tripped first — Ray isn't connected, so auth was never reached
(`api/app.py:106-122`).

`/whoami` is the friendly check — it resolves a key to its email and tags, and
returns `{"email": null, "tags": []}` for a missing/unknown key rather than
erroring (`api/app.py:341-365`):

```bash
curl -s -H "ndif-api-key: $NDIF_API_KEY" localhost:8001/whoami
# {"email": "researcher@example.edu", "tags": ["priority"]}
```

An empty `tags` array with a real email means the key exists but carries no tags —
which is what you want for a normal user: not trusted, not priority.

End to end from the client, which sends the key in the `ndif-api-key` header
(nnsight's `RemoteBackend`; `NDIF_API_KEY` in the environment also works):

```python
import nnsight
nnsight.CONFIG.API.HOST = "http://localhost:8001"
nnsight.CONFIG.set_default_api_key("<key_id>")   # persists to the user config

from nnsight.modeling.transformers import TransformersModel
model = TransformersModel("openai-community/gpt2")
with model.trace("hello", remote=True):
    h = model.transformer.h[-1].output.save()
```

Without the key the same script raises on the HTTP 401 before any job is created.

Confirm the fork actually flipped: a request from an untagged key now runs in a
short-lived runner subprocess on the model actor's node, one per request, and the
actor stops capturing stdout through `LogStream` — the sandboxed path forwards
prints as `PRINT` events instead (`sandbox/model.py:180-186`).

## 6. The dashboard's auth is separate

The dashboard has its own single-admin login and knows nothing about
`NDIF_POSTGRES_URL` (`dashboard/backend/auth.py`). The compose file ships it with
auth **disabled** (`NDIF_DASHBOARD_DEV_MODE: "true"`, `docker-compose.yml:192`),
which makes `require_auth` return the configured username unconditionally
(`auth.py:70-75`). Since the dashboard can deploy, evict, and restart models —
and every dashboard deploy is `trusted: True` — leaving dev mode on is equivalent
to leaving remote code execution open.

Generate a hash — it prints a `$2b$12$…` string:

```bash
docker compose -f docker/docker-compose.yml exec dashboard \
  python -m ndif.services.dashboard.backend.auth hash 'your-password'
```

Then, in the `dashboard` service's `environment:` — **remove**
`NDIF_DASHBOARD_DEV_MODE` and set:

| Variable | Value |
|---|---|
| `NDIF_DASHBOARD_USERNAME` | the admin username (default `admin`) |
| `NDIF_DASHBOARD_PASSWORD_HASH` | the bcrypt hash printed above |
| `NDIF_DASHBOARD_SESSION_SECRET` | 32+ random bytes, e.g. `openssl rand -hex 32` |
| `NDIF_DASHBOARD_SESSION_TTL_DAYS` | cookie lifetime, default 7 |

Login issues an HttpOnly cookie signed with `itsdangerous`
(`dashboard/backend/auth.py:52-67`). Two defaults to notice: `password_hash`
defaults to the **empty string** and `verify_password` returns `False` for an
empty hash (`auth.py:39-45`), so with dev mode off and no hash set nobody can log
in at all; and `session_secret` defaults to the literal
`"change-me-please-this-is-not-secure"` (`backend/config.py:42`), which anyone
who knows it can use to forge a session cookie.

```bash
just restart dashboard
curl -s -o /dev/null -w '%{http_code}\n' localhost:8081/api/schedule   # expect 401
```

## Gotchas

- **Auth is checked at ingress only.** Nothing downstream re-verifies. Anyone who
  can reach Redis, the Ray client port (10001), or the Ray dashboard (8265)
  bypasses it completely. Turning on API keys does not make those ports safe to
  expose — see [docs/reference/ports.md](../reference/ports.md).
- **`api_key` and `email` travel with the request** all the way through the queue
  and across the Ray boundary, and are attached to logs and metrics
  (`common/schema/request.py:44-51`). Enabling auth is also what makes per-user
  attribution work in Grafana.
- **A DB outage rejects everything with 503.** That is the intended behavior
  (`auth.py:120-126`): a Postgres blip must never quietly re-enable the trusted
  path.
- **Models deployed while auth was off keep their `trusted` deployment.** The flag
  is fixed on the `Deployment` at creation (`cluster/deployment.py:78-79`). Evict
  and redeploy after turning auth on if you care that a model no longer loads with
  `trust_remote_code`.

## Related

- [docs/concepts/auth-and-limits.md](../concepts/auth-and-limits.md) — the
  trusted/untrusted fork and client version gating in full.
- [docs/concepts/sandbox-execution.md](../concepts/sandbox-execution.md) — what
  the untrusted path actually isolates.
- [docs/operating/production.md](../operating/production.md) — the rest of
  hardening a real deployment.
- [docs/reference/http-api.md](../reference/http-api.md) — every endpoint and its
  auth requirements.
