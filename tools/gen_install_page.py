"""Build docs/install.html: step-by-step install for every platform."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from sitekit import page  # noqa: E402

REPO = "https://github.com/JosephsStuff/Flip-For-Free"


def shot(name, alt, caption):
    return (f'<figure><img src="shots/{name}.webp" alt="{alt}" loading="lazy" width="1600" height="1000">'
            f"<figcaption>{caption}</figcaption></figure>")


body = f"""
<h1>Install Flip for Free</h1>
<p class="lede">About 15 minutes, plus a one-off wait for eBay to approve your developer account. Everything is free,
and your data stays on your own computer or server.</p>

<div class="os">
<a href="#windows"><b>Windows</b><span>Double-click to run</span></a>
<a href="#mac"><b>Mac</b><span>Double-click to run</span></a>
<a href="#linux"><b>Linux</b><span>One terminal command</span></a>
<a href="#docker"><b>Docker</b><span>Desktop, NAS or server</span></a>
<a href="oracle.html"><b>Free server, 24/7</b><span>Oracle Cloud + Tailscale</span></a>
<a href="#demo"><b>Just looking?</b><span>Try it with fake data</span></a>
</div>

<h2 id="before">Before you start: an eBay developer account</h2>
<p>Flip for Free signs in to eBay with your own free developer keys, so nobody else ever has access to your account.
eBay checks new developer accounts by hand, so do this first:</p>
<ol class="steps">
<li>Register at <a href="https://developer.ebay.com">developer.ebay.com</a> (click <b>Register</b>) with the same
details as your eBay seller account.</li>
<li>Wait for the approval email. It usually takes about one working day, and you can't create keys until it arrives.
If it's rejected, use eBay's developer support form to ask for a manual review.</li>
</ol>
<p>While you wait, install the app below and explore it in <a href="#demo">demo mode</a>.</p>

<h2 id="windows">Windows</h2>
<ol class="steps">
<li>Install <a href="https://www.python.org/downloads/">Python 3.10 or newer</a>. On the first installer screen,
tick <b>Add python.exe to PATH</b>.</li>
<li>Download Flip for Free: on <a href="{REPO}">GitHub</a> click <b>Code → Download ZIP</b>, then right-click the
zip and choose <b>Extract All</b>. Put the folder somewhere permanent, such as <code>Documents\\Flip-For-Free</code>.</li>
<li>Double-click <code>run.bat</code>. The first run installs what it needs (a minute or two), then opens your browser.</li>
<li>If Windows Firewall asks, allow access on <b>Private networks only</b>. That's what lets your phone reach it on
your home Wi-Fi.</li>
</ol>

<h2 id="mac">Mac</h2>
<ol class="steps">
<li>Install <a href="https://www.python.org/downloads/macos/">Python 3.10 or newer</a> from python.org.</li>
<li>Download the ZIP from <a href="{REPO}">GitHub</a> (<b>Code → Download ZIP</b>) and double-click it to unzip.
Move the folder to <code>Documents</code>.</li>
<li>Double-click <code>run.command</code>. If macOS says it's from an unidentified developer, right-click it, choose
<b>Open</b>, then <b>Open</b> again. You only need to do this once.</li>
</ol>

<h2 id="linux">Linux</h2>
<pre><code>git clone {REPO}.git
cd Flip-For-Free
./run.sh</code></pre>
<p>Needs Python 3.10+ with <code>venv</code> (on Ubuntu: <code>sudo apt install python3-venv</code>).</p>

<h2 id="ready">When it's running</h2>
<p>The window prints two addresses and opens the first one:</p>
<pre><code>On this computer:   https://localhost:8765/
On other devices:   https://192.168.1.23:8765/   (same Wi-Fi only; pair each device once)</code></pre>
<ul>
<li>The browser warns about the certificate the first time. That's expected: Flip makes its own certificate because
eBay requires an https sign-in address. Choose <b>Advanced → Proceed to localhost</b>.</li>
<li>Keep the window open while you use Flip for Free. Closing it stops the app; your data is safe in the database.</li>
</ul>

<h2 id="connect">Connect your eBay account</h2>
{shot("setup", "The setup screen: choose a database folder, then add eBay developer keys", "The first-run setup screen.")}
<ol class="steps">
<li><b>Choose where your data lives.</b> Pick any folder. Use a Google Drive, OneDrive or Dropbox folder if you want
the same data on more than one computer. Flip creates <code>ebay_profit_tracker.db</code> there.</li>
<li><b>Create a Production keyset</b> at <a href="https://developer.ebay.com/my/keys">developer.ebay.com/my/keys</a>.
If it says the keyset is disabled, open the <i>Marketplace account deletion</i> notice and choose the exemption.</li>
<li><b>Copy the three keys</b> from the <b>Production</b> box into the fields with the same names: App ID (Client ID),
Dev ID and Cert ID (Client Secret).</li>
<li><b>Add a redirect.</b> Next to the keyset: <i>User Tokens → Get a Token from eBay via Your Application → Add eBay
Redirect URL</i>. Set both <i>Your auth accepted URL</i> and <i>Your auth declined URL</i> to
<code>https://localhost:8765/auth/callback</code> (or your server's address + <code>/auth/callback</code>), tick
<b>OAuth Enabled</b> and save. Copy the <b>RuName</b>: the short name in the first column, not a web address.</li>
<li><b>Sign in with eBay</b>, agree, and you're back in Flip. The first sync starts straight away and looks back up to
two years.</li>
</ol>
<div class="note">If eBay won't accept a <code>localhost</code> address, use the small relay page in the
<code>relay</code> folder (see the <a href="{REPO}#if-ebay-wont-accept-localhost">README</a>), or run Flip on a
<a href="oracle.html">free server</a>, which has a real https address.</div>
{shot("dashboard", "The Flip for Free dashboard showing true profit, what eBay took and a 12-month chart", "Once synced: the dashboard.")}

<h2 id="docker">Docker</h2>
<p>For Docker Desktop on Windows or Mac, or Docker on a NAS or Linux server:</p>
<pre><code>git clone {REPO}.git
cd Flip-For-Free
docker compose up -d --build</code></pre>
<p>Open <code>https://localhost:8765</code>. Forgotten the address? This works in PowerShell, Command Prompt, Mac
Terminal and Linux:</p>
<pre><code>docker compose exec flip flip-address</code></pre>
<ul>
<li>Your data lives in <code>flip-data</code> next to <code>docker-compose.yml</code>. Back that folder up; rebuilding
the container never touches it.</li>
<li>Only this computer can reach it. For other devices, put Tailscale on the Docker host, run
<code>tailscale serve --bg 8765</code>, and set <code>FLIP_PUBLIC_URL</code> in <code>docker-compose.yml</code> to the
<code>https://….ts.net</code> address.</li>
</ul>

<h2 id="server">A free server that's always on</h2>
<p>Oracle Cloud's Always Free tier plus Tailscale runs Flip for Free 24/7, private to your own devices, at a real https
address. One command installs everything, including automatic restarts and nightly backups:</p>
<pre><code>curl -fsSL https://raw.githubusercontent.com/JosephsStuff/Flip-For-Free/main/deploy/install.sh | bash</code></pre>
<p>The <a href="oracle.html">step-by-step Oracle guide</a> covers creating the account, the network (create it with
Oracle's <i>VCN Wizard → Create VCN with Internet Connectivity</i> first, so the server gets a public address), the
server itself and Tailscale.</p>

<h2 id="phone">Use it on your phone</h2>
<p>On the same Wi-Fi: open the <i>other devices</i> address on your phone, then pair it once from
<b>Settings → Use on your phone</b>. Away from home: use the free server, or put Tailscale on your computer.</p>
{shot("phone-dashboard", "The dashboard on a phone", "The same app on a phone: one card per row.").replace('width="1600" height="1000"', 'width="780" height="1688" style="max-width:360px"')}

<h2 id="shops">Connect CardTrader and Shopify (optional)</h2>
<p><b>Settings → Other shops</b>. CardTrader needs the API token from your CardTrader profile settings. Shopify needs
an app from its Dev Dashboard with the listed access scopes, installed on your store. Then every item gets
<i>List on CardTrader</i> and <i>List on Shopify</i>, and sales on any shop take the stock off the others.</p>
{shot("shops", "Settings, Other shops: connect CardTrader and Shopify", "Connecting other shops.")}

<h2 id="demo">Try it without an eBay account</h2>
<pre><code>python app.py --demo</code></pre>
<p>Fills the app with a year of realistic fake sales, stock and bulk buys so you can look around. Nothing is saved
and nothing touches eBay.</p>

<h2 id="updating">Updating</h2>
<p><b>Settings → Updates → Update now</b> downloads the latest version, backs up your database first and restarts.
Or tick <i>Install new versions automatically</i>. Your data is never in the app folder, so updating can't lose it.</p>

<h2 id="help">Something not working?</h2>
<table>
<tr><th>Problem</th><th>Fix</th></tr>
<tr><td>"python is not recognised" on Windows</td><td>Reinstall Python and tick <b>Add python.exe to PATH</b>, then run <code>run.bat</code> again.</td></tr>
<tr><td>Browser says the connection isn't private</td><td>Expected on <code>localhost</code>: choose Advanced → Proceed. The certificate is made on your computer.</td></tr>
<tr><td>eBay sign-in says the redirect is invalid</td><td>The accepted and declined URLs must exactly match the address you opened Flip on, ending in <code>/auth/callback</code>, with OAuth Enabled ticked.</td></tr>
<tr><td>My phone can't open the other-devices address</td><td>It only works on the same Wi-Fi, and the Windows firewall must allow Python on private networks.</td></tr>
<tr><td>Anything else</td><td>The error page has <b>Report this on GitHub</b>, which opens a pre-filled issue with personal details removed.</td></tr>
</table>
"""

toc = [("before", "eBay developer account"), ("windows", "Windows"), ("mac", "Mac"), ("linux", "Linux"),
       ("ready", "When it's running"), ("connect", "Connect eBay"), ("docker", "Docker"),
       ("server", "Free 24/7 server"), ("phone", "On your phone"), ("shops", "CardTrader and Shopify"),
       ("demo", "Try the demo"), ("updating", "Updating"), ("help", "Troubleshooting")]
(ROOT / "docs" / "install.html").write_text(page(
    file="install.html", title="Install Flip for Free on Windows, Mac, Linux, Docker or a free server",
    description="Step-by-step install of Flip for Free, the free eBay profit tracker: eBay developer keys, Windows, "
                "Mac, Linux, Docker, a free Oracle Cloud server, your phone, CardTrader and Shopify.",
    body=body, toc=toc))
print("wrote docs/install.html")
