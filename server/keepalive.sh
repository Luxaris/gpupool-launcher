#!/bin/bash
# Keep this hub server from the 90-min idle stop while a job runs: report activity to the hub every 10 min,
# the same call the Jupyter server makes for kernels (a running process alone is not "activity").
# Token + URL come from the server's own environment and are never printed. Stops when PID exits.
#   nohup experiments/keepalive.sh <pid> > logs/keepalive.log 2>&1 &
pid=$1
env_of() { tr '\0' '\n' < /proc/$(pgrep -f jupyterhub-singleuser | head -1)/environ | sed -n "s/^$1=//p"; }
url=$(env_of JUPYTERHUB_ACTIVITY_URL); tok=$(env_of JUPYTERHUB_API_TOKEN)
alive() { [ -e /proc/$pid ] && ! grep -q "^State:.*Z" /proc/$pid/status 2>/dev/null; }  # a zombie counts as gone
while alive; do
  now=$(date -u +%Y-%m-%dT%H:%M:%S.000Z)
  curl -s -o /dev/null -w "$(date +%T) %{http_code}\n" -X POST -H "Authorization: token $tok" -H "Content-Type: application/json" \
    -d "{\"last_activity\":\"$now\",\"servers\":{\"\":{\"last_activity\":\"$now\"}}}" "$url"
  sleep 600
done
echo "$(date +%T) pid $pid gone, stopping"
