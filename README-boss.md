# Tigress Bond Pricing — Quick Start

A short guide for using the Tigress Bond Pricing Engine on your desk.

---

## First-time setup (one-time, ~5 minutes)

Before you start, make sure **Bloomberg Terminal** is installed on this
machine and you can log in to it.

1. Open the folder you were sent (e.g. `BONDPRICING\`).
2. **Double-click `setup-boss-machine.bat`**.
3. A window pops up and runs through the install (Python, supporting
   software, your desktop shortcut). Takes about 3 minutes. Just wait.
4. When it says **Setup Complete**, you're done. Close the window.

You now have a **"Tigress Bond Pricing"** icon on your desktop.

---

## Every day

1. **Open Bloomberg Terminal** and log in like you normally would.
2. **Double-click "Tigress Bond Pricing"** on your desktop.
3. Your default browser opens to a login screen.
4. Sign in with your **Tigress credentials** (provided separately).
5. Type a ticker → click **Load** → walk through the screens.

That's it. Live pricing data, pulled from your own Bloomberg Terminal,
appears within a few seconds. Every search you do is automatically
saved so your clients can see it on the public site.

---

## Looking at data from anywhere else

When you're not at your desk — at home, on your phone, traveling — you
can still **view** what's been pulled (you just can't pull anything new
because Bloomberg isn't available off the desk).

- Visit **https://tigress-bondpricing.vercel.app**
- Sign in with the same credentials
- Search any ticker that's been pulled at the desk — you'll see the
  same dashboard, just as a snapshot from the last desk-pull.

Your clients visit this same URL with their own client login. They see
exactly what your desk has cached — never anything more.

---

## If something doesn't work

| Symptom | Try this |
|---|---|
| Browser opens but says "site can't be reached" | Wait 5 seconds and refresh. Server takes a moment to start. |
| Login shows "Connection error" | Make sure Bloomberg Terminal is open and logged in. |
| Search hangs forever | Bloomberg Terminal logged out — log back in and try again. |
| Icon doesn't do anything when clicked | Open the install folder, double-click `TigressBondPricing.bat` directly — it'll show the error in a console window you can screenshot. |
| Anything else | Email a screenshot to your Tigress technical contact. |

---

## What NOT to do

- **Don't** move or rename the install folder after running setup —
  the desktop shortcut will break. (If you must move it, just re-run
  `setup-boss-machine.bat` from the new location.)
- **Don't** share the `.env` file in this folder — it contains secrets
  that authenticate your machine to the Tigress backend.
- **Don't** close Bloomberg Terminal while using the bond pricing tool.

---

## Questions

Email your Tigress technical contact. Include a screenshot if you can.
