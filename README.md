# Lend your GPU to the team

**The deal:** whenever you have free time, press **Start** in the **Hermes** app. Your StratusGeo GPU switches on, works through a couple of the team's queued experiments, and switches itself off. You can close the app or shut your laptop right after pressing Start. Every log and result is saved to the team's S3 bucket and GitHub. There is no pressure: if you never press it again, nothing breaks and the jobs just wait.

## Install (one file, nothing else)

1. Download from https://github.com/Luxaris/gpupool-launcher/releases/latest:
   - **Windows:** `Hermes-Setup.exe`. If Windows says *"Windows protected your PC"*, click **More info**, then **Run anyway**. It installs for your user only, so no admin is needed. You get a **Hermes** shortcut in the Start menu and on the desktop, and it can be uninstalled from Settings > Apps.
   - **Mac:** `Hermes-mac-arm64.dmg` for Apple-chip Macs (M1 and later) or `Hermes-mac-x64.dmg` for Intel Macs. Open it and drag **Hermes** to Applications. The first time, **right-click Hermes > Open > Open**.
   - The `RunJobs-*` files on the release are the same installers, used to update older copies automatically. Ignore them.
2. Open **Hermes**. A short setup wizard walks you through about 15 minutes, once:
   - **Step 1, hub login:** a button opens the hub's token page. Copy the token and paste it with your username. The app checks it straight away.
   - **Step 2, the code from Pranav:** paste it. It expires 1 hour after he makes it; if it has expired, ask him for a new one.
   - **Step 3, one line on your hub:** the app starts your GPU server, then gives you an **Open terminal** button and a big **Copy** button. Paste the line into the terminal and press Enter. The app notices by itself when it's done.
   - **Step 4, self-test:** a checklist fills in with ✔ or ✘. If anything fails, it says what's wrong in plain words, and **Copy error for Pranav** puts everything he needs in your clipboard.
   - **Done:** you're set.

Your hub token stays on your computer, in a file only you can read. Everything runs only on **your own** hub server, in `~/gpupool` and `~/actions-runner`. Nothing else is installed by hand: the app sets up the job runner on your server for you.

## Day to day

- **Start** whenever you're free. Once it says *GPU on*, close the app.
- **Stop after current job** if you need your GPU back. It also confirms the backups before switching off.
- **Updates install themselves** (from v6 on), at a quiet moment: never mid-setup and never while a start is under way. A job already running on your GPU isn't affected. You'll see "Hermes updated to vN". If an update fails, an **Update** button appears with the error.
- **Last jobs** shows ✔ or ✘ for recent jobs. You don't need to do anything about a ✘: Pranav sees it and reruns the job.
- **Notify me if something fails** is on by default: a red banner (on a Mac, also a notification).
- **Let Pranav start my GPU (background mode)** is on by default after setup (from v7), so you don't need to press Start. While this laptop is on, Hermes opens at login and sits in the tray (on a Mac, in the Dock). It starts your GPU by itself when team jobs are waiting or Pranav asks. It never touches a server you started yourself. The tray menu has **Pause for 1 hour**, **Pause until tomorrow**, **Resume** and **Quit**. Untick it any time; that also removes the start-at-login entry, and Hermes remembers your choice.
- When the queue is empty, your GPU waits about 15 minutes for new jobs before it backs up and switches off.
- If your GPU slot sits unused for 14 days, GitHub removes it. The app tells you, goes back to Step 2 and asks for a new code. Nothing is reinstalled.
