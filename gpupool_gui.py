#!/usr/bin/env python3
"""Hermes: a small Tkinter window around gpupool.Engine (all real work is in gpupool.py).

First run: a setup wizard (welcome, hub login, registration code, one-time terminal paste, self-test, done).
After that: one big status line, Start, Stop after current job, Update (when a newer version exists), the last jobs
with a check or a cross, and a "notify me if something fails" switch (an in-window banner; also a macOS notification).
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


class App:
    def __init__(self, root, engine):
        self.root, self.e, self.q = root, engine, queue.Queue()
        self.settings = load_settings()
        self.code, self.screen, self.last, self.poll_job = None, None, {}, None
        root.title(gp.APP)
        root.geometry('560x600')
        root.minsize(520, 560)
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
        while not self.q.empty():
            cb, arg = self.q.get()
            if cb:
                cb(arg)
        self.root.after(100, self._pump)

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
        self.welcome_update = ttk.Frame(self.body)  # filled by _startup_config when a newer version exists
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

    def _startup_config(self, _):
        """Runs once when the app opens: offer the update on the first wizard screen too (the main view checks itself)."""
        if self.screen != 'wizard0' or not (self.e.update_needed() or self.e.too_old()):
            return
        ttk.Label(self.welcome_update, text=f'A newer {gp.APP} (v{self.e.config["launcher_version"]}) is available.',
                  style='Banner.TLabel').pack(side='left', fill='x', expand=True)
        b = ttk.Button(self.welcome_update, text='Update now', style='Main.TButton')
        b.configure(command=lambda: (b.configure(state='disabled', text='Updating...'),
                                     self.bg(self.e.update, lambda how: self.root.destroy() if how == 'installer'
                                             else self.err.configure(text=f'Updated. Please close and open {gp.APP} again.'))))
        b.pack(side='right', padx=(6, 0))
        self.welcome_update.pack_configure(pady=(0, 12))

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
        self.wizard(5, "You're set!", next_text='Finish', on_next=self.main)
        self.label('Your GPU passed every check. Press Start whenever you are free; you can close the window right after.')

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
        self.bg(self.e.load_config, lambda _: self._config_loaded(), lambda m: self.sub.configure(text=f'(cannot check for updates: {m})'))
        self.refresh()

    def _config_loaded(self):
        if self.e.update_needed():
            self.upd_btn.pack(side='right')
        if self.e.too_old():
            self.banner('This version is too old to run. Click Update.')
            self.start_btn.configure(state='disabled')

    def refresh(self):
        if self.screen == 'main':
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

    def start(self):
        self.banner_box.pack_forget()
        self.start_btn.configure(state='disabled')
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
        self.bg(go, lambda how: self.w_code('Your GPU needs a new code from Pranav.') if how == 'need_code'
                else self.w_terminal() if how == 'paste' else self.refresh(), self.banner)

    def stop(self):
        self.stop_btn.configure(state='disabled')
        self.bg(self.e.request_stop, lambda _: self.sub.configure(text='OK: your GPU stops after the current job and confirms the backups.'))

    def update(self):
        self.upd_btn.configure(state='disabled', text='Updating...')
        self.bg(self.e.update, lambda how: self.root.destroy() if how == 'installer'
                else self.banner(f'Updated. Please close and open {gp.APP} again.'), self.banner)


def run(engine=None):
    root = tk.Tk()
    e = engine or gp.Engine()
    app = App(root, e)
    app.bg(e.load_config, app._startup_config, lambda m: None)  # update check before the wizard, too
    if e.saved_login() and load_settings().get('setup_done'):
        app.main()
    else:
        app.w_welcome()
    root.mainloop()


if __name__ == '__main__':
    run()
