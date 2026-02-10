# FILE: scripts/netlify_fix.py
# Purpose: Netlify-only hotfix so sitemap.xml, robots.txt, rss.xml, IndexNow key file(s),
#          and verification files are actually present and not hijacked by Netlify rewrites.

from __future__ import annotations

import csv
import json
import os
import re
import shutil
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, ElementTree


INDEXNOW_KEY_RE = re.compile(r"^[a-f0-9]{32}\.txt$", re.I)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _norm_base_url(u: str) -> str:
    u = (u or "").strip()
    if not u:
        return u
    return u.rstrip("/")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _copy_if_missing(src: Path, dst: Path) -> None:
    if dst.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _collect_internal_pages(out_dir: Path, base_url: str) -> list[str]:
    pages: set[str] = set()

    if (out_dir / "index.html").exists():
        pages.add(base_url + "/")

    for p in out_dir.rglob("*.html"):
        rel = p.relative_to(out_dir).as_posix()
        if rel == "index.html":
            continue
        pages.add(f"{base_url}/{rel}")

    for name in ("all.html", "about.html", "rss.xml", "sitemap.xml", "robots.txt", "backlink-feed.xml"):
        if (out_dir / name).exists():
            pages.add(f"{base_url}/{name}")

    return sorted(pages)


def _ensure_robots(out_dir: Path, base_url: str) -> None:
    robots = out_dir / "robots.txt"
    text = "\n".join(
        [
            "User-agent: *",
            "Allow: /",
            f"Sitemap: {base_url}/sitemap.xml",
            "",
        ]
    )
    _write_text(robots, text)


def _ensure_sitemap(out_dir: Path, base_url: str, now_utc: datetime) -> None:
    urls = _collect_internal_pages(out_dir, base_url)
    urlset = Element("urlset", {"xmlns": "http://www.sitemaps.org/schemas/sitemap/0.9"})
    lastmod = now_utc.date().isoformat()

    for loc in urls:
        u = SubElement(urlset, "url")
        SubElement(u, "loc").text = loc
        SubElement(u, "lastmod").text = lastmod

    sitemap_path = out_dir / "sitemap.xml"
    sitemap_path.parent.mkdir(parents=True, exist_ok=True)
    ElementTree(urlset).write(sitemap_path, encoding="utf-8", xml_declaration=True)


def _read_recent_urls(repo_root: Path, limit: int = 50) -> list[str]:
    p = repo_root / "data" / "daily.csv"
    if not p.exists():
        return []
    urls: list[str] = []
    with p.open("r", encoding="utf-8", newline="") as f:
        r = csv.reader(f)
        rows = list(r)

    for row in rows[1:]:
        if not row:
            continue
        u = (row[0] or "").strip()
        if u.startswith("http://") or u.startswith("https://"):
            urls.append(u)

    # keep most recent
    if len(urls) > limit:
        urls = urls[-limit:]
    return urls


def _load_enriched(repo_root: Path) -> dict:
    p = repo_root / "data" / "enriched.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _ensure_rss(out_dir: Path, repo_root: Path, base_url: str, now_utc: datetime) -> None:
    urls = _read_recent_urls(repo_root, limit=50)
    enriched = _load_enriched(repo_root).get("items", {}) if isinstance(_load_enriched(repo_root), dict) else {}

    rss = Element("rss", {"version": "2.0"})
    channel = SubElement(rss, "channel")
    SubElement(channel, "title").text = "Discovery Hub"
    SubElement(channel, "link").text = base_url + "/"
    SubElement(channel, "description").text = "Recently added links"
    SubElement(channel, "lastBuildDate").text = format_datetime(now_utc)

    for u in reversed(urls):  # latest first
        meta = enriched.get(u, {}) if isinstance(enriched, dict) else {}
        title = (meta.get("title") or "").strip() or u
        desc = (meta.get("summary") or meta.get("description") or "").strip() or u

        item = SubElement(channel, "item")
        SubElement(item, "title").text = title
        SubElement(item, "link").text = u
        SubElement(item, "guid").text = u
        SubElement(item, "description").text = desc
        SubElement(item, "pubDate").text = format_datetime(now_utc)

    rss_path = out_dir / "rss.xml"
    rss_path.parent.mkdir(parents=True, exist_ok=True)
    ElementTree(rss).write(rss_path, encoding="utf-8", xml_declaration=True)


def _ensure_netlify_overrides(out_dir: Path) -> None:
    # If Netlify UI has a catch-all rewrite, this file forces exact files to resolve as themselves.
    redirects_lines: list[str] = []

    must_files = [
        "robots.txt",
        "sitemap.xml",
        "rss.xml",
        "backlink-feed.xml",
        "index.html",
        "all.html",
        "about.html",
    ]

    for fn in must_files:
        if (out_dir / fn).exists():
            redirects_lines.append(f"/{fn} /{fn} 200")

    # daily pages
    if (out_dir / "d").exists():
        redirects_lines.append("/d/* /d/:splat 200")

    # google verification + indexnow key files if present
    for fn in sorted([p.name for p in out_dir.iterdir() if p.is_file()]):
        if fn.lower().startswith("google") and fn.lower().endswith(".html"):
            redirects_lines.append(f"/{fn} /{fn} 200")
        if INDEXNOW_KEY_RE.match(fn):
            redirects_lines.append(f"/{fn} /{fn} 200")

    _write_text(out_dir / "_redirects", "\n".join(redirects_lines) + "\n")

    headers = []
    def add_header(path: str, content_type: str) -> None:
        headers.append(path)
        headers.append(f"  Content-Type: {content_type}")
        headers.append("")

    add_header("/sitemap.xml", "application/xml; charset=utf-8")
    add_header("/rss.xml", "application/rss+xml; charset=utf-8")
    add_header("/backlink-feed.xml", "application/xml; charset=utf-8")
    add_header("/robots.txt", "text/plain; charset=utf-8")

    _write_text(out_dir / "_headers", "\n".join(headers).rstrip() + "\n")


def _copy_root_tokens(repo_root: Path, out_dir: Path) -> None:
    # Copy IndexNow key file(s) and Google verification files if they exist in docs/
    docs_dir = repo_root / "docs"
    if not docs_dir.exists():
        return

    for p in docs_dir.glob("google*.html"):
        _copy_if_missing(p, out_dir / p.name)

    for p in docs_dir.glob("*.txt"):
        if INDEXNOW_KEY_RE.match(p.name):
            _copy_if_missing(p, out_dir / p.name)


def main() -> None:
    import sys

    if len(sys.argv) < 3:
        raise SystemExit("Usage: python scripts/netlify_fix.py <OUT_DIR> <BASE_URL>")

    out_dir = Path(sys.argv[1]).resolve()
    base_url = _norm_base_url(sys.argv[2])

    if not out_dir.exists():
        raise SystemExit(f"OUT_DIR not found: {out_dir}")
    if not base_url.startswith("http"):
        raise SystemExit("BASE_URL must start with http(s)")

    repo_root = Path(__file__).resolve().parents[1]
    now_utc = _utc_now()

    _copy_root_tokens(repo_root, out_dir)
    _ensure_robots(out_dir, base_url)
    _ensure_rss(out_dir, repo_root, base_url, now_utc)
    _ensure_sitemap(out_dir, base_url, now_utc)
    _ensure_netlify_overrides(out_dir)

    print("Netlify fix done:", out_dir)


if __name__ == "__main__":
    main()
