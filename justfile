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
# For local client development, an installed nnsight is bind-mounted over the
# image's copy (docker-compose.nnsight.yml) so changes are picked up without a
# rebuild. Install it editable — `pip install -e /path/to/nnsight` — so this
# resolves to your source tree; if nnsight isn't importable the mount is skipped
# and the image's own nnsight (from requirements.txt) is used.

nnsight_path := `python -c "import nnsight, os; print(os.path.dirname(nnsight.__file__))" 2>/dev/null || true`
export NNSIGHT_PATH := nnsight_path

compose := "docker compose -f docker/docker-compose.yml" + if nnsight_path != "" { " -f docker/docker-compose.nnsight.yml" } else { "" }

# Show the available recipes.
default:
    @just --list

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
