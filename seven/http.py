"""A polite HTTP client.

Rules we follow on every competitor site (they keep us unblocked AND
they're the right thing to do):
  1. At most one request per `min_interval` seconds per host.
  2. Respect robots.txt.
  3. Honest User-Agent.
  4. Back off and retry on 429 / 5xx instead of hammering.
"""

from __future__ import annotations

import time
import urllib.robotparser
from urllib.parse import urlsplit

import httpx

def _wire_bytes(r: httpx.Response) -> int:
    """Bytes as they crossed the network (compressed). This is what a proxy bills;
    decompressed pages are 5-40x bigger (a Salla home page: 46 KB vs 1.9 MB)."""
    return r.num_bytes_downloaded or len(r.content)


# Seconds between requests for hosts that share one pace (see set_group).
# Shopify: live, ~25 requests in a few minutes across 5 stores got EVERY Shopify
# store answering 429 (Retry-After: 60), including stores we had not touched.
# 30 s is a cautious first guess; measure the real tolerance before the MVP relies on it.
GROUP_INTERVALS = {"shopify": 30.0}

USER_AGENT = "SevenMonitor/0.1 (competitor price monitoring; public product data only)"


class PoliteClient:
    def __init__(
        self,
        min_interval: float = 1.0,
        timeout: float = 20.0,
        respect_robots: bool = True,
        transport: httpx.BaseTransport | None = None,   # tests inject a fake network here
        group_intervals: dict[str, float] | None = None,
        signer=None,                                     # botauth.Signer: Web Bot Auth on every request
    ) -> None:
        self.min_interval = min_interval
        self.respect_robots = respect_robots
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "ar,en;q=0.8"},
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
            event_hooks={"request": [self._sign]} if signer else None,
        )
        self.signer = signer
        self._last_hit: dict[str, float] = {}     # pace key -> time of our last request
        # Hosts that share one pace (e.g. every Shopify store: Shopify limits us per IP
        # across all its stores). pace key = group name for grouped hosts, else the host.
        self._group_of: dict[str, str] = {}
        self.group_intervals = dict(GROUP_INTERVALS if group_intervals is None else group_intervals)
        self._paused_until: dict[str, float] = {}  # pace key -> time a 429 told us to wait until
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        # Counters so every read can report what it cost us.
        self.requests_made = 0
        self.bytes_received = 0

    # ---------- internals ----------

    def _sign(self, request: httpx.Request) -> None:
        """Runs just before each request leaves (redirect hops too): add Web Bot Auth headers
        for the exact host we're talking to."""
        url = request.url
        authority = url.host if url.port is None else f"{url.host}:{url.port}"
        request.headers.update(self.signer.request_headers(authority))

    def set_group(self, host: str, group: str) -> None:
        """Make `host` share one request pace with every other host in `group`."""
        self._group_of[host] = group

    def _key(self, host: str) -> str:
        return self._group_of.get(host, host)

    def _wait_turn(self, host: str) -> None:
        """Sleep just long enough to keep one request per interval per host (or group),
        and until any "Retry-After" pause for it has passed."""
        key = self._key(host)
        interval = self.group_intervals.get(key, self.min_interval) if key != host else self.min_interval
        wait = self._paused_until.get(key, 0) - time.monotonic()
        last = self._last_hit.get(key)
        if last is not None:
            wait = max(wait, interval - (time.monotonic() - last))
        if wait > 0:
            time.sleep(wait)
        self._last_hit[key] = time.monotonic()

    def _robots_for(self, scheme: str, host: str) -> urllib.robotparser.RobotFileParser:
        if host not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                self._wait_turn(host)
                r = self._client.get(f"{scheme}://{host}/robots.txt")
                self.requests_made += 1
                self.bytes_received += _wire_bytes(r)
                if r.status_code in (401, 403):
                    rp.disallow_all = True          # same rule Python's stdlib uses
                elif r.status_code == 200:
                    rp.parse(r.text.splitlines())
                else:
                    rp.allow_all = True             # no robots.txt = everything allowed
            except httpx.HTTPError:
                rp.allow_all = True                 # can't fetch it -> don't block ourselves
            self._robots[host] = rp
        return self._robots[host]

    # ---------- public ----------

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parts = urlsplit(url)
        return self._robots_for(parts.scheme, parts.netloc).can_fetch(USER_AGENT, url)

    def get(self, url: str | httpx.URL, *, params: dict | None = None, headers: dict | None = None,
            retries: int = 3) -> httpx.Response:
        url = str(url)
        if not self.allowed(url):
            raise PermissionError(f"robots.txt disallows {url}")
        host = urlsplit(url).netloc
        for attempt in range(retries + 1):
            self._wait_turn(host)
            try:
                r = self._client.get(url, params=params, headers=headers)
            except httpx.TransportError:
                # Timeout or dropped connection: seen live on Zid product pages
                # (~500 KB each). Back off and retry; give up after `retries`.
                if attempt < retries:
                    time.sleep(2 ** attempt * 2)
                    continue
                raise
            self.requests_made += 1
            self.bytes_received += _wire_bytes(r)
            # 429 = "slow down", 5xx = server hiccup. Wait longer each time.
            if r.status_code == 429 or r.status_code >= 500:
                retry_after = r.headers.get("Retry-After", "")
                wait = min(float(retry_after) if retry_after.isdigit() else 2 ** attempt * 2, 120)
                # Every 429 pauses the whole group, even after our last retry:
                # the next Shopify store must not walk straight into the same limit.
                self._paused_until[self._key(host)] = time.monotonic() + wait
                if attempt < retries:
                    continue
            return r
        return r  # last response after all retries

    def close(self) -> None:
        self._client.close()
