#!/bin/bash
# Monitor API container logs for errors and reconnection attempts

CONTAINER_NAME="dev-api-1"
FOLLOW=true

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --no-follow)
            FOLLOW=false
            shift
            ;;
        --tail)
            TAIL="$2"
            shift 2
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: $0 [--no-follow] [--tail N]"
            exit 1
            ;;
    esac
done

echo "============================================================"
echo "Monitoring API logs for errors and reconnection events"
echo "Container: $CONTAINER_NAME"
echo "============================================================"
echo ""
echo "Key patterns to watch for:"
echo "  - 'Error connecting to Ray'"
echo "  - 'ActorUnavailableError'"
echo "  - 'Unrecoverable error in data channel'"
echo "  - 'Ray connection error'"
echo "  - 'Connected to Ray'"
echo ""
echo "============================================================"

TAIL_ARG=""
if [ -n "$TAIL" ]; then
    TAIL_ARG="--tail $TAIL"
fi

if [ "$FOLLOW" = true ]; then
    docker logs -f $TAIL_ARG "$CONTAINER_NAME" 2>&1 | grep --line-buffered -E \
        "(ERROR|WARN|Error connecting|ActorUnavailable|Unrecoverable|Ray connection|Connected to Ray|Reconnecting|reset|CANCELLED)"
else
    docker logs $TAIL_ARG "$CONTAINER_NAME" 2>&1 | grep -E \
        "(ERROR|WARN|Error connecting|ActorUnavailable|Unrecoverable|Ray connection|Connected to Ray|Reconnecting|reset|CANCELLED)"
fi
