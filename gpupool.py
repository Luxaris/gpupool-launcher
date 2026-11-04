#!/usr/bin/env python3
"""Hermes engine (gpupool): lend your StratusGeo GPU to the team job pool. Stdlib only (Python 3.8+).

Used by the Hermes app (gpupool_gui.py, built into Hermes-Setup.exe / Hermes-mac-*.dmg) and by a plain CLI:
    python gpupool.py [--check | --update | --dry-run | --reset]
Everything after a launch runs on the hub server (boot.sh): the laptop may close. Talks only to
  - the JupyterHub REST API (start your default server) and the Jupyter REST API on it (files, one terminal),
  - the public repo PUBLIC (config.yml + release assets, pinned by tag, sha256-checked) for updates/server scripts.
Your hub token stays in a user-only file in your config dir; nothing of Pranav's is used or shipped.
"""
import base64, hashlib, json, os, platform, stat, subprocess, sys, tempfile, time, urllib.error, urllib.request, webbrowser

VERSION = 5
APP = 'Hermes'
HUB = 'https://hub.stratusgeo.org'
TOKEN_PAGE = f'{HUB}/hub/token'
PUBLIC = 'Luxaris/gpupool-launcher'
CONFIG_URL = f'https://raw.githubusercontent.com/{PUBLIC}/main/config.yml'
ASSET_URL = f'https://github.com/{PUBLIC}/releases/download/{{tag}}/{{name}}'
GPU_OPTS = {'profile': 'gpu', 'image--unlisted-choice': 'quay.io/cryointhecloud/cryo-python-ai:latest'}  # = stratusgeo preset 5
SERVER_FILES = ('boot.sh', 'keepalive.sh')
INSTALL_LINE = 'bash ~/gpupool/boot.sh install'
CFG_DIR = os.path.join(os.environ.get('APPDATA') or os.path.expanduser('~/.config'), 'gpupool')
LOGIN = os.path.join(CFG_DIR, 'login.json')
HISTORY = os.path.join(CFG_DIR, 'history.txt')  # last jobs seen on the server, kept for when it is off
BUSY = ('Ready', 'Running', 'Done', 'Setting', 'Saving', 'Stopping after', 'Waiting for jobs')
QUEUE_URL = f'https://raw.githubusercontent.com/{PUBLIC}/main/queue.json'  # count only, kept current by `pool`


class Fail(Exception):
    """A step failed; the message is meant for the teammate."""


def fetch(url, headers=None, data=None, method=None, timeout=60):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def parse_config(text):
    """config.yml is flat `key: value` lines (no YAML library needed)."""
    out = {}
    for line in text.splitlines():
        line = line.split('#', 1)[0].strip()
        if ':' in line:
            k, v = line.split(':', 1)
            out[k.strip()] = v.strip()
    return out


def app_asset():
    """The release asset this copy updates from: the installer when frozen, else the two script files."""
    if not getattr(sys, 'frozen', False):
        return None
    if os.name == 'nt':
        return f'{APP}-Setup.exe'
    return f'{APP}-mac-{"arm64" if platform.machine() == "arm64" else "x64"}.dmg'


class Engine:
    def __init__(self, dry_run=False):
        self.dry, self.user, self.token, self.config = dry_run, None, None, None

    # ---- login (asked once) ----
    def saved_login(self):
        try:
            d = json.load(open(LOGIN))
            self.user, self.token = d['user'], d['token']
            return True
        except (OSError, ValueError, KeyError):
            return False

    def login(self, user, token):
        self.user, self.token = user.strip().lower(), token.strip()
        self.state()  # raises Fail when the hub refuses
        os.makedirs(CFG_DIR, exist_ok=True)
        fd = os.open(LOGIN, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)  # user-only file
        with os.fdopen(fd, 'w') as f:
            json.dump({'user': self.user, 'token': self.token}, f)

    @staticmethod
    def forget():
        if os.path.exists(LOGIN):
            os.remove(LOGIN)

    # ---- remote config + updates ----
    def load_config(self):
        self.config = parse_config(fetch(CONFIG_URL + f'?t={int(time.time())}').decode())
        return self.config

    def asset(self, name):
        """A release asset of the pinned tag, refused unless its sha256 matches config.yml."""
        c = self.config or self.load_config()  # the setup wizard runs before the main screen has loaded it
        b = fetch(ASSET_URL.format(tag=c['launcher_tag'], name=name), timeout=600)
        if hashlib.sha256(b).hexdigest() != c.get(f'sha256 {name}', ''):
            raise Fail(f'{name} from {c["launcher_tag"]} failed its checksum; not using it. Tell Pranav.')
        return b

    @staticmethod
    def queue():
        """The public pool indicator: {'waiting': n, 'set': opaque id of those jobs, 'updated': iso}. No login needed."""
        try:
            return json.loads(fetch(QUEUE_URL + f'?t={int(time.time())}', timeout=30))
        except urllib.error.HTTPError as e:
            if e.code == 404:  # not published yet
                return {'waiting': 0, 'set': ''}
            raise

    def update_needed(self):
        return int(self.config.get('launcher_version', VERSION)) > VERSION

    def too_old(self):
        return VERSION < int(self.config.get('min_launcher_version', 0))

    def update(self):
        """Install the pinned, checksum-verified version. Frozen app: run the standard installer (Windows: Inno Setup
        /SILENT, per user; Mac: copy the .app out of the .dmg with hdiutil + ditto). Script copy: replace the files."""
        name = app_asset()
        if name is None:
            files = {n: self.asset(n) for n in ('gpupool.py', 'gpupool_gui.py')}
            if not self.dry:
                here = os.path.dirname(os.path.abspath(__file__))
                for n, b in files.items():
                    with open(os.path.join(here, n + '.new'), 'wb') as f:
                        f.write(b)
                    os.replace(os.path.join(here, n + '.new'), os.path.join(here, n))
            return 'restart'
        b = self.asset(name)
        if self.dry:
            return 'restart'
        path = os.path.join(tempfile.mkdtemp(), name)
        with open(path, 'wb') as f:
            f.write(b)
        if os.name == 'nt':  # the per-user installer closes this app, updates it and reopens it
            subprocess.Popen([path, '/SILENT', '/SUPPRESSMSGBOXES', '/relaunch=1'])
            return 'installer'
        bundle = os.path.abspath(os.path.join(sys.executable, '..', '..', '..'))  # the installed .app (any folder name)
        mnt = tempfile.mkdtemp()
        subprocess.run(['hdiutil', 'attach', '-nobrowse', '-quiet', '-mountpoint', mnt, path], check=True)
        try:
            subprocess.run(['ditto', os.path.join(mnt, f'{APP}.app'), bundle], check=True)
        finally:
            subprocess.run(['hdiutil', 'detach', '-quiet', mnt])
        return 'restart'

    # ---- JupyterHub + Jupyter REST ----
    def api(self, method, url, body=None, raw=False):
        h = {'Authorization': f'token {self.token}'}
        data = None
        if body is not None:
            h['Content-Type'] = 'application/json'
            data = json.dumps(body).encode()
        b = fetch(url, h, data, method)
        return b if raw else (json.loads(b) if b else None)

    @property
    def base(self):
        return f'{HUB}/user/{self.user}'

    def state(self):
        """off | pending | gpu | cpu  (your default server)"""
        try:
            u = self.api('GET', f'{HUB}/hub/api/users/{self.user}')
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404):
                raise Fail('The hub did not accept your username/token.')
            raise Fail(f'Cannot reach the hub (HTTP {e.code}).')
        except urllib.error.URLError as e:
            raise Fail(f'Cannot reach the hub ({e.reason}).')
        s = (u.get('servers') or {}).get('')
        if not s:
            return 'off'
        if not s.get('ready'):
            return 'pending'
        return 'gpu' if (s.get('user_options') or {}).get('profile') == 'gpu' else 'cpu'

    def text(self, path):
        try:
            return self.api('GET', f'{self.base}/files/{path}?t={int(time.time())}', raw=True).decode('utf-8', 'replace')
        except urllib.error.HTTPError:
            return ''

    def exists(self, path):
        try:
            self.api('GET', f'{self.base}/api/contents/{path}?content=0')
            return True
        except urllib.error.HTTPError:
            return False

    def put(self, path, data):
        self.api('PUT', f'{self.base}/api/contents/{path}',
                 {'type': 'file', 'format': 'base64', 'content': base64.b64encode(data).decode()})

    def rm(self, path):
        try:
            self.api('DELETE', f'{self.base}/api/contents/{path}')
        except urllib.error.HTTPError:
            pass

    # ---- the launch, in steps (the app shows each one) ----
    def pooled(self, st):
        return st == 'gpu' and self.text('gpupool/status.txt').startswith(BUSY)

    def server_up(self, say=print):
        """Start your default server with a GPU and wait until it is ready. Leaves a server you started yourself alone."""
        st = self.state()
        if self.pooled(st):
            return 'pooled'
        if st in ('gpu', 'cpu'):
            raise Fail(f'Your hub server is already running (your own work?), so {APP} leaves it alone. To lend the GPU, '
                       'stop it on the hub (File > Hub Control Panel > Stop My Server) and press Start again.')
        if self.dry:
            say('[dry-run] would start a GPU server')
            return 'dry'
        for attempt in (1, 2):
            if st == 'off':
                say('Starting your GPU server (about 5-15 min)...' if attempt == 1 else
                    'First start can take ~10-15 min while the hub downloads the software; trying again...')
                try:
                    self.api('POST', f'{HUB}/hub/api/users/{self.user}/server', GPU_OPTS)
                except urllib.error.HTTPError as e:
                    raise Fail(f'The hub would not start a GPU server: {e.read().decode("utf-8", "replace")[:300]}')
            t0, note, seen = time.time(), '', 0
            while (st := self.state()) == 'pending':
                if time.time() - t0 > 1200:
                    raise Fail(f'Your GPU server was not ready after 20 min. Check the hub in a browser.{note}')
                if time.time() - seen > 60:  # remember the hub's latest progress line, for the error text
                    note, seen = self.spawn_note() or note, time.time()
                time.sleep(10)
            if st == 'gpu':
                time.sleep(5)  # the Jupyter API answers a few seconds after "ready"
                return 'up'
            if st != 'off':
                break
            note = self.spawn_note() or note  # the server vanished: the hub dropped the start
        raise Fail(f'No GPU server came up. Check the hub in a browser.{note}')

    def spawn_note(self, wait=8):
        """The hub's last spawn progress message (JupyterHub /server/progress event stream), as ' Hub: ...'; '' if none."""
        msg = ''
        req = urllib.request.Request(f'{HUB}/hub/api/users/{self.user}/server/progress',
                                     headers={'Authorization': f'token {self.token}', 'Accept': 'text/event-stream'})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=wait) as r:
                for line in r:
                    if line.startswith(b'data:'):
                        try:
                            msg = json.loads(line[5:]).get('message') or msg
                        except ValueError:
                            pass
                    if time.time() - t0 > wait:
                        break
        except Exception:  # timeout or no spawn in progress: keep whatever was read
            pass
        return f' Hub: {msg.strip()[:300]}' if msg.strip() else ''

    def prepare(self, mode='start', code=None):
        """Upload fresh, checksum-verified server scripts (+ the registration code) and arm boot.sh."""
        scripts = {n: self.asset(n) for n in SERVER_FILES}
        if self.dry:
            return
        self.api('PUT', f'{self.base}/api/contents/gpupool', {'type': 'directory'})
        for n, b in scripts.items():
            self.put(f'gpupool/{n}', b)
        for n in ('status.txt', 'selftest.txt', 'stop'):
            self.rm(f'gpupool/{n}')
        if code:
            self.put('gpupool/regtoken', code.strip().encode())
        self.put('gpupool/armed', mode.encode())

    def trigger(self):
        """Start boot.sh through a Jupyter terminal (its login shell runs the hook). 'paste' = one-time step needed."""
        if self.dry:
            return 'dry'
        if not self.exists('gpupool/hooked'):
            return 'paste'
        t = self.api('POST', f'{self.base}/api/terminals')
        time.sleep(5)
        self.api('DELETE', f'{self.base}/api/terminals/{t["name"]}')
        return 'started'

    def registered(self):
        return self.exists('gpupool/registered')

    def request_stop(self):
        """Stop after the current job (boot.sh still confirms the backups before it stops the server)."""
        self.put('gpupool/stop', b'stop')

    def poll(self):
        """Everything the app shows, in one call: server state, status line, self-test lines, last jobs."""
        st = self.state()
        out = {'server': st, 'status': '', 'selftest': [], 'hooked': False}
        if st == 'gpu':
            out['status'] = self.text('gpupool/status.txt').strip()
            out['selftest'] = [l for l in self.text('gpupool/selftest.txt').splitlines() if l.strip()]
            out['hooked'] = self.exists('gpupool/hooked')
            hist = self.text('gpupool/history.txt')
            if hist.strip():
                os.makedirs(CFG_DIR, exist_ok=True)
                with open(HISTORY, 'w') as f:
                    f.write(hist)
        out['history'] = [l.split('\t') for l in open(HISTORY).read().splitlines()][-5:] if os.path.exists(HISTORY) else []
        return out

    def launch(self, mode='start', ask_code=None, say=print):
        """The whole launch for the CLI. Returns True when boot.sh was started (or is already running)."""
        say(f'PASS hub login ({self.user})')
        up = self.server_up(say)
        if up == 'pooled':
            say('Your GPU is already in the pool.')
            return True
        say('PASS GPU server is up' if up == 'up' else '')
        code = None
        if mode == 'start' and not self.dry and not self.registered():
            code = ask_code() if ask_code else None
            if not code:
                raise Fail('This GPU is not registered yet: ask Pranav for a registration code.')
        self.prepare(mode, code)
        say(f'PASS server scripts {self.config["launcher_tag"]} downloaded and verified')
        if self.trigger() == 'paste':
            say(f'One-time step: in the browser tab that opens, click File > New > Terminal, paste this line and press Enter:\n    {INSTALL_LINE}')
            copy_to_clipboard(INSTALL_LINE)
            webbrowser.open(f'{self.base}/lab')
        return not self.dry


def copy_to_clipboard(text):
    cmd = ['clip'] if os.name == 'nt' else ['pbcopy'] if sys.platform == 'darwin' else ['xclip', '-selection', 'clipboard']
    try:
        subprocess.run(cmd, input=text.encode(), check=True)
    except (OSError, subprocess.CalledProcessError):
        pass


def main(argv):
    import getpass
    dry = '--dry-run' in argv
    e = Engine(dry_run=dry)
    say = lambda m: m and print(time.strftime('%H:%M ') + m, flush=True)
    if '--reset' in argv:
        Engine.forget()
    try:
        e.load_config()
    except Exception as x:
        raise Fail(f'Cannot read the pool settings ({CONFIG_URL}): {x}')
    if '--update' in argv or e.update_needed():
        say(f'Updating {APP} {VERSION} -> {e.config["launcher_version"]}...')
        e.update()
        say(f'PASS updated; please start {APP} again.' if not dry else '[dry-run] update verified, not installed')
        return 0
    if e.too_old():
        raise Fail(f'This {APP} ({VERSION}) is too old (minimum {e.config["min_launcher_version"]}): run it with --update.')
    if not e.saved_login():
        print(f'First time: your StratusGeo hub login (asked once). Token: {TOKEN_PAGE} > Request new API token.')
        e.login(input('  Hub username: '), getpass.getpass('  Hub token (hidden): '))
    mode = '--check' if '--check' in argv else 'start'
    ask = lambda: getpass.getpass('First time on this GPU: the registration code Pranav sent you (valid 1 hour, hidden): ')
    if e.launch(mode, ask, say=say):
        print('\nYou can close this window or shut your laptop now: your GPU carries on and stops itself.\n', flush=True)
        last = None
        while (p := e.poll())['server'] != 'off':
            if p['status'] != last:
                say(p['status'])
                last = p['status']
            time.sleep(15)
        say('Your GPU server is off. Thanks for helping!')
    return 0


if __name__ == '__main__':
    try:
        rc = main(sys.argv[1:])
    except Fail as x:
        print(f'\n{x}', flush=True)
        rc = 1
    except KeyboardInterrupt:
        rc = 0
    if sys.stdin.isatty() and '--dry-run' not in sys.argv:
        input('\nPress Enter to close.')
    sys.exit(rc)
