# Lend your GPU to the team

**The deal:** whenever you have free time, press **Start** in the Run Jobs app. Your StratusGeo GPU switches on, works through a couple of the team's queued experiments, and switches itself off. You can close the app or shut your laptop right after pressing Start. Every log and result is saved to the team's S3 bucket and GitHub. There is no pressure: if you never press it again, nothing breaks and the jobs just wait.

## Install (one file, nothing else)

1. Download from https://github.com/Luxaris/gpupool-launcher/releases/latest:
   - **Windows:** `RunJobs-Setup.exe`. If Windows says *"Windows protected your PC"*, click **More info**, then **Run anyway**. It installs for your user only, so no admin is needed. You get a **Run Jobs** shortcut in the Start menu and on the desktop, and it can be uninstalled from Settings > Apps.
   - **Mac:** `RunJobs-mac-arm64.dmg` for Apple-chip Macs (M1 and later) or `RunJobs-mac-x64.dmg` for Intel Macs. Open it and drag **Run Jobs** to Applications. The first time, **right-click Run Jobs > Open > Open**.
2. Open **Run Jobs**. A short setup wizard walks you through about 15 minutes, once:
   - **Step 1, hub login:** a button opens the hub's token page. Copy the token and paste it with your username. The app checks it straight away.
   - **Step 2, the code from Pranav:** paste it. It expires 1 hour after he makes it; if it has expired, ask him for a new one.
   - **Step 3, one line on your hub:** the app starts your GPU server, then gives you an **Open terminal** button and a big **Copy** button. Paste the line into the terminal and press Enter. The app notices by itself when it's done.
   - **Step 4, self-test:** a checklist fills in with ✔ or ✘. If anything fails, it says what's wrong in plain words, and **Copy error for Pranav** puts everything he needs in your clipboard.
   - **Done:** you're set.

Your hub token stays on your computer, in a file only you can read. Everything runs only on **your own** hub server, in `~/gpupool` and `~/actions-runner`. Nothing else is installed by hand: the app sets up the job runner on your server for you.

## Day to day

- **Start** whenever you're free. Once it says *GPU on*, close the app.
- **Stop after current job** if you need your GPU back. It also confirms the backups before switching off.
- **Update** appears when there is a new version. One click installs it (the file is checked first).
- **Last jobs** shows ✔ or ✘ for recent jobs. You don't need to do anything about a ✘: Pranav sees it and reruns the job.
- **Notify me if something fails** is on by default: a red banner (on a Mac, also a notification).
- If your GPU slot sits unused for 14 days, GitHub removes it. The app tells you, goes back to Step 2 and asks for a new code. Nothing is reinstalled.
