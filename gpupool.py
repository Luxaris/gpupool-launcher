#!/usr/bin/env python3
"""Hermes engine (gpupool): lend your StratusGeo GPU to the team job pool. Stdlib only (Python 3.8+).

Used by the Hermes app (gpupool_gui.py, built into Hermes-Setup.exe / Hermes-mac-*.dmg) and by a plain CLI:
    python gpupool.py [--check | --update | --dry-run | --reset]
Everything after a launch runs on the hub server (boot.sh): the laptop may close. Talks only to
  - the JupyterHub REST API (start your default server) and the Jupyter REST API on it (files, one terminal),
  - the public repo PUBLIC (config.yml + release assets, pinned by tag, sha256-checked) for updates/server scripts.
Your hub token stays in a user-only file in your config dir; nothing of Pranav's is used or shipped.
"""
import base64, hashlib, hmac, json, os, platform, ssl, stat, subprocess, sys, tempfile, time, urllib.error, urllib.request, webbrowser
from datetime import datetime


def _ssl_context():
    """HTTPS trust for every request (hub, Jupyter, GitHub, ntfy): the system's certificates plus certifi's bundle.
    The Mac app's bundled Python sees no system certificates, so without certifi every request failed with
    CERTIFICATE_VERIFY_FAILED (v11, Mac)."""
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
    except (ImportError, OSError):
        pass
    return ctx


SSL_CTX = _ssl_context()
urllib.request.install_opener(urllib.request.build_opener(urllib.request.HTTPSHandler(context=SSL_CTX)))


def net_error(e):
    """Plain words for a network failure; certificate problems are Hermes's fault, not the teammate's token."""
    r = getattr(e, 'reason', e)
    if isinstance(r, ssl.SSLCertVerificationError) or 'CERTIFICATE_VERIFY_FAILED' in str(r):
        return ("Hermes could not verify the hub's security certificate. This is a Hermes problem, not your token: "
                f'update Hermes or tell Pranav. ({r})')
    return f'Cannot reach the hub ({r}). Check your internet connection.'


VERSION = 14
APP = 'Hermes'
HUB = 'https://hub.stratusgeo.org'
TOKEN_PAGE = f'{HUB}/hub/token'
PUBLIC = 'Luxaris/gpupool-launcher'
RAW = f'https://raw.githubusercontent.com/{PUBLIC}/main'  # public fallback for repo files (before the repo goes private)
ASSET_URL = f'https://github.com/{PUBLIC}/releases/download/{{tag}}/{{name}}'
GH_API = f'https://api.github.com/repos/{PUBLIC}'
try:  # read-only fine-grained token (Contents: read, this repo only), written into hermes_token.py by build.yml from
    from hermes_token import TOKEN as _GH_TOKEN  # the HERMES_READ_TOKEN secret; never in the repo, never logged
except ImportError:
    _GH_TOKEN = ''
GH = {'state': 'none' if not _GH_TOKEN else 'unused', 'expires': ''}  # for heartbeats: is the read token healthy?
GPU_OPTS = {'profile': 'gpu', 'image--unlisted-choice': 'quay.io/cryointhecloud/cryo-python-ai:latest'}  # = stratusgeo preset 5
SERVER_FILES = ('boot.sh', 'keepalive.sh', 'autostart.py')
JUPYTER_CFG = '.jupyter/jupyter_server_config.py'
HOOK_BEGIN, HOOK_END = '# >>> gpupool (Hermes)', '# <<< gpupool (Hermes)'
JUPYTER_HOOK = f'''{HOOK_BEGIN}: start the pool watchdog at server boot, only for servers Hermes asked for (signed request,
# see ~/gpupool/autostart.py). Does nothing otherwise and never stops Jupyter from starting. Remove this block to opt out.
try:
    import os as _gp_os, subprocess as _gp_sp, sys as _gp_sys
    _gp_a = _gp_os.path.expanduser('~/gpupool/autostart.py')
    if _gp_os.path.exists(_gp_a):
        _gp_sp.Popen([_gp_sys.executable, _gp_a], start_new_session=True, stdin=_gp_sp.DEVNULL,
                     stdout=open(_gp_os.path.expanduser('~/gpupool/autostart.log'), 'a'), stderr=_gp_sp.STDOUT)
except Exception:
    pass
{HOOK_END}
'''


def with_hook(existing):
    """jupyter_server_config.py with our block in it exactly once; the teammate's own settings are kept."""
    text = existing or ''
    if HOOK_BEGIN in text and HOOK_END in text:
        a, b = text.index(HOOK_BEGIN), text.index(HOOK_END) + len(HOOK_END)
        text = text[:a] + text[b:].lstrip('\n')
    text = text.rstrip('\n')
    return (text + '\n\n' if text.strip() else '') + JUPYTER_HOOK


INSTALL_LINE = 'bash ~/gpupool/boot.sh install'
CFG_DIR = os.path.join(os.environ.get('APPDATA') or os.path.expanduser('~/.config'), 'gpupool')
LOGIN = os.path.join(CFG_DIR, 'login.json')
HISTORY = os.path.join(CFG_DIR, 'history.txt')  # last jobs seen on the server, kept for when it is off
SPAWNED = os.path.join(CFG_DIR, 'spawned.json')  # when Hermes last asked the hub for a server (own_server)
BUSY = ('Ready', 'Running', 'Done', 'Setting', 'Saving', 'Stopping after', 'Waiting for jobs')


class Fail(Exception):
    """A step failed; the message is meant for the teammate."""


_SKEW = [0.0]  # (true time) - (this laptop's clock), from the last HTTP Date header seen


def fetch(url, headers=None, data=None, method=None, timeout=60):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        _note_date(r.headers.get('Date'))
        return r.read()


def _note_date(value):
    """Learn the real time from a server's Date header (hub, Jupyter, GitHub all send one)."""
    try:
        from email.utils import parsedate_to_datetime
        _SKEW[0] = parsedate_to_datetime(value).timestamp() - time.time()
    except (TypeError, ValueError, IndexError):
        pass


def gh_fetch(url, accept, timeout=60):
    """GitHub API read with the build-time token. The Authorization header is 'unredirected': a release asset reply
    redirects to storage that must not get it (and never sees the token). Notes token expiry/rejection in GH."""
    req = urllib.request.Request(url, headers={'Accept': accept, 'X-GitHub-Api-Version': '2022-11-28'})
    req.add_unredirected_header('Authorization', f'Bearer {_GH_TOKEN}')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            _note_date(r.headers.get('Date'))
            GH['state'] = 'ok'
            exp = r.headers.get('github-authentication-token-expiration')
            if exp:
                GH['expires'] = exp
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            GH['state'] = 'rejected'
        raise


def repo_file(path):
    """A file from the launcher repo's main branch: GitHub API with the read token (works when the repo is private),
    else the public raw URL (works while it is public, and for builds without a token)."""
    if _GH_TOKEN:
        try:
            return gh_fetch(f'{GH_API}/contents/{path}?ref=main&t={int(time.time())}', 'application/vnd.github.raw')
        except urllib.error.HTTPError as e:
            if e.code == 404:  # genuinely missing (e.g. queue.json before the first `pool add`)
                raise
        except urllib.error.URLError:
            pass
    return fetch(f'{RAW}/{path}?t={int(time.time())}', timeout=30)


def release_asset(tag, name):
    """A release asset: via the API asset id with the token (private repo), else the public download URL."""
    if _GH_TOKEN:
        try:
            rel = json.loads(gh_fetch(f'{GH_API}/releases/tags/{tag}', 'application/vnd.github+json'))
            aid = next(a['id'] for a in rel.get('assets', []) if a['name'] == name)
            return gh_fetch(f'{GH_API}/releases/assets/{aid}', 'application/octet-stream', timeout=600)
        except (urllib.error.URLError, StopIteration, KeyError, ValueError):
            pass
    return fetch(ASSET_URL.format(tag=tag, name=name), timeout=600)


def token_note():
    """'' when the read token is fine; 'rejected' or 'expires-YYYY-MM-DD' (within 30 days) for heartbeats / pool status."""
    if GH['state'] == 'rejected':
        return 'rejected'
    if GH['state'] == 'none' and getattr(sys, 'frozen', False):
        return 'missing'
    try:
        e = GH['expires'].replace(' UTC', ' +0000')  # header looks like '2027-02-03 08:00:00 UTC' or '... -0800'
        t = datetime.strptime(e, '%Y-%m-%d %H:%M:%S %z').timestamp()
    except (ValueError, TypeError):
        return ''
    return f'expires-{datetime.fromtimestamp(t):%Y-%m-%d}' if t - now() < 30 * 86400 else ''


def now():
    """The real time, not this laptop's clock. Every comparison with a hub, server or GitHub timestamp uses this:
    a laptop clock an hour off (e.g. set by hand around BC's 2026 time change) once made a healthy pool server look
    'idle 61 min, no runner' (v9)."""
    return time.time() + _SKEW[0]


def _ts(iso, default=None):
    """Hub/Jupyter ISO time ('...Z') -> epoch seconds; `default` when missing or unreadable (raises if no default)."""
    try:
        return datetime.fromisoformat(str(iso).replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError):
        if default is None:
            raise ValueError(f'bad time {iso!r}')
        return default


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
        self.config_text = repo_file('config.yml').decode()
        self.config = parse_config(self.config_text)
        return self.config

    def push_config(self):
        """Give the server its settings: boot.sh reads ~/gpupool/config.yml, so it needs no GitHub access (the launcher
        repo may be private; servers never get the token)."""
        if getattr(self, 'config_text', None):
            self.put('gpupool/config.yml', self.config_text.encode())
            self.pushed_config = self.config_text

    def asset(self, name):
        """A release asset of the pinned tag, refused unless its sha256 matches config.yml."""
        c = self.config or self.load_config()  # the setup wizard runs before the main screen has loaded it
        b = release_asset(c['launcher_tag'], name)
        if hashlib.sha256(b).hexdigest() != c.get(f'sha256 {name}', ''):
            raise Fail(f'{name} from {c["launcher_tag"]} failed its checksum; not using it. Tell Pranav.')
        return b

    @staticmethod
    def queue():
        """The public pool indicator: {'waiting': n, 'set': opaque id of those jobs, 'updated': iso}. No login needed."""
        try:
            return json.loads(repo_file('queue.json'))
        except urllib.error.HTTPError as e:
            if e.code == 404:  # not published yet
                return {'waiting': 0, 'set': ''}
            raise

    def update_needed(self):
        return int(self.config.get('launcher_version', VERSION)) > VERSION

    def too_old(self):
        return VERSION < int(self.config.get('min_launcher_version', 0))

    def auto_update(self):
        """config.yml `auto_update: true|false` (default true): Pranav can switch automatic updates off for everyone."""
        return str(self.config.get('auto_update', 'true')).lower() not in ('false', 'no', 'off', '0')

    def release_age_ok(self, now=None):
        """Only auto-install a release published at least min_update_age_minutes ago (config, default 30), so a bad
        release can be pulled first. `launcher_published` is written by `pool publish`; unknown age = not yet."""
        try:
            t = datetime.fromisoformat(self.config['launcher_published'].replace('Z', '+00:00')).timestamp()
        except (KeyError, ValueError):
            return False
        return (now or globals()['now']()) - t >= 60 * int(self.config.get('min_update_age_minutes', 30))

    def update(self, relaunch=True, hidden=False):
        """Install the pinned, sha256-checked version, then (relaunch=True) start the new copy with --updated and return
        'exit': the caller must quit right away. Windows: the Inno installer /VERYSILENT for this user; it waits for
        this app to close and its [Run] entry reopens Hermes. Mac: copy the .app out of the .dmg in place (hdiutil +
        ditto) and reopen it. Script copy: replace the two files and restart them. Returns 'dry' in dry-run."""
        args = ['--updated'] + (['--background'] if hidden else [])
        name = app_asset()
        if name is None:
            files = {n: self.asset(n) for n in ('gpupool.py', 'gpupool_gui.py')}
            if self.dry:
                return 'dry'
            here = os.path.dirname(os.path.abspath(__file__))
            for n, b in files.items():
                with open(os.path.join(here, n + '.new'), 'wb') as f:
                    f.write(b)
                os.replace(os.path.join(here, n + '.new'), os.path.join(here, n))
            if relaunch:
                subprocess.Popen([sys.executable, os.path.join(here, 'gpupool_gui.py')] + args)
            return 'exit'
        b = self.asset(name)
        if self.dry:
            return 'dry'
        path = os.path.join(tempfile.mkdtemp(), name)
        with open(path, 'wb') as f:
            f.write(b)
        if os.name == 'nt':
            cmd = [path, '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CURRENTUSER']
            if relaunch:
                cmd += ['/relaunch=1'] + (['/hidden=1'] if hidden else [])
            subprocess.Popen(cmd)
            return 'exit'
        bundle = os.path.abspath(os.path.join(sys.executable, '..', '..', '..'))  # the installed .app (any folder name)
        mnt = tempfile.mkdtemp()
        subprocess.run(['hdiutil', 'attach', '-nobrowse', '-quiet', '-mountpoint', mnt, path], check=True)
        try:
            subprocess.run(['ditto', os.path.join(mnt, f'{APP}.app'), bundle], check=True)
        finally:
            subprocess.run(['hdiutil', 'detach', '-quiet', mnt])
        if relaunch:
            subprocess.Popen(['open', '-n', bundle, '--args'] + args)
        return 'exit'

    # ---- JupyterHub + Jupyter REST ----
    def api(self, method, url, body=None, raw=False):
        h = {'Authorization': f'token {self.token}'}
        data = None
        if body is not None:
            h['Content-Type'] = 'application/json'
            data = json.dumps(body).encode()
        if self.user and url.startswith(self.base):  # our own calls into the server also count as "activity" on the hub
            self.probes = (getattr(self, 'probes', []) + [now()])[-50:]
        b = fetch(url, h, data, method)
        return b if raw else (json.loads(b) if b else None)

    @property
    def base(self):
        return f'{HUB}/user/{self.user}'

    def server_record(self):
        """Your default server as the hub reports it ({} when off)."""
        try:
            u = self.api('GET', f'{HUB}/hub/api/users/{self.user}')
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404):
                raise Fail('The hub did not accept your username/token.')
            raise Fail(f'Cannot reach the hub (HTTP {e.code}).')
        except urllib.error.URLError as e:
            raise Fail(net_error(e))
        return (u.get('servers') or {}).get('') or {}

    def state(self):
        """off | pending | gpu | cpu  (your default server)"""
        s = self.server_record()
        if not s:
            return 'off'
        if not s.get('ready'):
            return 'pending'
        return 'gpu' if (s.get('user_options') or {}).get('profile') == 'gpu' else 'cpu'

    def boot_alive(self):
        """boot.sh rewrites ~/gpupool/status.txt every 10 s while it runs: fresh = the pool watchdog is alive."""
        try:
            m = self.api('GET', f'{self.base}/api/contents/gpupool/status.txt?content=0')['last_modified']
            return now() - _ts(m) < 180  # real time (Date header of this very reply), not the laptop clock
        except (urllib.error.HTTPError, KeyError, ValueError, TypeError):
            return False

    def boot_alive_strict(self, gap=15):
        """Final check before Hermes takes over or stops a server: status.txt written again within `gap` s (two looks,
        compared with each other only: no clocks involved), or fresh by the server's own clock."""
        def mtime():
            try:
                return self.api('GET', f'{self.base}/api/contents/gpupool/status.txt?content=0')['last_modified']
            except (urllib.error.HTTPError, KeyError, TypeError):
                return None
        first = mtime()
        time.sleep(gap)
        second = mtime()
        return (first is not None and second != first) or self.boot_alive()

    def own_server(self, rec=None):
        """True when Hermes itself started the server that is up now (spawn time noted on this laptop, matched
        against the hub's start time)."""
        try:
            t = json.load(open(SPAWNED))['t']
            started = _ts((rec or self.server_record())['started'])
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return False
        return t - 120 <= started <= t + 3600

    def needs_resume(self, st):
        """A GPU server Hermes started is up but its pool watchdog never ran (laptop slept or lost network mid-launch,
        the terminal call failed, ...): finish the launch instead of leaving an idle GPU on."""
        return st == 'gpu' and not self.boot_alive() and self.own_server()

    def user_idle(self, rec, now=None):
        """Seconds since someone last USED the server: the hub's last_activity, except bumps our own API calls into the
        server caused (the hub folds proxy traffic into last_activity up to ~5 min late). Starts at the server's start."""
        now = now or globals()['now']()
        key = rec.get('started', '')
        seen = getattr(self, 'user_act', {})
        last = seen.get(key) or _ts(key, now)
        la = _ts(rec.get('last_activity'), 0)
        if la > last and not any(p - 5 <= la <= p + 330 for p in getattr(self, 'probes', [])):
            last = la
        self.user_act = {key: last}
        return now - last

    def server_check(self, limit):
        """One look at your hub server for the safety rules (App.server_action): state, whether Hermes started it,
        how long nobody used it, and whether its pool watchdog is alive. The watchdog check touches the server, so
        it only runs when it matters: our own server, or one idle for `limit` seconds."""
        rec = self.server_record()
        st = 'off' if not rec else 'pending' if not rec.get('ready') else \
            'gpu' if (rec.get('user_options') or {}).get('profile') == 'gpu' else 'cpu'
        info = {'st': st, 'started': rec.get('started', ''), 'own': False, 'idle': 0, 'alive': False}
        if st in ('gpu', 'cpu'):
            info['own'] = self.own_server(rec)
            info['idle'] = self.user_idle(rec)
            if info['own'] or info['idle'] >= limit:
                info['alive'] = self.boot_alive()
        return info

    def adopt(self):
        """Make the running server a normal pool server: fresh scripts, armed boot.sh, started through a terminal
        (then the usual idle / jobs_per_launch / 8 h limits apply). Raises Fail if the start line does not run."""
        self.prepare('start')
        if self.trigger() == 'paste':
            raise Fail('the one-time start line is not installed on this server')

    def stop_server(self):
        """Stop your default server (the same hub API call boot.sh's hub_stop makes)."""
        self.api('DELETE', f'{HUB}/hub/api/users/{self.user}/server')

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
        return st == 'gpu' and self.text('gpupool/status.txt').startswith(BUSY) and self.boot_alive()

    def server_up(self, say=print):
        """Start your default server with a GPU and wait until it is ready. Leaves a server you started yourself alone.
        Returns 'pooled' (already working), 'resume' (Hermes's own server is up without its watchdog), 'up', 'dry'."""
        st = self.state()
        if self.pooled(st):
            return 'pooled'
        if self.needs_resume(st):
            say('Your GPU server is on but its job runner never started; starting it now...')
            return 'resume'
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
                os.makedirs(CFG_DIR, exist_ok=True)
                with open(SPAWNED, 'w') as f:  # lets a later check finish this launch (own_server)
                    json.dump({'t': now()}, f)
                self.post_spawn_request()  # lets the server start its own watchdog even if this laptop goes away now
                try:
                    self.api('POST', f'{HUB}/hub/api/users/{self.user}/server', GPU_OPTS)
                except urllib.error.HTTPError as e:
                    raise Fail(f'The hub would not start a GPU server: {e.read().decode("utf-8", "replace")[:300]}')
            t0, note, seen, errs = time.time(), '', 0, 0
            while True:
                try:
                    st = self.state()
                except Fail:  # network hiccup, or the laptop just woke up: keep waiting a while
                    errs += 1
                    if errs > 60:
                        raise
                    time.sleep(10)
                    continue
                if st != 'pending':
                    break
                if time.time() - t0 > 1200:  # (a sleeping laptop also lands here: the next check resumes the launch)
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
        if not self.dry:
            self.api('PUT', f'{self.base}/api/contents/gpupool', {'type': 'directory'})
            self.install_autostart()
        if self.dry:
            return
        self.api('PUT', f'{self.base}/api/contents/gpupool', {'type': 'directory'})
        for n, b in scripts.items():
            self.put(f'gpupool/{n}', b)
        for n in ('status.txt', 'selftest.txt', 'stop'):
            self.rm(f'gpupool/{n}')
        if code:
            self.put('gpupool/regtoken', code.strip().encode())
        self.push_config()
        self.put('gpupool/armed', mode.encode())

    # ---- server-side autostart: a server Hermes spawned starts its own watchdog, laptop or not ----
    def spawn_key(self):
        """The secret that signs spawn requests: in the teammate's home (~/gpupool/spawn_key) and on this laptop."""
        path = os.path.join(CFG_DIR, f'spawn_key-{self.user}')
        key = open(path).read().strip() if os.path.exists(path) else ''
        return key, path

    def install_autostart(self):
        """Once per launch (server running): the spawn key on both sides and our block in jupyter_server_config.py."""
        key, path = self.spawn_key()
        remote = self.text('gpupool/spawn_key').strip()
        if remote and remote != key:
            key = remote  # another laptop set it up: use the same key
        if not key:
            key = base64.b16encode(os.urandom(24)).decode().lower()
        if remote != key:
            self.put('gpupool/spawn_key', key.encode())
        os.makedirs(CFG_DIR, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, 'w') as f:
            f.write(key)
        if not self.exists('.jupyter'):
            self.api('PUT', f'{self.base}/api/contents/.jupyter', {'type': 'directory'})
        old = self.text(JUPYTER_CFG) if self.exists(JUPYTER_CFG) else ''
        new = with_hook(old)
        if new != old:
            self.put(JUPYTER_CFG, new.encode())

    def post_spawn_request(self):
        """'gpupool-spawn <user> <time> <hmac>' to the pool's ntfy topic, read by the new server's autostart.py."""
        key, _ = self.spawn_key()
        topic = (self.config or {}).get('heartbeat_topic')
        if not key or not topic:
            return  # set up before v14 (no key yet) or no topic: the laptop-driven start still works
        t = int(now())
        sig = hmac.new(key.encode(), f'{self.user}|{t}'.encode(), hashlib.sha256).hexdigest()
        try:
            fetch(f'https://ntfy.sh/{topic}', data=f'gpupool-spawn {self.user} {t} {sig}'.encode(), method='POST', timeout=15)
        except Exception:
            pass

    def trigger(self):
        """Start boot.sh through a Jupyter terminal (its login shell runs the ~/.profile hook), then check it ran:
        boot.sh deletes ~/gpupool/armed first thing. Up to 3 tries (a just-started server can refuse terminals for a
        moment). 'paste' = the one-time hook install is still needed."""
        if self.dry:
            return 'dry'
        if not self.exists('gpupool/hooked'):
            return 'paste'
        why = ''
        for wait in (5, 15, 30):
            try:
                t = self.api('POST', f'{self.base}/api/terminals')
                time.sleep(wait)
                self.api('DELETE', f'{self.base}/api/terminals/{t["name"]}')
            except (urllib.error.URLError, KeyError, TypeError) as e:  # HTTPError is a URLError
                why = f' ({type(e).__name__}: {e})'
                time.sleep(wait)
                continue
            if not self.exists('gpupool/armed'):
                return 'started'
            why = ' (the terminal opened, but the start line in ~/.profile did not run)'
        raise Fail(f'Could not start the job runner on your server{why}. Open a terminal on the hub and run: '
                   f'{INSTALL_LINE}')

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
        raise Fail(f'Cannot read the pool settings (config.yml): {x}')
    if '--update' in argv or e.update_needed():
        say(f'Updating {APP} {VERSION} -> {e.config["launcher_version"]}...')
        e.update(relaunch=False)
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
