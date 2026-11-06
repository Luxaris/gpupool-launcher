"""gpupool autostart, run by Jupyter itself when a teammate's hub server boots (a few lines Hermes adds to
~/.jupyter/jupyter_server_config.py). It starts the pool watchdog (~/gpupool/boot.sh) WITHOUT any laptop or terminal,
but only for a server Hermes asked the hub for: Hermes posts a signed spawn request ("gpupool-spawn <user> <time>
<hmac>") to the pool's ntfy topic right before it starts the server, keyed with ~/gpupool/spawn_key (random, only in
the teammate's home and on their laptop). A server the teammate started themselves finds no fresh, unused, valid
request and is never touched. Once boot.sh runs, its own limits apply (runner not online within stuck_stop_minutes ->
the server stops itself), so a laptop that dies mid-start can no longer leave a GPU idling until the hub's 8 h cap.
Stdlib only; never raises into Jupyter; logs to ~/gpupool/autostart.log.
"""
import hashlib, hmac, json, os, re, subprocess, sys, time, urllib.request

G = os.path.expanduser('~/gpupool')
WINDOW_S = 1800  # a spawn request older than this does not count (Hermes waits at most ~20 min for the hub)


def log(msg):
    print(time.strftime('%F %T'), msg, flush=True)


def valid_request(lines, user, key, now, used):
    """The newest signed spawn request for `user`, not older than WINDOW_S and not used before; None otherwise."""
    best = None
    for line in lines:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get('event') != 'message':
            continue
        p = str(m.get('message', '')).split()
        if len(p) != 4 or p[0] != 'gpupool-spawn' or p[1] != user or not p[2].isdigit():
            continue
        t = int(p[2])
        want = hmac.new(key.encode(), f'{user}|{t}'.encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(want, p[3]) and -300 <= now - t <= WINDOW_S and t not in used and (best is None or t > best):
            best = t
    return best


def main():
    need = [os.path.join(G, f) for f in ('registered', 'spawn_key', 'boot.sh', 'config.yml')]
    if not all(os.path.exists(f) for f in need):
        return log('not a pool member here (or set up before v14): nothing to do')
    user = os.environ.get('JUPYTERHUB_USER', '')
    key = open(os.path.join(G, 'spawn_key')).read().strip()
    topic = re.search(r'(?m)^heartbeat_topic:\s*(\S+)', open(os.path.join(G, 'config.yml')).read())
    if not user or not key or not topic:
        return log('missing user / key / topic')
    used_f = os.path.join(G, 'spawn_used')
    used = {int(x) for x in open(used_f).read().split() if x.isdigit()} if os.path.exists(used_f) else set()
    t = None
    for wait in (0, 10, 20, 30, 60):  # the network can come up a little after Jupyter
        time.sleep(wait)
        try:
            body = urllib.request.urlopen(f'https://ntfy.sh/{topic.group(1)}/json?poll=1&since=40m', timeout=20).read()
        except Exception as e:
            log(f'ntfy not reachable yet ({type(e).__name__})')
            continue
        t = valid_request(body.decode('utf-8', 'replace').splitlines(), user, key, time.time(), used)
        break
    if t is None:
        return log('no fresh signed spawn request from Hermes: this server is left alone')
    with open(used_f, 'a') as f:  # one request arms one server
        f.write(f'{t}\n')
    if subprocess.run(['pgrep', '-f', 'gpupool/boot.sh'], capture_output=True).returncode == 0:
        return log('boot.sh already running')
    with open(os.path.join(G, 'armed'), 'w') as f:
        f.write('start')
    log(f'Hermes asked for this server at {t}: starting the pool watchdog (no laptop needed from here)')
    subprocess.Popen(['bash', os.path.join(G, 'boot.sh'), 'start'], start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=open(os.path.join(G, 'boot.log'), 'a'), stderr=subprocess.STDOUT)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:  # never bother Jupyter
        log(f'error: {type(e).__name__}: {e}')
