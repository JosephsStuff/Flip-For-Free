# Run Flip for Free on Oracle Cloud's free tier

You get Flip for Free running 24/7 on a free Oracle server, reachable only from your own devices
through Tailscale, at a proper https address. That address also fixes eBay sign-in, so there's
no relay page and no certificate warning. Time needed: about 45 minutes. Cost: £0.

What you need: a debit/credit card (Oracle uses it to verify you; Always Free resources stay free),
and about 45 minutes.

---

## 1. Create your Oracle account
1. Go to **cloud.oracle.com/free** and sign up.
2. **Home region: choose UK South (London).** Free servers can only be created in your home region,
   and it can't be changed later.

## 2. Create the server
1. In the Oracle console: **☰ → Compute → Instances → Create instance**.
2. Name it `flip`.
3. **Image:** click *Change image* → **Canonical Ubuntu 24.04**.
4. **Shape:** click *Change shape* and pick one marked **Always Free-eligible**:
   - **Ampere VM.Standard.A1.Flex** with 1 OCPU and 6 GB memory (best), or
   - **VM.Standard.E2.1.Micro** (AMD, 1 GB) if Ampere says "out of capacity". Flip runs fine on it.
5. **Networking:** leave the defaults (a new network with a public IP).
6. **SSH keys:** choose **Generate a key pair for me** and **download the private key**. Keep it safe:
   it's the only way into the server until Tailscale is set up.
7. Click **Create**. When it shows *Running*, copy the **Public IP address**.

## 3. Connect to it (Windows)
Put the downloaded key somewhere simple, e.g. `C:\Users\you\.ssh\flip.key`, then in **PowerShell**:
```powershell
# Windows refuses keys other people can read, so lock it down once:
icacls $env:USERPROFILE\.ssh\flip.key /inheritance:r /grant:r "$($env:USERNAME):R"
ssh -i $env:USERPROFILE\.ssh\flip.key ubuntu@YOUR.PUBLIC.IP
```
Type `yes` the first time. You're on the server when the prompt says `ubuntu@flip`.

## 4. Install Flip for Free
**Bringing your existing data?** First download it from Flip for Free on your PC (**Settings → Your data → Download
database**), then from a **second** PowerShell window on your PC upload it (and your receipts folder, if you use receipts):
```powershell
ssh -i $env:USERPROFILE\.ssh\flip.key ubuntu@YOUR.PUBLIC.IP "mkdir -p ~/flip-data"
scp -i $env:USERPROFILE\.ssh\flip.key "$env:USERPROFILE\Downloads\flip-for-free-DATE.db" ubuntu@YOUR.PUBLIC.IP:~/flip-data/ebay_profit_tracker.db
scp -i $env:USERPROFILE\.ssh\flip.key -r "G:\My Drive\YOUR FOLDER\Profit Tracker receipts" ubuntu@YOUR.PUBLIC.IP:~/flip-data/
```
> ⚠ **Stop using the copy on your PC once the server is running.** Both would sync with eBay and run
> automations from their own database, so listings could be relisted or promoted twice. Close the app on
> your PC and don't start it again (keep the downloaded file as an extra backup).

Then, on the server, install straight from GitHub:
```bash
curl -fsSL https://raw.githubusercontent.com/JosephsStuff/flip-for-free/main/deploy/install.sh | bash
```
Partway through, it prints a **Tailscale sign-in link**. Open it, sign in (create a free Tailscale
account if you don't have one, e.g. with Google) and approve the server.

## 5. Turn on HTTPS in Tailscale (one-off)
In the Tailscale admin console (**login.tailscale.com/admin/dns**): make sure **MagicDNS** is on and
click **Enable HTTPS**. If the install script said it couldn't switch on Tailscale Serve, run this on
the server afterwards:
```bash
sudo tailscale serve --bg 8765
```
(Your server's name will appear in public certificate logs as `flip.<your-tailnet>.ts.net`, which is why
it's called "flip" rather than anything personal.)

## 6. Put Tailscale on your PC and phone
Install Tailscale from **tailscale.com/download** on your PC and phone and sign in with the same account.
Then open the address the installer printed, e.g. **https://flip.tail1234.ts.net**.

## 7. Point eBay sign-in at the server
On **developer.ebay.com → Application Keys → User Tokens**, edit your RuName and set **both**
*Your auth accepted URL* and *Your auth declined URL* to:
```
https://flip.<your-tailnet>.ts.net/auth/callback
```
(The installer prints the exact address.) eBay only redirects *your browser* there, so it works
even though the server isn't public. Then in Flip for Free click **Sign in with eBay**.

If you uploaded your database, your keys, costs and bulk buys are already there. If not, the setup page
asks for your eBay keys as before. The database is already set to `~/flip-data`.

## 8. Connect Claude to the server
In your Claude desktop config, change `FLIP_URL` to the server's address:
```json
"env": { "FLIP_URL": "https://flip.<your-tailnet>.ts.net", "FLIP_API_KEY": "flip_…" }
```
Your PC needs Tailscale running for this.

---

## Day to day
| Task | Command (on the server) |
|---|---|
| Is it running? | `sudo systemctl status flip` |
| Live log | `journalctl -u flip -f` |
| Restart | `sudo systemctl restart flip` |
| Back up now | `bash ~/flip-for-free/deploy/backup.sh` |
| Update to a new version | **Settings → Updates → Update now** in Flip for Free (or run the install command again) |

Updating never touches `~/flip-data`.

## Backups
Every night at 03:30 a copy of your database is saved in `~/flip-data/backups` and kept for 30 days.
To also send each backup to **Google Drive** (recommended, so a copy lives off the server):
1. On your **PC**, install rclone (rclone.org/downloads) and run `rclone authorize "drive"`. Sign in
   to Google and copy the token it prints.
2. On the **server**: `sudo apt-get install -y rclone && rclone config`. Choose **n** (new remote),
   name it **gdrive**, storage **drive**, accept the defaults, answer **n** to "Use auto config?",
   and paste the token from step 1.
3. Test with `bash ~/flip-for-free/deploy/backup.sh`. It should say *Copied to Google Drive*.

To restore a backup: `sudo systemctl stop flip`, then
`gunzip -c ~/flip-data/backups/flip-DATE.db.gz > ~/flip-data/ebay_profit_tracker.db`, then
`sudo systemctl start flip`.

## Keeping it free and safe
- **Idle servers:** Oracle's docs say it may reclaim an Always Free server that's been mostly idle for
  7 days. Flip is very light, so keep the Google Drive backup on. If the server ever disappears,
  create a new one and restore. Oracle also suspends *accounts* left unused for 30 days, so log in to
  the Oracle console now and then.
- **Close the public door (optional):** once Tailscale works, run `sudo tailscale set --ssh` and connect
  with `ssh ubuntu@flip` from your PC (with Tailscale on). Then in Oracle's console
  (**Networking → Virtual cloud networks → your VCN → Security lists**) remove the rule allowing port 22
  from 0.0.0.0/0. The server then has no open ports to the internet at all.
- Flip for Free only listens on the server itself; Tailscale is the only way in, and only your own
  signed-in devices can use it.
