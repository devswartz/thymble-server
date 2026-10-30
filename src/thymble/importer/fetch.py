"""Server-side page fetch: the fallback when the device could not send the page (D-025).

Polite by design: an honest User-Agent, robots.txt honoured, one request, a timeout and a
size cap. Bot protection (Cloudflare challenges, 403/429) is reported, never worked around;
the app then asks the user to share the page from the browser instead.

Because the URL comes from a user and the server may run on a home network, only public
http(s) addresses are fetched (no localhost, LAN, link-local or metadata addresses).
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from charset_normalizer import from_bytes

from thymble import __version__

MAX_REDIRECTS = 5


class FetchError(Exception):
    """The page could not be fetched; `str(exc)` is safe to show to the user."""


@dataclass
class FetchedPage:
    html: str
    final_url: str
    status: int


def user_agent(public_url: str) -> str:
    return f"ThymbleBot/{__version__} (+{public_url}; recipe import for one user)"


async def _check_public(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FetchError("only http(s) URLs can be imported")
    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo, parts.hostname, parts.port or 443, type=socket.SOCK_STREAM
        )
    except OSError as exc:
        raise FetchError(f"could not resolve {parts.hostname}") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise FetchError("that address is not on the public internet")


def decode_html(body: bytes, content_type: str | None) -> str:
    """Honour the HTTP charset, then <meta charset>, then detect; never assume Latin-1 just
    because the header has no charset (that garbles UTF-8 pages)."""
    m = re.search(r"charset=[\"']?([\w-]+)", content_type or "", flags=re.I)
    if not m:
        m = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", body[:4096], flags=re.I)
    if m:
        encoding = m.group(1)
        if isinstance(encoding, bytes):
            encoding = encoding.decode("ascii", "ignore")
        try:
            return body.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            pass
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        best = from_bytes(body).best()
        return str(best) if best is not None else body.decode("utf-8", errors="replace")


def blocked_reason(status: int, body: str) -> str | None:
    low = body[:200_000].lower()
    if status in (401, 402, 403, 429, 503):
        if "cloudflare" in low or "cf-chl" in low or "just a moment" in low:
            return "the site uses a bot check (Cloudflare)"
        if "captcha" in low or "perimeterx" in low:
            return "the site uses a captcha"
        if status == 429:
            return "the site is rate-limiting requests (HTTP 429)"
        return f"the site refused the request (HTTP {status})"
    if status >= 400:
        return f"the page returned HTTP {status}"
    return None


async def _robots_allows(client: httpx.AsyncClient, url: str, agent: str) -> bool:
    robots_url = urljoin(url, "/robots.txt")
    try:
        response = await client.get(robots_url)
    except httpx.HTTPError:
        return True  # unreachable robots.txt: no rules to honour
    if response.status_code >= 400:
        return True
    parser = RobotFileParser()
    parser.parse(response.text.splitlines())
    return parser.can_fetch(agent, url)


async def fetch_page(
    url: str,
    *,
    public_url: str,
    timeout: float,
    max_bytes: int,
    transport: httpx.AsyncBaseTransport | None = None,
    check_address: bool = True,
) -> FetchedPage:
    agent = user_agent(public_url)
    headers = {
        "User-Agent": agent,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
        "Accept-Language": "en;q=0.9,*;q=0.5",
    }
    async with httpx.AsyncClient(
        headers=headers, timeout=timeout, follow_redirects=False, transport=transport
    ) as client:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            if check_address:
                await _check_public(current)
            if not await _robots_allows(client, current, agent):
                raise FetchError("the site's robots.txt does not allow fetching this page")
            try:
                async with client.stream("GET", current) as response:
                    if response.is_redirect and "location" in response.headers:
                        current = urljoin(current, response.headers["location"])
                        continue
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise FetchError("the page is too large to import")
                        chunks.append(chunk)
                    html = decode_html(b"".join(chunks), response.headers.get("content-type"))
                    status = response.status_code
            except httpx.TimeoutException as exc:
                raise FetchError("the site took too long to answer") from exc
            except httpx.HTTPError as exc:
                raise FetchError(f"could not reach the site ({type(exc).__name__})") from exc
            reason = blocked_reason(status, html)
            if reason:
                raise FetchError(f"{reason}; open the recipe in the app and share it from there")
            return FetchedPage(html=html, final_url=current, status=status)
        raise FetchError("too many redirects")
