"""Build docs/api.html from docs/api-examples.json (run tools/gen_api_docs.py first)."""
import json
import re
import sys
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))
from sitekit import page  # noqa: E402

import api  # noqa: E402

EX = json.loads((ROOT / "docs" / "api-examples.json").read_text())
BASE = "https://localhost:8765"

GROUPS = [
    ("reports", "Profit and reports", ["/summary", "/time-to-sell", "/tax"]),
    ("sales", "Sales", ["/sales"]),
    ("items", "Inventory", ["/items"]),
    ("bulk", "Bulk buys", ["/bulk-buys"]),
    ("listings", "eBay listings", ["/listings"]),
    ("money", "Expenses and mileage", ["/expenses", "/mileage"]),
    ("alerts", "Notifications and sync", ["/notifications", "/sync"]),
]


def slug(m, p):
    return (m + "-" + re.sub(r"[^a-z0-9]+", "-", p.lower())).strip("-")


def example_for(m, p):
    pat = "^/api/v1" + re.sub(r"<[^>]+>", "[^/?]+", p) + r"(\?.*)?$"
    for e in EX:
        if e["method"] == m and re.match(pat, e["path"]):
            return e
    return None


def snippets(e):
    url = BASE + e["path"]
    body = json.dumps(e["body"], indent=2) if e["body"] is not None else None
    curl = f'curl -k -X {e["method"]} "{url}" \\\n  -H "Authorization: Bearer $FLIP_KEY"'
    if body:
        curl += ' \\\n  -H "Content-Type: application/json" \\\n  -d \'' + json.dumps(e["body"]) + "'"
    py = ("import os, requests\n\n"
          f'r = requests.{e["method"].lower()}(\n    "{url}",\n'
          '    headers={"Authorization": "Bearer " + os.environ["FLIP_KEY"]},\n'
          + (f"    json={json.dumps(e['body'])},\n" if body else "")
          + "    verify=False,  # your own copy uses a self-signed certificate\n)\nprint(r.json())")
    js = (f'const r = await fetch("{url}", {{\n  method: "{e["method"]}",\n'
          '  headers: { Authorization: `Bearer ${process.env.FLIP_KEY}`'
          + (', "Content-Type": "application/json" },\n  body: JSON.stringify(' + json.dumps(e["body"]) + "),\n" if body
             else " },\n")
          + "});\nconsole.log(await r.json());")
    return curl, py, js


def endpoint_html(m, p, does):
    e = example_for(m, p)
    sid = slug(m, p)
    out = [f'<section class="ep" id="{sid}"><div class="ep-head"><span class="m {m}">{m}</span>'
           f"<h3>/api/v1{escape(p)}</h3></div><p>{escape(does)}</p>"]
    if e:
        if e.get("note"):
            out.append(f"<p class=status>{escape(e['note'])}</p>")
        curl, py, js = snippets(e)
        out.append(f'<div class="tabs" role="tablist" aria-label="Example request">'
                   f'<button role="tab" aria-selected="true" data-for="{sid}-curl">curl</button>'
                   f'<button role="tab" aria-selected="false" data-for="{sid}-py">Python</button>'
                   f'<button role="tab" aria-selected="false" data-for="{sid}-js">JavaScript</button></div>'
                   f'<pre id="{sid}-curl"><code>{escape(curl)}</code></pre>'
                   f'<pre id="{sid}-py" hidden><code>{escape(py)}</code></pre>'
                   f'<pre id="{sid}-js" hidden><code>{escape(js)}</code></pre>'
                   f'<p class="status">Response · HTTP {e["status"]} (from the demo data)</p>'
                   f'<pre><code>{escape(json.dumps(e["response"], indent=2, ensure_ascii=False))}</code></pre>')
    out.append("</section>")
    return "".join(out)


def mcp_tools():
    import ast
    tree = ast.parse((ROOT / "mcp_server.py").read_text())
    rows = []
    for fn in tree.body:
        if isinstance(fn, ast.FunctionDef) and any("tool" in ast.unparse(d) for d in fn.decorator_list):
            doc = (ast.get_docstring(fn) or "").split("\n\n")[0].replace("\n", " ")
            rows.append(f"<tr><td><code>{fn.name}</code></td><td>{escape(doc)}</td></tr>")
    return "".join(rows)


groups_html, toc = [], [("start", "Getting started"), ("conventions", "Conventions"), ("claude", "Claude connector")]
for gid, gname, prefixes in GROUPS:
    eps = [(m, p, d) for m, p, d in api.ENDPOINTS if any(p == x or p.startswith(x + "/") for x in prefixes)]
    if not eps:
        continue
    toc.append((gid, gname))
    groups_html.append(f'<h2 id="{gid}">{escape(gname)}</h2>' + "".join(endpoint_html(*ep) for ep in eps))
toc.append(("openapi", "OpenAPI file"))
index = EX[0]

body = f"""
<h1>Flip for Free API</h1>
<p class="lede">Everything you can see in Flip for Free, you can read and change from a script, a spreadsheet or
Claude. The API runs inside your own copy, on your computer or your own server: nobody else has your data.</p>

<h2 id="start">Getting started</h2>
<ol class="steps">
<li><b>Create a key.</b> In Flip for Free go to <b>Settings → API keys</b>, name the key (for example "Spreadsheet")
and copy it. It starts with <code>flip_</code> and is only shown once. Delete it there to stop it working.</li>
<li><b>Find your address.</b> On your computer it's <code>https://localhost:8765</code>. On a server it's your
Tailscale address, such as <code>https://flip.your-tailnet.ts.net</code>.</li>
<li><b>Make a request.</b> Send the key as a bearer token. Your own copy uses a self-signed certificate, hence
<code>-k</code> (curl) or <code>verify=False</code> (Python) on <code>localhost</code>; a Tailscale address has a
real certificate.
<pre><code>export FLIP_KEY=flip_your_key_here
curl -k https://localhost:8765/api/v1/summary?period=30d -H "Authorization: Bearer $FLIP_KEY"</code></pre></li>
</ol>

<h2 id="conventions">Conventions</h2>
<table>
<tr><th>Topic</th><th>How it works</th></tr>
<tr><td>Base path</td><td><code>/api/v1</code>. <code>GET /api/v1</code> lists every endpoint as JSON.</td></tr>
<tr><td>Money</td><td>Pounds (GBP) as numbers, e.g. <code>12.5</code>. CardTrader and Shopify sales in other currencies are converted with the European Central Bank's daily rate.</td></tr>
<tr><td>Dates</td><td><code>YYYY-MM-DD</code>. Timestamps in responses are ISO 8601 in UTC.</td></tr>
<tr><td>Periods</td><td><code>?period=</code> <code>today</code>, <code>7d</code>, <code>30d</code>, <code>month</code>, <code>last_month</code>, <code>90d</code>, <code>ytd</code>, <code>fy</code> (financial year), <code>all</code>, <code>custom</code> with <code>start</code> and <code>end</code>, or a saved range <code>r:&lt;id&gt;</code>.</td></tr>
<tr><td>Errors</td><td>A JSON body <code>{{"error": "…"}}</code> with the HTTP status: 400 bad input, 401 missing or wrong key, 404 not found, 409 would change a filed tax year.</td></tr>
<tr><td>Filed tax years</td><td>Changes that would alter a tax year you've marked as filed are refused with 409 unless the body includes <code>"confirm_closed": true</code>.</td></tr>
<tr><td>Writes</td><td>Send JSON with <code>Content-Type: application/json</code>. PATCH changes only the fields you send.</td></tr>
</table>

<h2 id="claude">Claude connector</h2>
<p>Flip for Free also runs a Model Context Protocol (MCP) server, so Claude can use the same API with plain English:
<i>"I sold a Zara coat on Vinted for £28 yesterday"</i> or <i>"which sales are missing a cost?"</i>.</p>
<ol class="steps">
<li>Create an API key in <b>Settings → API keys</b> (name it "Claude desktop").</li>
<li>Install the connector's library once, in the Flip for Free folder: <code>pip install -r requirements.txt</code></li>
<li>In the Claude desktop app open <b>Settings → Developer → Edit Config</b> and add (with your own folder and key):
<pre><code>{{
  "mcpServers": {{
    "flip-for-free": {{
      "command": "python",
      "args": ["C:\\Users\\you\\flip-for-free\\mcp_server.py"],
      "env": {{ "FLIP_URL": "https://localhost:8765", "FLIP_API_KEY": "flip_your_key_here" }}
    }}
  }}
}}</code></pre></li>
<li>Restart Claude. The tools below appear while Flip for Free is running.</li>
</ol>
<table><tr><th>Tool</th><th>What it does</th></tr>
{mcp_tools()}</table>

{''.join(groups_html)}

<h2 id="openapi">OpenAPI file</h2>
<p>The whole API is described in <a href="openapi.json">openapi.json</a> (OpenAPI 3.1, examples included). Load it into
Postman, Insomnia or Bruno, or generate a client with <code>openapi-generator</code>. Both this page and that file are
made by <code>tools/gen_api_docs.py</code>, which calls every endpoint on demo data, so the examples are real output.</p>
"""

script = """<script>
document.querySelectorAll('.tabs').forEach(function (t) {
  t.addEventListener('click', function (ev) {
    var b = ev.target.closest('button'); if (!b) return;
    t.querySelectorAll('button').forEach(function (x) {
      var on = x === b; x.setAttribute('aria-selected', on); document.getElementById(x.dataset.for).hidden = !on;
    });
  });
});
</script>"""

html = page(file="api.html", title="Flip for Free API: endpoints, keys and examples",
            description="The Flip for Free JSON API: create a key, then read profit, sales, stock and tax, and record "
                        "sales, items, expenses and mileage. Real examples in curl, Python and JavaScript.",
            body=body, toc=toc, script=script)
(ROOT / "docs" / "api.html").write_text(html)
print("wrote docs/api.html", len(html), "bytes;", len(api.ENDPOINTS), "endpoints")
