#!/bin/bash
# gpupool on a teammate's StratusGeo GPU server (uploaded to ~/gpupool by the Hermes app). Runs GitHub's own runner
# (~/actions-runner/run.sh), takes queued jobs until none are left, then stops this server. Everything lives here:
# the laptop can close as soon as Hermes says so. Never prints tokens.
#   bash ~/gpupool/boot.sh install    once, pasted into a JupyterLab terminal: adds a one-line hook to ~/.profile and
#                                     ~/.bashrc that runs boot.sh only while ~/gpupool/armed exists, then starts
#   bash ~/gpupool/boot.sh [start]    normal start (Hermes writes ~/gpupool/armed, then opens a terminal via the API)
#   bash ~/gpupool/boot.sh --check    PASS/FAIL checks (GPU, S3 read, runner registered), then stop this server
#   bash ~/gpupool/boot.sh --dry-run  the same checks, changes nothing, does not stop the server
# Stops the server (hub API, as ~/hubq/runner.sh does) after jobs_per_launch jobs, or when no job came for
# idle_wait_minutes (both from config.yml in the public repo, read at launch and between jobs; 0 = the old short wait,
# IDLE_S), whichever first. The wait never resets the job count. Before stopping after jobs it
# re-uploads anything the workflow parked in ~/gpupool/unsaved (results it could not confirm in S3) and says so if
# that still fails. Lifetime: no new job after MAX_S - DRAIN_S (a job arriving later fails fast and `pool recover`
# reruns it elsewhere; a running job's time is capped so its log + results are saved); stop at MAX_S.
# Status line for the Hermes app: ~/gpupool/status.txt.
set -euo pipefail
G=~/gpupool; R=~/actions-runner; REPO_URL=https://github.com/Luxaris/gpupool-jobs  # pool-only repo, never the hub backup repo
CONFIG_URL=${GPUPOOL_CONFIG_URL:-https://raw.githubusercontent.com/Luxaris/gpupool-launcher/main/config.yml}
IDLE_S=${GPUPOOL_IDLE_S:-180}; MAX_S=${GPUPOOL_MAX_S:-28800}; DRAIN_S=${GPUPOOL_DRAIN_S:-1800}
KNOWN=teams/team-sound/bench/barkley_truth_testday.json  # small object every experiment chain reads
HOOK='[ -f "$HOME/gpupool/armed" ] && bash "$HOME/gpupool/boot.sh" "$(cat "$HOME/gpupool/armed")" || true  # gpupool'
MODE=${1:-start}
mkdir -p $G

say() { echo "$*" > $G/status.tmp && mv -f $G/status.tmp $G/status.txt; echo "$(date +%F' '%T) $*"; }
cfg() {  # cfg <key> <default>: a number from ~/gpupool/config.yml (uploaded and kept fresh by Hermes: the launcher repo
  # may be private and servers never get a GitHub token), else the public config, else the last known value / default
  local v=""
  [ -f "$G/config.yml" ] && v=$(sed -n "0,/^$1: *\([0-9][0-9]*\).*/s//\1/p" "$G/config.yml")
  [ -n "$v" ] || v=$(curl -fsS -m 20 "$CONFIG_URL?t=$(date +%s)" | sed -n "s/^$1: *\([0-9][0-9]*\).*/\1/p") || v=""
  [ -n "$v" ] && echo "$v" > "$G/$1"; cat "$G/$1" 2> /dev/null || echo "$2"
}
read_config() {  # sets max_jobs, wait_s and stuck_s
  max_jobs=$(cfg jobs_per_launch 2)
  local m; m=$(cfg idle_wait_minutes 15)
  if [ "$m" -gt 0 ]; then wait_s=$((m * 60)); else wait_s=$IDLE_S; fi
  stuck_s=$(( $(cfg stuck_stop_minutes 30) * 60 ))
}
confirm_saved() {  # never stop silently with unconfirmed results: retry what the workflow parked in ~/gpupool/unsaved
  local d left=0 aws=/srv/conda/envs/notebook/bin/aws
  for d in $G/unsaved/*/; do
    [ -d "$d" ] || continue
    say "Saving results of job $(basename "$d") (retrying the upload)..." > /dev/null
    for _ in 1 2 3; do
      "$aws" s3 sync --profile hack-team --only-show-errors --exclude S3PREFIX "$d" "$(cat "$d/S3PREFIX")" > /dev/null 2>&1 && { rm -rf "$d"; break; }
      sleep 30
    done
    [ -d "$d" ] && left=$((left + 1))
  done
  [ $left -eq 0 ] || echo " WARNING: $left job result(s) could not be uploaded; they are kept safe on your server in ~/gpupool/unsaved and Pranav will collect them."
}
hub_stop() {
  say "$1"
  pkill -INT -f "$R/bin/Runner.Listener" || true; sleep 20  # give Hermes (if still open) time to show the line
  curl -s -o /dev/null -X DELETE -H "Authorization: token ${JUPYTERHUB_API_TOKEN:-}" "${JUPYTERHUB_API_URL:-}/users/${JUPYTERHUB_USER:-}/server" || true
  sleep 90; pkill -TERM -f jupyterhub-singleuser || true; exit 0  # the hub did not stop us: end the server ourselves
}
checks() {  # one PASS/FAIL line each, also appended to ~/gpupool/selftest.txt for the Hermes window
  local fails=0
  c() { local n=$1; shift; if "$@" > /dev/null 2>&1; then r="PASS $n"; else r="FAIL $n"; fails=$((fails + 1)); fi; echo "CHECK $r" | tee -a $G/selftest.txt; }
  c "GPU visible (nvidia-smi)" nvidia-smi -L
  c "S3 read with the team profile (s3://ocean-hackathon-2026/$KNOWN)" /srv/conda/envs/notebook/bin/python -c \
    "import boto3,json; json.loads(boto3.Session(profile_name='hack-team').client('s3').get_object(Bucket='ocean-hackathon-2026',Key='$KNOWN')['Body'].read())"
  c "job runner registered (~/actions-runner)" test -f $R/.runner
  c "start hook installed (~/.profile)" grep -qF '# gpupool' ~/.profile
  if [ $fails -eq 0 ]; then echo "CHECK DONE all passed" | tee -a $G/selftest.txt; else echo "CHECK DONE $fails FAILED: send Pranav a screenshot" | tee -a $G/selftest.txt; fi
}

case "$MODE" in
  --dry-run) checks; exit 0 ;;
  install)
    for f in ~/.profile ~/.bashrc; do grep -qF '# gpupool' "$f" 2> /dev/null || echo "$HOOK" >> "$f"; done
    touch $G/hooked; echo "gpupool hook installed. You can close this browser tab."
    MODE=$(cat $G/armed 2> /dev/null || echo start) ;;
esac
if [ "${2:-}" != --bg ] && [ -f $G/armed ] && [ $(( $(date +%s) - $(stat -c %Y $G/armed) )) -gt 1800 ]; then
  rm -f $G/armed  # left over from a launch whose laptop went away: never let it arm a server started later by hand
  echo "$(date +%F' '%T) stale start request (over 30 min old) ignored"; exit 0
fi
rm -f $G/armed  # one start per Start press; the teammate's own terminals never trigger anything
if [ "${2:-}" != --bg ]; then  # detach from the Jupyter terminal
  setsid nohup bash $G/boot.sh "$MODE" --bg >> $G/boot.log 2>&1 < /dev/null &
  exit 0
fi
exec 9> /tmp/gpupool.lock; flock -n 9 || { echo "$(date +%T) already running"; exit 0; }
: > $G/selftest.txt
if [ "$MODE" = --check ]; then checks; hub_stop "Check finished, stopping your GPU. Thanks!"; fi

export TZ=PCT7 DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1  # the runner then needs no libicu
START=$(date +%s); echo "$START $MAX_S $DRAIN_S" > $G/lifetime  # read by the workflow to cap a job's time
nvidia-smi -L > /dev/null 2>&1 || hub_stop "ERROR: this server has no GPU. Stopping it; press Start in Hermes again."
if [ -s $G/regtoken ] || [ ! -f $R/.runner ]; then  # GitHub's documented install: download, config.sh --unattended
  [ -s $G/regtoken ] || { rm -f $G/registered; hub_stop "ERROR: this GPU is not registered yet. Ask Pranav for a registration code and run again."; }
  say "Setting up your GPU for the job pool (first time only, about 2 min)..."
  if [ ! -x $R/config.sh ]; then
    v=$(curl -fsS https://api.github.com/repos/actions/runner/releases/latest | sed -n 's/.*"tag_name": *"v\([0-9.]*\)".*/\1/p') || v=""
    { [ -n "$v" ] && mkdir -p $R && curl -fsSL "https://github.com/actions/runner/releases/download/v$v/actions-runner-linux-x64-$v.tar.gz" | tar xz -C $R; } \
      || { rm -f $G/regtoken; hub_stop "ERROR: could not download the GitHub runner. Try again later."; }
  fi
  rm -f $R/.runner $R/.credentials $R/.credentials_rsaparams  # a new code = a fresh registration
  printf 'DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1\n' > $R/.env
  chmod 600 $G/regtoken
  # no `gpu` label yet: only this teammate's self-test reaches a new runner until Pranav runs `pool admit`
  (cd $R && ./config.sh --unattended --replace --url $REPO_URL --token "$(cat $G/regtoken)" \
     --name "gpu-$JUPYTERHUB_USER" --labels "stratusgeo,selftest-$JUPYTERHUB_USER" --work _work) > $G/config.log 2>&1 || ok=no
  rm -f $G/regtoken
  [ "${ok:-}" != no ] || hub_stop "ERROR: registration failed (codes expire after 1 hour). Ask Pranav for a new code and run again."
fi
touch $G/registered  # Hermes checks this (the Jupyter API cannot read hidden files like .runner)
ro=$(wc -c < $G/runner.out 2> /dev/null || echo 0)  # this boot's part of runner.out starts here
pgrep -f "$R/bin/Runner.Listener" > /dev/null || (cd $R && nohup ./run.sh >> $G/runner.out 2>&1 < /dev/null &)
read_config
bash $G/keepalive.sh $$ >> $G/keepalive.log 2>&1 &  # hub activity every 10 min for as long as this watchdog runs (jobs + waiting)
say "Ready: your GPU takes up to $max_jobs job(s) from the queue. You can close this window."
idle_since=$START; wpid=""; wname=""; n=0; restarts=0; online=""
while sleep 10; do
  now=$(date +%s); age=$((now - START))
  [ $age -ge "$MAX_S" ] && hub_stop "Reached the $((MAX_S / 3600)) h server limit, stopping your GPU. Thanks!"
  # the runner prints "Listening for Jobs" once GitHub has it online; never by stuck_stop_minutes = stop, don't burn the GPU
  # (grep -c reads to the end: with pipefail a grep -q would SIGPIPE tail and make the match look like a failure)
  [ -z "$online" ] && [ "$(tail -c +$((ro + 1)) $G/runner.out 2> /dev/null | grep -c 'Listening for Jobs' || true)" != 0 ] && online=yes
  [ -z "$online" ] && [ $age -ge "$stuck_s" ] && hub_stop "ERROR: the job runner did not come online within $((stuck_s / 60)) min (see ~/gpupool/runner.out). Stopping your GPU; Pranav has been told."
  w=$(pgrep -f "$R/bin/Runner.Worker" | head -1 || true)
  if [ -n "$w" ]; then  # a job is running (so the runner is online)
    online=yes
    [ "$w" != "$wpid" ] && { wpid=$w; rm -f $G/current; }
    wname=$(cat $G/current 2> /dev/null || true)  # written by the workflow's run step
    say "Running job $((n + 1)) of up to $max_jobs: ${wname:-starting...}$([ -e $G/stop ] && echo ' (stopping after this job)')"
    idle_since=$now; continue
  fi
  if [ -n "$wpid" ]; then  # a job just ended; self-tests do not count toward jobs_per_launch
    case "$wname" in selftest-*) ;; *) n=$((n + 1)) ;; esac
    wpid=""; wname=""
  fi
  [ -e $G/stop ] && hub_stop "Stopped after the current job, as you asked.$(confirm_saved) Thanks!"
  if [ $n -gt 0 ] && [ $((now - idle_since)) -lt 15 ]; then read_config; fi  # re-read the shared config between jobs
  [ $n -ge "$max_jobs" ] && hub_stop "Done $n job(s), this launch's share.$(confirm_saved) Stopping your GPU. Thanks!"
  [ $age -ge $((MAX_S - DRAIN_S)) ] && hub_stop "Close to the $((MAX_S / 3600)) h server limit.$(confirm_saved) Stopping your GPU. Thanks!"
  [ $((now - idle_since)) -ge "$wait_s" ] && hub_stop "Queue empty.$(confirm_saved) Stopping your GPU. Thanks!"
  if ! pgrep -f "$R/bin/Runner.Listener" > /dev/null; then
    if grep -qiE 'registration has been deleted|runner is not configured|registration.*not found' $G/runner.out 2> /dev/null; then
      : > $G/runner.out; rm -f $G/registered  # GitHub removes runners unused for 14 days; the binaries stay, only a new code is needed
      hub_stop "EXPIRED: your GPU slot expired after 14 days unused. Ask Pranav for a new code, then press Start."
    fi
    restarts=$((restarts + 1))
    [ $restarts -gt 3 ] && { rm -f $R/.runner $G/registered; hub_stop "ERROR: the job runner keeps stopping (~/gpupool/runner.out). Ask Pranav for a new registration code and run again."; }
    (cd $R && nohup ./run.sh >> $G/runner.out 2>&1 < /dev/null &)
  fi
  left=$(( (wait_s - (now - idle_since) + 59) / 60 ))
  say "Waiting for jobs (stops in $left min). $([ $n -eq 0 ] && echo "Takes up to $max_jobs job(s)." || echo "Done $n of up to $max_jobs.") You can close this window."
done
