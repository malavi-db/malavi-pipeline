#!/usr/bin/env python3
# @title Build the MalAvi orientation guide from its editable source
# @purpose Inline League Gothic into malavi-user-guide.src.html and write the
#   self-contained page that gets published.
# @why The font is 17,000 characters of base64 on one line. Leaving it in the file
#   anyone edits by hand makes the file hostile to edit; keeping it out of the
#   published file is not an option, because the artifact host blocks external fonts.
# @input ops/malavi-user-guide.src.html
# @input curation/src/malavi_curation/_league_gothic_b64.py
# @output ops/*.html
# @output docs/how-it-works.html
# @program python3
# @critical-var PLACEHOLDER
# @critical-var GUIDE_START
"""Turn the editable guide source into the two pages it is published as.

Edit ``malavi-user-guide.src.html`` — ordinary HTML with one placeholder where the font
goes. Run this and both outputs are rebuilt from it:

* ``ops/malavi-user-guide.html`` — self-contained, for the private artifact. Carries its
  own copy of the design tokens and the font inlined as base64, because the artifact host
  blocks every outside request.
* ``docs/how-it-works.html`` — the public site page. Takes tokens, font and masthead from
  the site itself, so it looks like the rest of MalAvi and follows the site's theme toggle.

One source, two wrappers, because the alternative is two copies of the same prose drifting
apart — and the one that drifts is always the one nobody is looking at.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Where the font goes. The source keeps this marker so the file stays readable.
PLACEHOLDER = "__LEAGUE_GOTHIC__"

ROOT = Path(__file__).resolve().parents[1]
STYLE_PARTIAL = ROOT / "ops" / "_shared_style.html"

# Every document built from this directory. `site` is the public page it also becomes, or
# None for documents that stay private. One registry so adding a document is one entry
# rather than a new script that slowly diverges from this one.
DOCS = {
    "guide": {
        "source": ROOT / "ops" / "malavi-user-guide.src.html",
        "output": ROOT / "ops" / "malavi-user-guide.html",
        "site": ROOT / "docs" / "how-it-works.html",
        "title": "How MalAvi works after the update",
        "description": "How the rebuilt MalAvi works: how data are submitted, "
                       "how curators review them, and how a new version is generated.",
        "artifact": "https://claude.ai/code/artifact/4915b73f-7414-409a-802e-551ab24599b8",
    },
    "curator": {
        "source": ROOT / "ops" / "curator-instructions.src.html",
        "output": ROOT / "ops" / "curator-instructions.html",
        "site": ROOT / "docs" / "curating.html",
        "title": "A guide to curating MalAvi",
        "description": "What a MalAvi curator is asked to do, and how to do it.",
        "artifact": "https://claude.ai/code/artifact/701dcd9c-2a79-4f12-b106-584a4c83b7f9",
    },
}

SOURCE = DOCS["guide"]["source"]
OUTPUT = DOCS["guide"]["output"]
SITE_OUTPUT = DOCS["guide"]["site"]

# The shared content, between these markers in the source. Everything outside them differs
# between the two outputs and is supplied by the wrappers below.
GUIDE_START = "<!-- GUIDE:CONTENT-START -->"
GUIDE_END = "<!-- GUIDE:CONTENT-END -->"

# The style block splits at this comment: above it is standalone-only (tokens, font, body
# ground), below it is the guide's own layout, scoped under .guide and shared by both.
STYLE_SPLIT = "/* ==== GUIDE ===="
STYLE_INCLUDE = "<!-- INCLUDE:STYLE -->"


def _with_style(text: str) -> str:
    """Splice the shared style partial into a source's INCLUDE marker.

    The two documents share one stylesheet on purpose: they are read one after the other
    and should look like the same object. Keeping it in a partial rather than copied into
    each source means a change to the type scale cannot land in one and not the other.
    """
    if STYLE_INCLUDE not in text:
        raise ValueError(f"source is missing its {STYLE_INCLUDE!r} marker")
    return text.replace(STYLE_INCLUDE, STYLE_PARTIAL.read_text(encoding="utf-8"))


def _with_assets(text: str) -> str:
    """Splice any base64 image assets into their placeholders.

    Images live beside the sources as .b64 files rather than inline, for the same reason
    the font does: a 45,000-character data URI on one line makes the file impossible to
    edit by hand. The placeholder is __NAME__ for ops/assets/name.png.b64, lowercased with
    underscores becoming hyphens.
    """
    import re as _re
    for placeholder in set(_re.findall(r"__([A-Z0-9_]+)__", text)):
        if placeholder == "LEAGUE_GOTHIC":
            continue
        asset = ROOT / "ops" / "assets" / (placeholder.lower().replace("_", "-") + ".png.b64")
        if asset.is_file():
            text = text.replace(f"__{placeholder}__", asset.read_text().strip())
        else:
            raise ValueError(f"no asset for placeholder __{placeholder}__ (looked for {asset})")
    return text


def _site_base_url() -> str:
    """Where the published site serves from, per config/project.yml links.site.

    Returns an empty string when it is not configured, which leaves relative links alone
    and lets the external-reference check below report them rather than this quietly
    inventing a hostname.
    """
    import yaml
    config = yaml.safe_load((ROOT / "config" / "project.yml").read_text(encoding="utf-8"))
    return (config.get("links") or {}).get("site", "").rstrip("/")


def _absolutize(page: str) -> str:
    """Point site-relative links at the published site, for the standalone artifact only.

    A source document links to ``assets/downloads/example-curator-report.pdf`` because that
    is what works on the site. The standalone copy is hosted elsewhere, so the same path
    would resolve against the artifact host and 404. Rewriting it here means one source
    produces a working link in both outputs.

    Only ``src``/``href`` values that are genuinely site-relative are touched: anchors,
    data URIs and anything already absolute are left exactly as they are.
    """
    import re
    base = _site_base_url()
    if not base:
        return page

    def replace(match: "re.Match") -> str:
        attribute, value = match.group(1), match.group(2)
        if value.startswith(("#", "data:", "http://", "https://", "mailto:", "//")):
            return match.group(0)
        return f'{attribute}="{base}/{value.lstrip("/")}"'

    return re.sub(r'(src|href)="([^"]+)"', replace, page)


def _parts(text: str) -> tuple:
    """Split the source into (shared style, content). Raises if a marker is missing."""
    for marker in (GUIDE_START, GUIDE_END, STYLE_SPLIT):
        if marker not in text:
            raise ValueError(f"source is missing its {marker!r} marker")
    style = text[text.index(STYLE_SPLIT):text.index("</style>")]
    content = text[text.index(GUIDE_START) + len(GUIDE_START):text.index(GUIDE_END)]
    return style, content


def build_site_page(text: str, title: str = "How MalAvi works after the update",
                    description: str = "") -> str:
    """The public site page: the guide's content wearing the site's own chrome.

    The masthead is lifted from docs/index.html at build time rather than copied, so a
    change to the site's header reaches this page too. Its nav buttons are dropped: the
    site is a single-page app with no deep links, so a nav button here would either do
    nothing or land the reader on the home view having asked for something else. A link
    back to the site is honest about where it goes.
    """
    index = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
    brand_start = index.index('<div class="brand" id="brandHome">')
    brand_end = index.index("</div>", index.index('class="brand-sub"')) + len("</div>")
    brand = index[brand_start:brand_end]
    # Close whatever the slice left open, counted rather than assumed. The brand markup
    # nests three deep and guessing the number is exactly the kind of thing that produces
    # a page which looks fine until a browser reflows it.
    import re as _re
    unclosed = (len(_re.findall(r"<div[\s>]", brand)) - len(_re.findall(r"</div>", brand)))
    brand += "\n      " + "</div>" * unclosed

    style, content = _parts(text)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{description}">
<link rel="icon" href="assets/img/favicon.ico">
<link rel="stylesheet" href="assets/css/malavi.css">
<style>
{style}
/* The site page keeps the guide off the masthead's toes. */
.guide{{padding-top:8px}}
.guide header.hero{{padding-top:40px}}
</style>
</head>
<body>
<header class="masthead">
  <div class="masthead-inner">
    <a href="index.html" style="text-decoration:none; color:inherit">
      {brand}
    </a>
    <nav class="primary">
      <a href="index.html" style="text-decoration:none">Back to MalAvi</a>
    </nav>
    <button class="theme-toggle" id="themeBtn" type="button">Dark</button>
  </div>
</header>

<main>
{content}
</main>

<script>
/* The site's malavi.js drives the single-page app and expects elements this page does not
   have, so it is not loaded here. This is only the theme toggle, matching its behaviour so
   a reader's choice looks the same on both pages. */
(function () {{
  var root = document.documentElement, btn = document.getElementById("themeBtn");
  function dark() {{
    var set = root.getAttribute("data-theme");
    if (set) return set === "dark";
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
  }}
  btn.addEventListener("click", function () {{
    root.setAttribute("data-theme", dark() ? "light" : "dark");
    btn.textContent = dark() ? "Light" : "Dark";
  }});
  btn.textContent = dark() ? "Light" : "Dark";
}})();
</script>
</body>
</html>
"""


def build(name: str = "guide", dry_run: bool = False) -> int:
    """Build one registered document. Returns 0 on success."""
    spec = DOCS[name]
    source, output = spec["source"], spec["output"]

    sys.path.insert(0, str(ROOT / "curation" / "src"))
    from malavi_curation._league_gothic_b64 import WOFF_BASE64

    if not source.is_file():
        print(f"No source at {source}", file=sys.stderr)
        return 1

    text = _with_style(source.read_text(encoding="utf-8"))
    text = _with_assets(text)
    if PLACEHOLDER not in text:
        # Rebuilding from an already-built file would embed the font twice and produce a
        # page that silently falls back to Impact.
        print(f"{source.name} has no {PLACEHOLDER} placeholder. Is it already built?",
              file=sys.stderr)
        return 1

    page = text.replace(PLACEHOLDER, WOFF_BASE64.strip())

    # A link to something the site serves (assets/downloads/..., index.html) is written
    # site-relative in the source, because that is what the site page needs. The standalone
    # artifact is hosted somewhere else entirely and would resolve the same path against
    # the artifact host, so it is rewritten to the published site's absolute URL here --
    # only in the standalone copy. The site page below keeps the relative form.
    page = _absolutize(page)

    # Anything still pointing outside after that. The artifact host blocks every outside
    # request, so a stray reference does not degrade -- it silently disappears from the
    # page, which is why this is fatal rather than a warning.
    #
    # Fatal for the standalone artifact only: returning here used to abandon the site page
    # as well, so one link that is perfectly valid on the site (the example curator report)
    # silently stopped docs/curating.html being rebuilt at all, and the published page went
    # stale while the build reported an error about a different output.
    import re
    external = re.findall(r'(?:src|href)="(?!#|data:|https://)([^"]+)"', page)
    if external:
        print(f"External references found, which will not load: {external}",
              file=sys.stderr)
        if spec["site"]:
            site_page = build_site_page(text, spec["title"], spec["description"])
            spec["site"].write_text(site_page, encoding="utf-8")
            print(f"wrote {spec['site']} anyway (the site resolves these paths); "
                  f"the standalone {output.name} was NOT written", file=sys.stderr)
        return 1

    size_kb = len(page.encode("utf-8")) // 1024
    if dry_run:
        print(f"[dry-run] would write {output} ({size_kb} KB)")
        if spec["site"]:
            print(f"[dry-run] would write {spec['site']}")
        return 0

    output.write_text(page, encoding="utf-8")
    print(f"wrote {output} ({size_kb} KB)")

    if spec["site"]:
        site_page = build_site_page(text, spec["title"], spec["description"])
        spec["site"].write_text(site_page, encoding="utf-8")
        print(f"wrote {spec['site']} "
              f"({len(site_page.encode('utf-8')) // 1024} KB, uses the site's CSS)")
    if spec["artifact"]:
        print("Publish to the SAME artifact URL so the existing link keeps working:")
        print(f"  {spec['artifact']}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("doc", nargs="?", default="all",
                        choices=["all", *DOCS], help="which document to build")
    parser.add_argument("--dry-run", action="store_true", help="report, write nothing")
    args = parser.parse_args(argv)

    names = list(DOCS) if args.doc == "all" else [args.doc]
    worst = 0
    for name in names:
        if not DOCS[name]["source"].is_file():
            print(f"skipping {name}: no source yet at {DOCS[name]['source']}")
            continue
        worst = max(worst, build(name, dry_run=args.dry_run))
    return worst


if __name__ == "__main__":
    sys.exit(main())
