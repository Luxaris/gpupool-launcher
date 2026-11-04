#!/usr/bin/env python3
"""Hermes: a small Tkinter window around gpupool.Engine (all real work is in gpupool.py).

First run: a setup wizard (welcome, hub login, registration code, one-time terminal paste, self-test, done).
After that: one big status line, Start, Stop after current job, Update (when a newer version exists), the last jobs
with a check or a cross, and a "notify me if something fails" switch (an in-window banner; also a macOS notification).
Background mode (opt-in, off by default): Hermes starts at login (Windows: HKCU Run value; Mac: a user LaunchAgent),
sits in the tray (Windows, pystray; on a Mac the Dock icon instead: pystray and Tk both need the Mac main thread)
and every 5 min reads the public, count-only queue.json; jobs waiting + GPU off = the same launch as Start.
Engine calls run in a worker thread; the window never blocks. Closing the window changes nothing on the server.
"""
import json, os, queue, subprocess, sys, threading, time, tkinter as tk, webbrowser
from datetime import datetime
from tkinter import ttk

import gpupool as gp

SETTINGS = os.path.join(gp.CFG_DIR, 'settings.json')
POLL_S = 15
CHECKS = [  # (what the teammate reads, text in the server's PASS/FAIL lines, what to do on FAIL)
    ('Hub login', 'hub login', 'Your hub username or token was not accepted. Go back to step 1 and paste a fresh token.'),
    ('GPU server started', 'GPU server', 'The hub did not give you a GPU. Try again later; GPUs can be busy.'),
    ('Job runner online', 'runner', 'The job runner did not start. Your code may have expired: ask Pranav for a new one.'),
    ('CPU test job', 'cpu:', 'Python on your server did not work. Send Pranav the error.'),
    ('GPU test job (PyTorch)', 'gpu:', 'PyTorch could not use the GPU. Send Pranav the error.'),
    ('Reading team data from S3', 's3:', 'Your server cannot read the team data. Pranav needs to check your S3 access.'),
    ('Writing a result file', 'results:', 'The test could not write its result. Send Pranav the error.'),
    ('Results and log saved, read back', 'saved to S3', 'Saving to S3 or GitHub did not work. Send Pranav the error.'),
    ('Nothing else on your server touched', 'home', 'The test changed files it should not have. Send Pranav the error.'),
]


def load_settings():
    try:
        return json.load(open(SETTINGS))
    except (OSError, ValueError):
        return {}


def save_settings(s):
    os.makedirs(gp.CFG_DIR, exist_ok=True)
    with open(SETTINGS, 'w') as f:
        json.dump(s, f)


def local_time(iso):
    """The server records UTC (2026-11-03T21:10:00Z); show it in this computer's time zone."""
    try:
        return datetime.fromisoformat(iso.replace('Z', '+00:00')).astimezone().strftime('%a %d %b %H:%M')
    except ValueError:
        return iso


def os_notify(title, msg):
    if sys.platform == 'darwin':  # built-in; on Windows the in-window banner (plus a beep) is the notification
        subprocess.run(['osascript', '-e', f'display notification {json.dumps(msg)} with title {json.dumps(title)}'], check=False)


LOG = os.path.join(gp.CFG_DIR, 'hermes.log')


def log_line(text):
    """Local diagnostics (what background mode decided, callback errors); trimmed to the last ~200 KB. No secrets."""
    try:
        os.makedirs(gp.CFG_DIR, exist_ok=True)
        if os.path.exists(LOG) and os.path.getsize(LOG) > 400_000:
            keep = open(LOG, 'rb').read()[-200_000:]
            open(LOG, 'wb').write(keep)
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(f'{datetime.now():%Y-%m-%d %H:%M:%S} {text}\n')
    except OSError:
        pass


BG_EVERY_S = 120
HEARTBEAT_S = 1800
BG_SENTENCE = ('Hermes starts in the tray at login and turns your GPU on only when team jobs are waiting '
               '(or Pranav asks); untick any time.')
WAKE_VALID_S = 3600  # a `pool wake` request older than this is ignored (the laptop was off or asleep at the time)
WARN_S = 300  # warning before Hermes takes over or stops an idle hub server that has no pool runner
KEEP_S = 7200  # "Keep my server": leave it alone this long
UPDATE_EVERY_S = 3600  # config.yml re-check for a newer Hermes (also at every app start)
UPDATE_RETRY_S = 86400  # at most one automatic install attempt per version per day
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
AGENT = os.path.expanduser('~/Library/LaunchAgents/org.teamsound.hermes.plist')
try:  # Windows tray icon; optional (no tray -> the window just minimises)
    if os.name != 'nt':
        raise ImportError
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    pystray = None


def launch_cmd():
    """How the OS should start Hermes at login: the installed app, or this script with pythonw."""
    if getattr(sys, 'frozen', False):
        return [sys.executable, '--background']
    py = sys.executable.replace('python.exe', 'pythonw.exe') if os.name == 'nt' else sys.executable
    return [py, os.path.abspath(__file__), '--background']


def set_autostart(on):
    """Start at login for this user only. Turning it off removes the entry (the uninstaller removes it too)."""
    if os.name == 'nt':
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, gp.APP, 0, winreg.REG_SZ, subprocess.list2cmdline(launch_cmd()))
            else:
                try:
                    winreg.DeleteValue(k, gp.APP)
                except FileNotFoundError:
                    pass
    elif sys.platform == 'darwin':
        import plistlib
        if on:
            os.makedirs(os.path.dirname(AGENT), exist_ok=True)
            with open(AGENT, 'wb') as f:
                plistlib.dump({'Label': 'org.teamsound.hermes', 'ProgramArguments': launch_cmd(), 'RunAtLoad': True}, f)
        elif os.path.exists(AGENT):
            os.remove(AGENT)


def wake_time(q, user):
    """Latest `pool wake` request for this user (or 'all'), as epoch seconds; 0 if none."""
    ts = [v for k, v in (q.get('wake') or {}).items() if k in (user, 'all')]
    try:
        return max(datetime.fromisoformat(t.replace('Z', '+00:00')).timestamp() for t in ts) if ts else 0
    except ValueError:
        return 0


def should_autostart(q, settings, server, now, user=''):
    """Background decision, kept pure for tests: start when (new waiting jobs, or a fresh `pool wake` for this user)
    and the hub server is off and not paused. One automatic start per new set of waiting jobs / per wake request, so a
    stale indicator (or jobs nobody can take) costs at most one start, never a start-stop loop."""
    if not settings.get('background') or settings.get('paused_until', 0) > now or server != 'off':
        return False
    jobs = int(q.get('waiting', 0)) > 0 and q.get('set', '') != settings.get('last_auto_set')
    w = wake_time(q, user)
    return jobs or (w > settings.get('last_wake', 0) and now - w < WAKE_VALID_S)


def should_auto_update(e, settings, now, safe):
    """Pure, for tests: a newer pinned version, auto_update on, released long enough ago, not already tried today,
    and a safe moment (see App.safe_to_update)."""
    if not e.config or not e.update_needed() or not e.auto_update() or not e.release_age_ok(now):
        return False
    tried = settings.get('update_try') or {}
    if str(tried.get('v')) == str(e.config['launcher_version']) and now - tried.get('t', 0) < UPDATE_RETRY_S:
        return False
    return safe


def server_action(info, r, now, limit, may_adopt):
    """Pure, for tests. What to do with a hub server that is on (info from Engine.server_check; r = what Hermes
    already did to this server). Never touches a server someone used in the last `limit` s (stuck_stop_minutes), or
    one they asked to keep. Hermes's own launch: take it over at once (3 tries). Any other idle one without a pool
    runner: warn, 5 min later take it over (background mode on) or stop it; take-over failed 3x: stop it.
      none | active | kept | wait | warn | adopt | stop"""
    if info['st'] not in ('gpu', 'cpu') or info['alive']:
        return 'none'
    if r.get('keep_until', 0) > now:
        return 'kept'
    tries = r.get('tries', 0)
    can_adopt = may_adopt and info['st'] == 'gpu' and tries < 3
    if info['own'] and can_adopt:
        return 'adopt'
    if info['idle'] < limit:
        return 'active'
    if not r.get('warned_at'):
        return 'warn'
    if now - r['warned_at'] < WARN_S:
        return 'wait'
    return 'adopt' if can_adopt else 'stop'


def tomorrow_morning(now):
    t = datetime.fromtimestamp(now).replace(hour=7, minute=0, second=0, microsecond=0)
    return (t.timestamp() + 86400) if t.timestamp() <= now else t.timestamp()


def tray_image(state):
    """A dot: green = GPU on, orange = paused, grey = idle."""
    img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((6, 6, 58, 58), fill={'gpu': '#1b9e4b', 'paused': '#e08a00'}.get(state, '#8a8a8a'))
    return img


class App:
    def __init__(self, root, engine):
        self.root, self.e, self.q = root, engine, queue.Queue()
        self.settings = load_settings()
        self.code, self.screen, self.last, self.poll_job = None, None, {}, None
        root.title(gp.APP)
        root.geometry('560x660')
        root.minsize(520, 620)
        st = ttk.Style()
        st.configure('Big.TLabel', font=('Segoe UI', 17, 'bold'))
        st.configure('H.TLabel', font=('Segoe UI', 14, 'bold'))
        st.configure('Small.TLabel', foreground='#666')
        st.configure('Banner.TLabel', background='#fde2e1', foreground='#8a1c1c', padding=8)
        st.configure('Banner.TFrame', background='#fde2e1')
        st.configure('Code.TLabel', font=('Consolas', 12), background='#f2f2f2', padding=8)
        st.configure('Accent.TButton', font=('Segoe UI', 11, 'bold'), padding=8)
        st.configure('Main.TButton', font=('Segoe UI', 11), padding=8)  # Stop / Update: same height as Start
        self.body = ttk.Frame(root, padding=24)
        self.body.pack(fill='both', expand=True)
        root.after(100, self._pump)
        self.tray, self.tray_state = None, None
        self.starting, self.updating, self.auto_started_at, self.taking_over = False, False, 0.0, False
        self.bg_note = ''
        root.protocol('WM_DELETE_WINDOW', self.on_close)
        if self.settings.get('background'):
            self.start_tray()
        root.after(60 * 1000, self._bg_tick)
        root.after(UPDATE_EVERY_S * 1000, self._update_tick)

    # ---- background mode: autostart at login, tray, queue check every 5 min ----
    def on_close(self):
        if not self.settings.get('background'):
            self.quit()
        elif self.tray:
            self.root.withdraw()  # keeps running in the tray
        else:
            self.root.iconify()  # Mac / no tray: keeps running in the Dock

    def show(self):
        self.root.deiconify()
        self.root.lift()
        if self.screen == 'main':
            self.refresh()

    def quit(self):
        if self.tray:
            self.tray.stop()
        self.root.destroy()

    def visible(self):
        return self.root.state() == 'normal'

    def toggle_background(self):
        on = self.bg_var.get()
        if not self.set_background(on):
            self.bg_var.set(not on)

    def heartbeat(self, force=False):
        """'<hub user> background on|off v<N> last=<what background mode last did>' to the public ntfy topic in
        config.yml, so Pranav's `pool status` shows who lets him start their GPU and why one did not start. Status
        only (no secrets, anyone can read or fake it); every 30 min, and at once when the outcome changes."""
        topic = (self.e.config or {}).get('heartbeat_topic')
        if not topic or not self.e.user or not (force or time.time() - self.settings.get('last_heartbeat', 0) > HEARTBEAT_S):
            return
        self.settings['last_heartbeat'] = time.time()
        note = ' '.join(self.bg_note.split())[:120]
        tok = gp.token_note()  # read-token trouble ('rejected' / 'expires-YYYY-MM-DD'), for Pranav's `pool status`
        msg = (f'{self.e.user} background {"on" if self.settings.get("background") else "off"} v{gp.VERSION}'
               f'{" tok=" + tok if tok else ""}{" last=" + note if note else ""}')
        self.bg(lambda: gp.fetch(f'https://ntfy.sh/{topic}', data=msg.encode(), method='POST', timeout=15), None, lambda m: None)

    def note(self, text):
        """Remember (and log) what background mode just did; a change is reported in the next heartbeat right away."""
        log_line(f'background: {text}')
        if text != self.bg_note:
            self.bg_note = text
            self.heartbeat(force=True)

    def pause(self, until):
        self.settings['paused_until'] = until
        save_settings(self.settings)
        self.set_tray_state(self.tray_state)

    def start_tray(self):
        if not pystray or self.tray:
            return
        post = lambda f: (lambda *_: self.q.put((lambda _: f(), None)))  # tray thread -> Tk thread
        menu = pystray.Menu(
            pystray.MenuItem('Open', post(self.show), default=True),
            pystray.MenuItem('Pause for 1 hour', post(lambda: self.pause(gp.now() + 3600))),
            pystray.MenuItem('Pause until tomorrow', post(lambda: self.pause(tomorrow_morning(gp.now())))),
            pystray.MenuItem('Resume', post(lambda: self.pause(0))),
            pystray.MenuItem('Keep my server (2 h)', post(self.keep_server)),
            pystray.MenuItem('Quit', post(self.quit)))
        self.tray = pystray.Icon(gp.APP, tray_image('idle'), gp.APP, menu)
        self.tray.run_detached()

    def stop_tray(self):
        if self.tray:
            self.tray.stop()
            self.tray = None

    def set_tray_state(self, server):
        self.tray_state = server
        paused = self.settings.get('paused_until', 0) > gp.now()
        state = 'paused' if paused else 'gpu' if server in ('gpu', 'pending') else 'idle'
        text = {'paused': f'{gp.APP}: paused', 'gpu': f'{gp.APP}: GPU on', 'idle': f'{gp.APP}: waiting for team jobs'}[state]
        if self.tray:
            self.tray.icon, self.tray.title = tray_image(state), text
        elif self.settings.get('background'):
            self.root.title(text)  # Mac: the Dock / window title shows the state

    def _bg_tick(self):
        self.root.after(BG_EVERY_S * 1000, self._bg_tick)
        if not self.settings.get('setup_done') or not self.e.user:
            return
        on = bool(self.settings.get('background'))
        if on:
            self.heartbeat()
        # every 2 min, background mode on or off: fresh settings (v9 read them hourly, so a remote change came too
        # late), the queue, and one safety look at the hub server (server_action)
        def look():
            self.refresh_config()
            return self.e.queue() if on else {}, self.e.server_check(self.stuck_limit())
        self.bg(look, self._bg_decide, lambda m: self.note(f'check failed: {m}'))

    def refresh_config(self):
        try:
            self.e.load_config()
        except Exception as x:  # keep the last settings; GitHub hiccups must not stop the checks
            log_line(f'config re-read failed: {type(x).__name__}: {x}')

    def stuck_limit(self):
        return 60 * int((self.e.config or {}).get('stuck_stop_minutes', 30))

    def still_due(self, info):
        """Right before taking over or stopping a server that is not Hermes's own launch: re-read config.yml (Pranav
        may just have raised stuck_stop_minutes) and check again that the server is idle long enough."""
        self.refresh_config()
        return info['own'] or info['idle'] >= self.stuck_limit()

    def _bg_decide(self, qs):
        q, info = qs
        self.cur_server = info.get('started', '')  # for "Keep my server"
        server = info['st']
        self.set_tray_state(server)
        now = gp.now()  # real time: wake requests and hub times come from other clocks
        limit = self.stuck_limit()
        if info.get('alive') and info.get('own') and getattr(self.e, 'config_text', None) != getattr(self.e, 'pushed_config', None):
            self.bg(self.e.push_config, None, lambda m: None)  # boot.sh reads its settings from the copy Hermes uploads
        if server in ('gpu', 'cpu') and not self.starting and not self.taking_over:
            return self.take_control(info, now, limit)
        if not self.settings.get('background'):
            return
        trigger = int(q.get('waiting', 0)) > 0 or wake_time(q, self.e.user) > self.settings.get('last_wake', 0)
        if self.settings.get('paused_until', 0) > now:
            return self.note('paused') if trigger else None
        if server != 'off':
            return self.note(f'skip: your hub server is {server}') if trigger else None
        if self.settings.get('fresh_start') and self.screen == 'main' and not self.starting:
            self.settings.pop('fresh_start')  # Hermes just stopped a stuck server: start a controlled one in its place
            if now - self.settings.get('fresh_at', 0) < KEEP_S:  # at most one replacement server per 2 h: no loop
                save_settings(self.settings)
                return self.note('stuck server stopped; not starting another replacement within 2 h')
            self.settings['fresh_at'] = now
            save_settings(self.settings)
            self.note('starting a fresh GPU server (the old one was stuck)')
            return self.start()
        if not should_autostart(q, self.settings, server, now, self.e.user):
            return
        if self.screen != 'main' and self.settings.get('setup_done') and not self.visible():
            # hidden in the tray but left on a setup step (e.g. an earlier background start hit a hiccup and opened
            # Step 2/3): nobody is typing there, so go back to the main view instead of skipping every start
            log_line(f'background: hidden on {self.screen}, back to the main view')
            self.main()
        if self.screen != 'main':
            return self.note(f'skip: Hermes is on the {self.screen} screen')
        if self.e.config and self.e.too_old():  # (the Start button state may be stale while hidden in the tray)
            return self.note('skip: this version is too old; updating')
        if self.starting:
            return
        # mark the jobs/wake as handled only once the start worked; a failed start is retried on later ticks (3x)
        key = f'{q.get("set", "")}|{wake_time(q, self.e.user)}'
        fails = self.settings.get('auto_fail', {})
        if fails.get('key') == key and fails.get('n', 0) >= 3:
            return
        self.auto_started_at = now
        self.note('starting your GPU (background mode)')
        self.start(auto=(q.get('set', ''), wake_time(q, self.e.user), key))

    def take_control(self, info, now, limit):
        """Apply server_action to a hub server that is on: adopt it into the pool, warn, or stop it (see there)."""
        r = self.settings.get('rogue') or {}
        if r.get('server') != info['started']:
            r = {'server': info['started']}  # a different server than last time: start over
        may_adopt = info['own'] or (bool(self.settings.get('background')) and self.settings.get('paused_until', 0) <= now)
        act = server_action(info, r, now, limit, may_adopt)
        mins = limit // 60
        if act == 'active':
            r.pop('warned_at', None)
            if not info['own']:
                self.note('your hub server is on and recently used: left alone')
        elif act == 'kept':
            self.note('your hub server: kept by you, left alone for now')
        elif act == 'warn':
            r['warned_at'] = now
            what = 'takes it over for pool jobs' if may_adopt and info['st'] == 'gpu' and r.get('tries', 0) < 3 else 'stops it'
            self.warn_keep(f'Your GPU server is on but idle and not doing pool jobs; {gp.APP} {what} in 5 min. '
                           'Click "Keep my server" to leave it alone for 2 h.')
            self.note(f'warned: idle {int(info["idle"] / 60)} min, no runner; acting in 5 min')
        elif act == 'adopt':
            r['tries'] = r.get('tries', 0) + 1
            self.taking_over = True
            self.note(f'taking over the server for pool jobs ({r["tries"]}/3)')

            def ok(res):
                self.taking_over = False
                if res == 'alive':  # the final clock-free check found boot.sh running: not a stuck server after all
                    r['tries'] -= 1
                    save_settings(self.settings)
                    return self.note('pool watchdog is running after all: left alone')
                self.note('took over the server: pool watchdog started')

            def bad(m):
                self.taking_over = False
                self.note(f'take-over failed ({r["tries"]}/3): {m}')
            self.bg(lambda: self.guarded(info, self.e.adopt), ok, bad)
        elif act == 'stop':
            self.taking_over = True

            def stopped(res):
                self.taking_over = False
                if res == 'alive':
                    return self.note('pool watchdog is running after all: not stopped')
                self.note(f'stopped: no runner, idle {mins} min')
                if self.settings.get('background'):
                    self.settings['fresh_start'] = True  # a controlled server takes its place on the next check
                    save_settings(self.settings)
                if self.screen == 'main':
                    self.sub.configure(text=f'Stopped your GPU server: idle for {mins} min without a job runner.')

            def failed(m):
                self.taking_over = False
                self.note(f'could not stop the idle server: {m}')
            self.bg(lambda: self.guarded(info, self.e.stop_server), stopped, failed)
        self.settings['rogue'] = r
        save_settings(self.settings)

    def guarded(self, info, action):
        """Worker side of a take-over / stop: only if still due with fresh settings, and boot.sh really is not running."""
        if not self.still_due(info):
            return 'alive'  # (reported like 'running after all': nothing was done)
        if self.e.boot_alive_strict():
            return 'alive'
        action()
        return 'done'

    def keep_server(self):
        """'Keep my server': leave the current hub server alone for 2 h."""
        r = self.settings.get('rogue') or {}
        cur = getattr(self, 'cur_server', '')
        if cur and r.get('server') != cur:
            r = {'server': cur}
        r['keep_until'] = gp.now() + KEEP_S
        r.pop('warned_at', None)
        self.settings['rogue'] = r
        save_settings(self.settings)
        self.note('kept by you for 2 h')
        if self.screen == 'main':
            self.banner_box.pack_forget()

    def warn_keep(self, msg):
        """The 5-min warning: tray balloon (Windows) / notification (Mac) and a banner with a Keep button."""
        if self.tray:
            try:
                self.tray.notify(msg, gp.APP)
            except Exception:
                pass
        os_notify(gp.APP, msg)
        if self.screen == 'main':
            self.banner(msg, retry=False)
            if not any(isinstance(w, ttk.Button) and w.cget('text') == 'Keep my server' for w in self.banner_box.winfo_children()):
                ttk.Button(self.banner_box, text='Keep my server', command=self.keep_server).pack(side='right', padx=(6, 0))

    # ---- background work: fn runs in a thread, done(result) / fail(message) run in the Tk thread ----
    def bg(self, fn, done=None, fail=None):
        def work():
            try:
                r = fn()
                self.q.put((done, r))
            except gp.Fail as x:
                self.q.put((fail or self.banner, str(x)))
            except Exception as x:  # network hiccups etc.: show them plainly, never crash the window
                self.q.put((fail or self.banner, f'{type(x).__name__}: {x}'))
        threading.Thread(target=work, daemon=True).start()

    def _pump(self):
        """Run worker results in the Tk thread. Rescheduled FIRST and each callback guarded: before v8 one exception
        here silently stopped every later callback (background starts included) while heartbeats kept going."""
        self.root.after(100, self._pump)
        while not self.q.empty():
            cb, arg = self.q.get()
            if cb:
                try:
                    cb(arg)
                except Exception as x:
                    log_line(f'callback error: {type(x).__name__}: {x}')

    # ---- layout helpers ----
    def clear(self):
        if self.poll_job:
            self.root.after_cancel(self.poll_job)
            self.poll_job = None
        for w in self.body.winfo_children():
            w.destroy()

    def label(self, text, style=None, wrap=500, **pack):
        w = ttk.Label(self.body, text=text, style=style or 'TLabel', wraplength=wrap, justify='left')
        w.pack(anchor='w', **({'pady': (0, 10)} | pack))
        return w

    def wizard(self, step, title, back=None, next_text='Next', on_next=None):
        """A wizard page: progress dots, title, then the caller adds content; Back/Next at the bottom."""
        self.clear()
        self.screen = f'wizard{step}'
        dots = ttk.Label(self.body, text='  '.join('●' if i == step else '○' for i in range(6)), style='Small.TLabel')
        dots.pack(anchor='center', pady=(0, 14))
        self.label(title, 'H.TLabel')
        nav = ttk.Frame(self.body)
        nav.pack(side='bottom', fill='x', pady=(12, 0))
        if back:
            ttk.Button(nav, text='Back', command=back).pack(side='left')
        self.next_btn = ttk.Button(nav, text=next_text, style='Accent.TButton', command=on_next) if on_next else None
        if self.next_btn:
            self.next_btn.pack(side='right')
        self.err = ttk.Label(self.body, text='', foreground='#b00020', wraplength=500)
        self.err.pack(side='bottom', anchor='w')

    def copy(self, text, btn=None):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        if btn:
            btn.configure(text='Copied ✔')

    # ---- setup wizard ----
    def w_welcome(self):
        self.wizard(0, 'Lend your GPU to the team', on_next=self.w_login)
        self.welcome_update = ttk.Frame(self.body)  # update notices / the manual Update fallback (offer_manual, notice)
        self.welcome_update.pack(fill='x')
        self.label(f'Welcome to {gp.APP}. When you have free time, press Start: your StratusGeo GPU switches on, runs a couple '
                   'of the team\'s queued experiments and switches itself off. Every log and result is saved to the team cloud and GitHub.')
        self.label('Setup takes about 15 minutes, once. After you press Start you can close this window or shut your laptop.')
        self.label('If Windows showed "Windows protected your PC" when you installed this: that is normal for small team apps. '
                   'Click More info > Run anyway. On a Mac: right-click the app > Open > Open (first time only).', 'Small.TLabel')

    def w_login(self):
        self.wizard(1, 'Step 1: your hub login', back=self.w_welcome, on_next=self._login_next)
        self.label('1. Click the button below; the hub opens in your browser.\n2. Click "Request new API token", copy it, paste it here.')
        ttk.Button(self.body, text='Get your hub token', command=lambda: webbrowser.open(gp.TOKEN_PAGE)).pack(anchor='w', pady=(0, 14))
        ttk.Label(self.body, text='Hub username').pack(anchor='w')
        self.user_in = ttk.Entry(self.body, width=40)
        self.user_in.pack(anchor='w', pady=(0, 8))
        self.user_in.insert(0, self.e.user or '')
        ttk.Label(self.body, text='Hub token').pack(anchor='w')
        self.tok_in = ttk.Entry(self.body, width=40, show='•')
        self.tok_in.pack(anchor='w')
        self.label('Saved only on this computer, readable only by you.', 'Small.TLabel', pady=(6, 0))

    def _login_next(self):
        u, t = self.user_in.get(), self.tok_in.get()
        if not u.strip() or not t.strip():
            self.err.configure(text='Please fill in both.')
            return
        self.next_btn.configure(state='disabled', text='Checking...')
        self.bg(lambda: self.e.login(u, t), lambda _: self.w_code(), self._wizard_err)

    def _wizard_err(self, msg):
        self.err.configure(text=msg)
        if self.next_btn:
            self.next_btn.configure(state='normal', text='Next')

    def w_code(self, why=None):
        self.wizard(2, 'Step 2: the code from Pranav', back=self.w_login, on_next=self._code_next)
        if why:
            self.label(why, 'Banner.TLabel')
        self.label('Pranav sends you a registration code. Paste it here. It lets your GPU join the team pool, nothing else.')
        self.label('It expires 1 hour after Pranav makes it. If it has expired, just ask him for a new one.', 'Small.TLabel')
        self.code_in = ttk.Entry(self.body, width=44)
        self.code_in.pack(anchor='w')

    def _code_next(self):
        if not self.code_in.get().strip():
            self.err.configure(text='Please paste the code.')
            return
        self.code = self.code_in.get().strip()
        self.w_terminal()

    def w_terminal(self):
        self.wizard(3, 'Step 3: one line on your hub', back=lambda: self.w_code())
        self.step_msg = self.label('Starting your GPU server (about 5-15 minutes; the first start is the slowest)...')
        self.paste_box = ttk.Frame(self.body)
        self.paste_box.pack(fill='x')
        self.bg(self._start_for_setup, self._after_trigger, self._terminal_err)

    def _terminal_err(self, msg):
        self._wizard_err(msg)
        if self.screen == 'wizard3':
            self.step_msg.configure(text='Your GPU server did not start this time.')
            ttk.Button(self.paste_box, text='Try again', style='Accent.TButton', command=self.w_terminal).pack(anchor='w', pady=6)

    def _step_say(self, m):  # engine progress lines from the worker thread -> Step 3 text
        if m:
            self.q.put((lambda t: self.screen == 'wizard3' and self.step_msg.configure(text=t), m))

    # ---- automatic updates: config checked at start and hourly; installed only when safe, once a day per version ----
    def check_updates(self):
        self.bg(self.e.load_config, lambda _: self._update_decide(), lambda m: None)

    def _update_tick(self):
        self.root.after(UPDATE_EVERY_S * 1000, self._update_tick)
        self.check_updates()

    def safe_to_update(self):
        """Nothing the teammate is doing gets cut off: not mid-wizard, no Start in flight, no background start in the
        last 15 min, no install already running. A job on the GPU is never affected (it runs on the hub server)."""
        return (self.screen in ('main', 'wizard0') and not self.starting and not self.updating
                and gp.now() - self.auto_started_at > 900)

    def _update_decide(self):
        if self.screen == 'main':
            self._config_loaded()
        if should_auto_update(self.e, self.settings, gp.now(), self.safe_to_update()):
            self.install_update()
        elif self.e.update_needed() and (not self.e.auto_update() or self.settings.get('update_error')):
            self.offer_manual(self.settings.get('update_error'))

    def install_update(self):
        """Silent install of the pinned version, then the new copy starts (--updated) and this one quits."""
        v = self.e.config['launcher_version']
        self.settings['update_try'] = {'v': v, 't': gp.now()}  # one attempt per version per day, set BEFORE trying
        save_settings(self.settings)
        self.updating = True
        self.notice(f'Updating {gp.APP} to v{v}...')
        self.bg(lambda: self.e.update(hidden=not self.visible()), self._installed, self._install_failed)

    def _installed(self, how):
        self.settings.pop('update_error', None)
        save_settings(self.settings)
        if how == 'exit':
            self.quit()
        self.updating = False

    def _install_failed(self, msg):
        self.updating = False
        self.settings['update_error'] = msg
        save_settings(self.settings)
        self.offer_manual(msg)

    def offer_manual(self, err=None):
        """Fallback when automatic updates are off or failed: the Update button (main view or welcome screen)."""
        text = f'Automatic update failed: {err}' if err else f'A newer {gp.APP} (v{self.e.config["launcher_version"]}) is available.'
        if self.screen == 'main':
            self.upd_btn.configure(state='normal', text='Update')
            self.upd_btn.pack(side='right')
            if err:
                self.banner(text, retry=False)
        elif self.screen == 'wizard0' and not self.welcome_update.winfo_children():
            ttk.Label(self.welcome_update, text=text, style='Banner.TLabel', wraplength=380).pack(side='left', fill='x', expand=True)
            ttk.Button(self.welcome_update, text='Update', style='Main.TButton', command=self.install_update).pack(side='right', padx=(6, 0))
            self.welcome_update.pack_configure(pady=(0, 12))

    def notice(self, msg, secs=10):
        """A short non-blocking line (e.g. 'Hermes updated to v6'), gone after a few seconds."""
        if self.screen == 'main':
            self.banner(msg, retry=False)
            self.root.after(secs * 1000, self.banner_box.pack_forget)
        elif self.screen == 'wizard0':
            lbl = ttk.Label(self.welcome_update, text=msg, style='Small.TLabel')
            lbl.pack(anchor='w')
            self.root.after(secs * 1000, lambda: lbl.winfo_exists() and lbl.destroy())

    def _start_for_setup(self):
        up = self.e.server_up(self._step_say)
        if up != 'pooled':
            self.e.prepare('start', self.code)
        return self.e.trigger() if up != 'pooled' else 'started'

    def _after_trigger(self, how):
        if how != 'paste':
            self.w_selftest()
            return
        self.step_msg.configure(text='Your GPU server is on. Now, once only:\n1. Click "Open terminal": your hub opens in the browser.\n'
                                     '2. There click File > New > Terminal.\n3. Click "Copy", paste into the terminal (Ctrl+V / Cmd+V), press Enter.')
        ttk.Button(self.paste_box, text='Open terminal', command=lambda: webbrowser.open(f'{self.e.base}/lab')).pack(anchor='w', pady=6)
        ttk.Label(self.paste_box, text=gp.INSTALL_LINE, style='Code.TLabel').pack(anchor='w', fill='x', pady=6)
        b = ttk.Button(self.paste_box, text='Copy', style='Accent.TButton')
        b.configure(command=lambda: self.copy(gp.INSTALL_LINE, b))
        b.pack(anchor='w')
        self.wait_lbl = self.label('Waiting for you to paste it... (this page moves on by itself)', 'Small.TLabel', pady=(12, 0))
        self._wait_hook()

    def _wait_hook(self):
        if self.screen != 'wizard3':
            return
        self.bg(lambda: self.e.exists('gpupool/hooked'),
                lambda ok: self.w_selftest() if ok else self.root.after(5000, self._wait_hook), lambda _: self.root.after(5000, self._wait_hook))

    def w_selftest(self):
        self.wizard(4, 'Step 4: self-test', back=None)
        self.label('Your GPU is running a short test job of each kind. This takes a few minutes.')
        self.rows = {}
        for name, _, _ in CHECKS:
            r = ttk.Label(self.body, text=f'○  {name}')
            r.pack(anchor='w')
            self.rows[name] = r
        for n in CHECKS[:2]:
            self.rows[n[0]].configure(text=f'✔  {n[0]}', foreground='#1b7f3b')
        self.st_note = self.label('Pranav queues your test when he sends the code. Nothing after 10 minutes? Tell him.',
                                  'Small.TLabel', pady=(12, 0))
        self.t_selftest = time.time()
        self._poll_selftest()

    def _poll_selftest(self):
        if self.screen != 'wizard4':
            return
        self.bg(self.e.poll, self._show_selftest, lambda m: self.root.after(10000, self._poll_selftest))

    def _show_selftest(self, p):
        lines, failed = p['selftest'], []
        for name, key, why in CHECKS[2:]:
            hit = [l for l in lines if key in l and ('PASS' in l or 'FAIL' in l)]
            if hit:
                ok = 'PASS' in hit[-1]
                self.rows[name].configure(text=f'{"✔" if ok else "✘"}  {name}', foreground='#1b7f3b' if ok else '#b00020')
                if not ok:
                    failed.append((name, why))
        if p['status'].startswith('EXPIRED'):
            self.w_code(p['status'][9:])
            return
        done = [l for l in lines if ' DONE ' in f' {l}']
        if done and not failed and 'all checks passed' in done[-1]:
            self.w_done()
            return
        if failed or (done and 'FAIL' in done[-1]) or p['status'].startswith('ERROR'):
            self.st_note.configure(text='\n'.join(f'✘ {n}: {w}' for n, w in failed) or p['status'], foreground='#b00020')
            report = '\n'.join([f'{gp.APP} self-test for {self.e.user}', p['status']] + lines)
            b = ttk.Button(self.body, text='Copy error for Pranav')
            b.configure(command=lambda: self.copy(report, b))
            b.pack(anchor='w', pady=6)
            ttk.Button(self.body, text='Try again', command=self.w_terminal).pack(anchor='w')
            return
        self.root.after(10000, self._poll_selftest)

    def w_done(self):
        self.settings['setup_done'] = True
        save_settings(self.settings)
        self.wizard(5, "You're set!", next_text='Finish', on_next=self._finish_setup)
        self.label('Your GPU passed every check.')
        self.done_bg = tk.BooleanVar(value=self.settings.get('background', True))
        ttk.Checkbutton(self.body, text='Let Pranav start my GPU (background mode)', variable=self.done_bg).pack(anchor='w')
        self.label(BG_SENTENCE, 'Small.TLabel', pady=(4, 10))
        self.label('You can also press Start whenever you are free and close the window right after.')

    def _finish_setup(self):
        self.set_background(self.done_bg.get())
        self.main()

    def set_background(self, on):
        """Background mode on/off, explicitly chosen (the key in settings records that a choice was made)."""
        try:
            set_autostart(on)
        except OSError as x:
            self.banner(f'Could not change the start-at-login setting: {x}', retry=False)
            return False
        self.settings['background'] = on
        save_settings(self.settings)
        self.start_tray() if on else self.stop_tray()
        self.heartbeat(force=True)
        return True

    def migrate_background(self):
        """v7: background mode is on by default. Installs that never chose (no key) get it on once, with a notice;
        anyone who unticked it keeps it off."""
        if 'background' in self.settings or not self.settings.get('setup_done'):
            return
        if self.set_background(True):
            self.notice(f'Background mode is now on: {BG_SENTENCE}', secs=30)

    # ---- main view ----
    def main(self):
        self.clear()
        self.screen = 'main'
        self.big = ttk.Label(self.body, text='Checking...', style='Big.TLabel', wraplength=500)
        self.big.pack(anchor='w')
        self.sub = self.label('', 'Small.TLabel', pady=(4, 14))
        self.banner_box = ttk.Frame(self.body, style='Banner.TFrame', padding=8)
        self.banner_lbl = ttk.Label(self.banner_box, text='', style='Banner.TLabel', wraplength=370, padding=0)
        self.banner_lbl.pack(side='left', fill='x', expand=True)
        self.retry_btn = ttk.Button(self.banner_box, text='Retry', command=self.start)
        ttk.Button(self.banner_box, text='✕', width=3, command=lambda: self.banner_box.pack_forget()).pack(side='right')
        row = ttk.Frame(self.body)
        row.pack(fill='x', pady=(0, 16))
        self.start_btn = ttk.Button(row, text='Start', style='Accent.TButton', width=12, command=self.start)
        self.start_btn.pack(side='left')
        self.stop_btn = ttk.Button(row, text='Stop after current job', style='Main.TButton', width=20, command=self.stop)
        self.stop_btn.pack(side='left', padx=8)
        self.upd_btn = ttk.Button(row, text='Update', style='Main.TButton', width=12, command=self.update)
        ttk.Label(self.body, text='Last jobs', style='H.TLabel').pack(anchor='w')
        self.hist = ttk.Frame(self.body)
        self.hist.pack(fill='x', pady=(4, 12))
        self.notify_var = tk.BooleanVar(value=self.settings.get('notify', True))
        ttk.Checkbutton(self.body, text='Notify me if something fails', variable=self.notify_var,
                        command=lambda: (self.settings.update(notify=self.notify_var.get()), save_settings(self.settings))).pack(side='bottom', anchor='w')
        self.label(BG_SENTENCE, 'Small.TLabel', side='bottom', pady=(0, 8))
        self.bg_var = tk.BooleanVar(value=bool(self.settings.get('background')))
        ttk.Checkbutton(self.body, text='Let Pranav start my GPU (background mode)', variable=self.bg_var,
                        command=self.toggle_background).pack(side='bottom', anchor='w')
        self.check_updates()
        self.refresh()

    def _config_loaded(self):
        if self.e.too_old():
            self.banner('This version is too old to run. It updates itself; if that keeps failing, click Update.')
            self.upd_btn.pack(side='right')
            self.start_btn.configure(state='disabled')

    def refresh(self):
        if self.screen == 'main':
            if self.visible():  # hidden in the tray: the 5-min background tick keeps the state fresh instead
                self.bg(self.e.poll, self._show, lambda m: self.sub.configure(text=f'(cannot reach the hub: {m})'))
            self.poll_job = self.root.after(POLL_S * 1000, self.refresh)

    def _show(self, p):
        s, srv = p['status'], p['server']
        if s.startswith('EXPIRED'):
            self.w_code(s[9:])
            return
        if srv == 'off':
            big = 'Done, backups confirmed, GPU stopped. Thanks!' if 'Thanks' in self.last.get('status', '') and 'WARNING' not in self.last['status'] \
                else 'Your GPU is off'
            sub = 'Press Start whenever you are free.'
        elif srv == 'pending':
            big, sub = 'Your GPU is starting...', 'About 5-15 minutes. You can close this window.'
        elif s.startswith('Running job'):
            head, _, job = s.partition(': ')
            big, sub = f'{head.replace("up to ", "")} · GPU on', job
        elif s.startswith('Waiting for jobs'):  # "Waiting for jobs (stops in 12 min). Done 1 of up to 2. ..."
            head, _, rest = s.partition('. ')
            big, sub = head, rest
        elif srv == 'gpu' and s:
            big, sub = 'GPU on', s
        else:
            big, sub = 'Your hub server is on (not lending)', f'{gp.APP} leaves your own server alone.'
        self.big.configure(text=big)
        self.sub.configure(text=sub)
        self.start_btn.configure(state='normal' if srv == 'off' and not self.e.too_old() else 'disabled')
        self.stop_btn.configure(state='normal' if srv == 'gpu' and s.startswith(gp.BUSY) else 'disabled')
        for w in self.hist.winfo_children():
            w.destroy()
        for h in reversed(p['history'] or []):
            ok = len(h) > 2 and h[2] == 'success'
            ttk.Label(self.hist, text=f'{"✔" if ok else "✘"}  {h[1] if len(h) > 1 else "?"}   {local_time(h[0])}',
                      foreground='#1b7f3b' if ok else '#b00020').pack(anchor='w')
        if not p['history']:
            ttk.Label(self.hist, text='No jobs yet.', style='Small.TLabel').pack(anchor='w')
        new_fail = [h for h in p['history'] if len(h) > 2 and h[2] != 'success' and h not in self.last.get('history', [])]
        if self.last and (new_fail or (s.startswith(('ERROR', 'WARNING')) or 'WARNING' in s) and s != self.last.get('status')):
            self.banner(f'Job {new_fail[-1][1]} failed. Pranav sees it and reruns it.' if new_fail else s, notify=True, retry=not new_fail)
        self.last = p

    def banner(self, msg, notify=False, retry=True):
        """A red line under the status: launch problems get Retry; job failures only inform (Pranav reruns jobs)."""
        if self.screen != 'main':
            if getattr(self, 'err', None):
                self.err.configure(text=msg)
            return
        self.banner_lbl.configure(text=msg)
        self.banner_box.pack(after=self.sub, fill='x', pady=(0, 12))
        if retry:
            self.retry_btn.pack(side='right', padx=(6, 0))
        else:
            self.retry_btn.pack_forget()
        if notify and self.notify_var.get():
            self.root.bell()
            self.root.deiconify()
            self.root.lift()
            os_notify(gp.APP, msg)

    def start(self, auto=None):
        """Start button, or background mode with auto=(job set, wake time, key): those count as handled only once
        the launch worked, so a failed background start is tried again on the next checks (at most 3 times)."""
        self.banner_box.pack_forget()
        self.start_btn.configure(state='disabled')
        self.starting = True
        self.big.configure(text='Starting your GPU...')
        self.sub.configure(text='About 5-15 minutes. You can close this window once it says GPU on.')

        def go():
            up = self.e.server_up(lambda m: m and self.q.put((lambda t: self.screen == 'main' and self.sub.configure(text=t), m)))
            if up == 'pooled':
                return 'started'
            if not self.e.registered():
                return 'need_code'
            self.e.prepare('start')
            return self.e.trigger()

        def done(how):
            self.starting = False
            if auto:
                self.settings['last_auto_set'] = auto[0]
                self.settings['last_wake'] = max(self.settings.get('last_wake', 0), auto[1])
                self.settings.pop('auto_fail', None)
                save_settings(self.settings)
                self.note({'need_code': 'needs a new registration code (Step 2)', 'paste': 'needs the one-time terminal paste (Step 3)'}
                          .get(how, 'started your GPU'))
            self.w_code('Your GPU needs a new code from Pranav.') if how == 'need_code' \
                else self.w_terminal() if how == 'paste' else self.refresh()

        def failed(msg):
            self.starting = False
            if auto:
                f = self.settings.get('auto_fail', {})
                n = f.get('n', 0) + 1 if f.get('key') == auto[2] else 1
                self.settings['auto_fail'] = {'key': auto[2], 'n': n}
                save_settings(self.settings)
                self.note(f'start failed ({n}/3): {msg}')
            self.banner(msg)
        self.bg(go, done, failed)

    def stop(self):
        self.stop_btn.configure(state='disabled')
        self.bg(self.e.request_stop, lambda _: self.sub.configure(text='OK: your GPU stops after the current job and confirms the backups.'))

    def update(self):  # the manual fallback button
        self.upd_btn.configure(state='disabled', text='Updating...')
        self.install_update()


def run(engine=None):
    root = tk.Tk()
    e = engine or gp.Engine()
    app = App(root, e)
    if e.saved_login() and load_settings().get('setup_done'):
        app.main()  # also checks for updates
        app.migrate_background()
        app.bg_var.set(bool(app.settings.get('background')))
        if '--background' in sys.argv and app.settings.get('background'):  # started at login: straight to the tray/Dock
            app.on_close()
    else:
        app.w_welcome()
        app.check_updates()
    if '--updated' in sys.argv:
        app.settings.pop('update_error', None)
        save_settings(app.settings)
        app.notice(f'{gp.APP} updated to v{gp.VERSION}')
    root.mainloop()


if __name__ == '__main__':
    run()
