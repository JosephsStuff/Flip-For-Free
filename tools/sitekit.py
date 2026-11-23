"""Shared frame for the reading pages on GitHub Pages (docs/*.html built by tools/gen_*.py)."""
from html import escape

SITE = "https://josephsstuff.github.io/Flip-For-Free/"
NAV = [("index.html", "Home"), ("install.html", "Install"), ("how-it-works.html", "How it works"),
       ("api.html", "API"), ("oracle.html", "Free server"),
       ("https://github.com/JosephsStuff/Flip-For-Free", "GitHub")]


def page(*, file: str, title: str, description: str, body: str, toc: list[tuple[str, str]] | None = None,
         extra_head: str = "", script: str = "") -> str:
    nav = "".join(f'<a href="{h}"{" aria-current=page" if h == file else ""}>{escape(t)}</a>' for h, t in NAV)
    # code blocks scroll sideways, so they must be reachable with the keyboard (WCAG 2.1.1)
    body = body.replace("<pre>", '<pre tabindex="0">').replace("<pre id=", '<pre tabindex="0" id=')
    toc_html = ""
    if toc:
        toc_html = ('<aside class="toc" aria-label="On this page"><p>On this page</p><ol>' +
                    "".join(f'<li><a href="#{a}">{escape(t)}</a></li>' for a, t in toc) + "</ol></aside>")
    return f"""<!doctype html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<meta name="description" content="{escape(description)}">
<link rel="canonical" href="{SITE}{file}">
<meta property="og:title" content="{escape(title)}">
<meta property="og:description" content="{escape(description)}">
<meta property="og:image" content="{SITE}share.jpg">
<meta property="og:url" content="{SITE}{file}">
<meta name="twitter:card" content="summary_large_image">
<link rel="icon" href="favicon.svg" type="image/svg+xml">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Funnel+Display:wght@400..700&family=Funnel+Sans:wght@400..600&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<link rel="stylesheet" href="docs.css">
{extra_head}
</head>
<body>
<a class="skip" href="#main">Skip to content</a>
<header class="bar"><a class="brand" href="index.html"><img src="favicon.svg" alt="">Flip for Free</a><nav aria-label="Site">{nav}</nav></header>
<div class="doc">{toc_html}<main id="main">{body}</main></div>
<footer>Flip for Free is free for your own selling. Source on <a href="https://github.com/JosephsStuff/Flip-For-Free">GitHub</a> · FSL-1.1-MIT licence</footer>
{script}
</body>
</html>
"""
