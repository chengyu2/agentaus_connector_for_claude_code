"""A text-mode browser over a fixed local site.

It renders a page the way a text browser does: headings, paragraphs, table rows and
numbered links. Images appear as their file name only, and scripts are not run, so a
fact held only in an image or loaded by script cannot be read - which is the point of
the tasks that use them.
"""
from __future__ import annotations

from html.parser import HTMLParser

BLOCK = {"p", "div", "h1", "h2", "h3", "h4", "li", "tr", "br", "table", "section", "ul", "ol"}


class _Render(HTMLParser):
    def __init__(self):
        super().__init__()
        self.out: list[str] = []
        self.links: list[str] = []
        self.skip = 0
        self.href: str | None = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("script", "style"):
            self.skip += 1
        elif tag == "a" and attrs.get("href"):
            self.href = attrs["href"]
        elif tag == "img":
            self.out.append(f"[image: {attrs.get('src', '?').rsplit('/', 1)[-1]}]")
        elif tag in ("td", "th"):
            self.out.append(" | ")
        if tag in BLOCK:
            self.out.append("\n")
        if tag in ("h1", "h2", "h3"):
            self.out.append("#" * int(tag[1]) + " ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip -= 1
        elif tag == "a" and self.href is not None:
            self.links.append(self.href)
            self.out.append(f" [{len(self.links)}]")
            self.href = None
        if tag in BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(" ".join(data.split()) if data.strip() else "")


class Browser:
    def __init__(self, site: dict[str, str], host: str):
        self.site = site
        self.host = host
        self.links: list[str] = []
        self.history: list[str] = []

    def open(self, url: str) -> str:
        path = url.split(self.host, 1)[-1] if self.host in url else url
        path = "/" + path.lstrip("/").split("#")[0]
        if path not in self.site:
            return f"404 Not Found: {path}"
        render = _Render()
        render.feed(self.site[path])
        self.links = render.links
        self.history.append(path)
        text = "".join(render.out)
        lines = [line.strip() for line in text.splitlines()]
        body = "\n".join(line for line in lines if line)
        links = "\n".join(f"[{i}] {href}" for i, href in enumerate(self.links, 1))
        return f"URL: https://{self.host}{path}\n\n{body}\n\nLinks:\n{links or '(none)'}"

    def follow(self, number: int) -> str:
        if not 1 <= number <= len(self.links):
            return f"No link [{number}] on this page."
        return self.open(self.links[number - 1])
