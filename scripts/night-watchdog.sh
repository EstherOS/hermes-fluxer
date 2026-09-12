#!/usr/bin/env bash
# Night-shift load watchdog. Prints HIGHLOAD lines when the box is stressed;
# Hermes gets notified and can kill/throttle whatever is misbehaving.
LOG=/home/agent/workspace/fluxer/status/night-watchdog.log
mkdir -p "$(dirname "$LOG")"
COOLDOWN=900
last=0
streak=0
while true; do
  load=$(cut -d' ' -f1 /proc/loadavg)
  over=$(awk -v l="$load" 'BEGIN{print (l>5.0)?1:0}')
  if [ "$over" = "1" ]; then streak=$((streak+1)); else streak=0; fi
  now=$(date +%s)
  if [ "$streak" -ge 2 ] && [ $((now-last)) -ge $COOLDOWN ]; then
    echo "HIGHLOAD load=$load streak=$streak $(date -u +%H:%M:%SZ)" | tee -a "$LOG"
    ps -eo pid,ppid,pcpu,pmem,args --sort=-pcpu | head -7 | tee -a "$LOG"
    last=$now
  fi
  sleep 60
done
