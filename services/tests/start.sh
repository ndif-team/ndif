#!/bin/bash
# Default environment variables with explanatory comments
# Override these by setting them before running the script

# Test scope/selection
: ${PYTEST_MARKERS:=""}  # e.g. "slow" or "integration" to run specific test categories
: ${PYTEST_PATH:="src"}  # Directory or specific test file to run
: ${PYTEST_PATTERN:=""}  # Pattern matching for test names e.g. "test_api*"

# Test execution configuration
: ${PYTEST_WORKERS:=4}   # Number of parallel workers (-n flag)
: ${PYTEST_TIMEOUT:=300} # Timeout in seconds for each test
: ${PYTEST_DEBUG:=0}     # Set to 1 for verbose debug output

# Coverage reporting
: ${PYTEST_COV:=1}      # Set to 0 to disable coverage reporting
: ${COV_REPORT:="term"} # Coverage report format: term, html, xml, etc.

# Build command based on configuration
CMD="pytest"

# Basic configuration
CMD="$CMD -v"  # Verbose output

# Parallel execution if workers > 1
if [ $PYTEST_WORKERS -gt 1 ]; then
    CMD="$CMD -n $PYTEST_WORKERS"
fi

# Add timeout
CMD="$CMD --timeout=$PYTEST_TIMEOUT"

# Coverage configuration
if [ $PYTEST_COV -eq 1 ]; then
    CMD="$CMD --cov=src --cov-report=$COV_REPORT"
fi

# Debug mode
if [ $PYTEST_DEBUG -eq 1 ]; then
    CMD="$CMD -vv --pdb"
fi

# Add markers if specified
if [ ! -z "$PYTEST_MARKERS" ]; then
    CMD="$CMD -m '$PYTEST_MARKERS'"
fi

# Add pattern if specified
if [ ! -z "$PYTEST_PATTERN" ]; then
    CMD="$CMD -k '$PYTEST_PATTERN'"
fi

# Add test path
CMD="$CMD $PYTEST_PATH"

# Print the final command
echo "Running: $CMD"

# Execute tests
eval $CMD

# Store the exit code
TEST_EXIT_CODE=$?

# Output summary
echo "Tests completed with exit code: $TEST_EXIT_CODE"
exit $TEST_EXIT_CODE
