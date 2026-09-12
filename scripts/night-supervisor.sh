#!/usr/bin/env bash
# Night supervisor (fluxer): load watchdog + delegation diagnostics + heartbeat.
# Prints HIGHLOAD / STALL-ALERT / NIGHT-HEARTBEAT lines; Hermes gets notified.
set -u
LIVE=/home/agent/.hermes/cache/delegation/live/deleg_79096f38
LOG=/home/agent/workspace/fluxer/status/night-supervisor.log
DONE_MARKER=/home/agent/workspace/fluxer/status/delegation-done
mkdir -p "$(dirname "$LOG")"
started=$(date +%s)
last_load=0; streak=0
last_stall=0
last_hb=$started
while true; do
  now=$(date +%s)
  if [ $((now - started)) -ge 32400 ]; then
    echo "NIGHT-SUPERVISOR-EXIT $(date -u +%H:%M:%SZ)" >>"$LOG"; exit 0
  fi
  load=$(cut -d' ' -f1 /proc/loadavg)
  if awk -v l="$load" 'BEGIN{exit !(l>5.0)}'; then streak=$((streak+1)); else streak=0; fi
  if [ "$streak" -ge 2 ] && [ $((now-last_load)) -ge 900 ]; then
    { echo "HIGHLOAD load=$load streak=$streak at $(date -u +%H:%M:%SZ)"; ps -eo pid,pcpu,pmem,args --sort=-pcpu | head -7; } | tee -a "$LOG"
    last_load=$now
  fi
  if [ ! -e "$DONE_MARKER" ]; then
    newest=0; any=0
    for f in "$LIVE"/task-*.log; do
      [ -e "$f" ] || continue
      any=1
      m=$(stat -c %Y "$f")
      [ "$m" -gt "$newest" ] && newest=$m
    done
    if [ "$any" = "1" ] && [ $((now-newest)) -ge 2700 ] && [ $((now-last_stall)) -ge 1800 ]; then
      echo "STALL-ALERT delegation transcripts idle $(( (now-newest)/60 ))m at $(date -u +%H:%M:%SZ)" | tee -a "$LOG"
      last_stall=$now
    fi
  fi
  if [ $((now-last_hb)) -ge 5400 ]; then
    echo "NIGHT-HEARTBEAT $(date -u +%H:%M:%SZ) load=$load" | tee -a "$LOG"
    last_hb=$now
  fi
  sleep 60
done
