# NDIF task runner. Thin wrapper over the docker compose stack.
#
#   just build            # build every service image
#   just up               # start the whole stack (detached)
#   just up api           # start just the api service
#   just down             # tear the stack down
#   just ta               # down -> build -> up (full refresh)
#   just ta api           # ...targeting one service
#   just logs api         # follow a service's logs
#
# Recipes taking *services accept zero or more compose service names; with none
# they apply to the whole stack.
#
# For local client development, an *editable* nnsight checkout is bind-mounted
# over the image's copy (docker-compose.nnsight.yml) so changes are picked up
# without a rebuild. Install it editable — `pip install -e /path/to/nnsight` —
# in the shell you run `just` from, or set NNSIGHT_PATH to the package
# directory explicitly. A non-editable nnsight (one living under site-packages,
# e.g. the wrong conda env's copy of an older release) is deliberately NOT
# mounted: it would silently replace the image's pinned nnsight with whatever
# your shell happens to have, and the api/ray containers die on import with
# errors that don't mention nnsight at all. `just nnsight` shows what would be
# mounted.

nnsight_path := env("NNSIGHT_PATH", `python -c "import nnsight, os; p = os.path.dirname(nnsight.__file__); print('' if 'site-packages' in p or 'dist-packages' in p else p)" 2>/dev/null || true`)
export NNSIGHT_PATH := nnsight_path

# The package version is the git tag (setuptools-scm); the image build has no
# .git, so hand it the same string setuptools-scm would derive.
# setuptools-scm if the shell has it, else a PEP 440 rendering of `git describe`
# (v0.0.1-441-g0ac4463 -> 0.0.1.post441+g0ac4463), else empty.
export NDIF_VERSION := env("NDIF_VERSION", `python -m setuptools_scm 2>/dev/null || git describe --tags 2>/dev/null | sed -E 's/^v//; s/-([0-9]+)-g/.post\1+g/' || true`)

compose := "docker compose -f docker/docker-compose.yml" + if nnsight_path != "" { " -f docker/docker-compose.nnsight.yml" } else { "" }

# Show the available recipes.
default:
    @just --list

# Show which nnsight (if any) would be bind-mounted over the image's copy.
nnsight:
    @if [ -n "{{nnsight_path}}" ]; then echo "mounting editable nnsight from {{nnsight_path}}"; else echo "no editable nnsight found; the image's own nnsight will be used"; fi

# Build service image(s).
build *services:
    {{compose}} build {{services}}

# Start service(s) in the background.
up *services:
    {{compose}} up -d {{services}}

# Stop and remove the stack (pass -v to also drop volumes).
down *args:
    {{compose}} down {{args}}

# Full refresh: down, rebuild, then bring back up.
ta *services:
    just down
    just build {{services}}
    just up {{services}}

# Restart service(s).
restart *services:
    {{compose}} restart {{services}}

# Follow logs (Ctrl-C to detach).
logs *services:
    {{compose}} logs -f {{services}}

# Show container status.
ps:
    {{compose}} ps
