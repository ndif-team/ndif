# Contributing to NDIF

## Branches and PRs

Branch off `dev` and open your pull request into `dev`. A push to `main` builds
the hosted image, and a `v*` tag publishes the Docker Hub images (the PyPI package
publishes on a GitHub release), so a tag is a release, not a checkpoint.

## Setup

[docs/operating/quickstart.md](docs/operating/quickstart.md) brings up a stack
from a checkout. For tests, install the `dev` extra plus a client nnsight:

```bash
pip install -e ".[dev]"
```

[docs/developing/nnsight-integration.md](docs/developing/nnsight-integration.md#developing-against-a-local-nnsight)
covers developing against a local nnsight checkout.

## Tests

No workflow runs the tests, so run them before you open a PR:

```bash
just up                    # bring the compose stack up (needs a GPU)
just ta                    # after a code change: the source is baked into the image
pytest tests/              # everything; live-server tests skip unless localhost:8001 is up
pytest tests/test_docs.py  # the docs gate; no server needed
```

An all-skipped live run means the server wasn't up. If you touched execution, also
run the suite with `trusted=False` forced so the sandbox path is covered — a
local stack is trusted by default.
[docs/developing/testing.md](docs/developing/testing.md) has the recipe and the
file-by-file inventory.

## Docs

The docs are for a reader — usually a coding agent — who needs to find the one page
for their task and apply it correctly. Keep them that way:

- **Docs change in the same PR as the code that invalidates them.** If your change
  makes a sentence false, fix the sentence in the same PR.
- **One topic, one home.** Extend the page that owns a topic; link to it from
  elsewhere rather than restating it. [docs/index.md](docs/index.md) is the map.
- **Nothing environment-specific.** No machine or host names, personal paths,
  tunnels, or one operator's deployment details. State the requirement, not the
  machine.
- **Present tense, current truth.** No history narration, no issue numbers.
- **Cite code by path + symbol**, never by line number:
  `src/ndif/services/api/auth.py` `verify_api_key`. Line numbers drift with every
  edit; symbols don't. `tests/test_docs.py` checks that cited paths exist, that
  cited symbols are defined in the cited file, and that every relative link and
  anchor resolves.

House style for the code itself — comments, env vars, logging, where code
belongs — is in [docs/developing/contributing.md](docs/developing/contributing.md).

## Bug reports

Open an issue at [github.com/ndif-team/ndif](https://github.com/ndif-team/ndif/issues)
with:

- what you ran and what you expected;
- the exact error text, and the request id if there is one;
- `ndif version` from the server and your client's nnsight, Python, torch and
  transformers versions (`ndif env --local`, or `ndif.compare()` from nnsight);
- how you run the server: the published image, the compose stack, or from source.

For a security problem, don't open a public issue; reach the maintainers
privately on the [NDIF Discord](https://discord.gg/6uFJmCSwW7).

Please follow the [Code of Conduct](CODE_OF_CONDUCT.md).
