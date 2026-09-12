#!/usr/bin/env bash
# Stop the sandbox gateway (sandbox home only). Never affects the live gateway.
set -u
REPO=/home/agent/.hermes/hermes-agent
SANDBOX=/home/agent/workspace/fluxer/sandbox
HOME_S="$SANDBOX/hermes-home"
HERMES_HOME="$HOME_S" "$REPO/venv/bin/python" "$REPO/hermes" gateway stop
