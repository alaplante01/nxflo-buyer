"""
Nexflo Full-Pipeline Stress Test — comprehensive RTB chain testing
Usage: python stress-test.py [test_name] [total_requests] [concurrency]

Tests:
  dsp          - DSP /bid direct (non-matching, throughput only)
  pbs          - PBS→DSP full pipeline (non-matching, throughput only)
  match        - DSP /bid with campaign-matching requests (gets real bids)
  chain        - Full chain: bid → win → imp pixel → click redirect
  multi-imp    - Multi-impression bid requests (2-6 imps per request)
  mixed-media  - Banner + video + native mixed requests
  cookie-sync  - PBS /cookie_sync endpoint
  churn        - Connection churn (new TCP connection per request)
  geo          - Geographic distribution sweep
  all          - Run ALL tests sequentially
"""
import asyncio
import aiohttp
import json
import time
import sys
import random
import re
from collections import defaultdict
from urllib.parse import urlparse, parse_qs, unquote

DSP_URL = "https://dsp.nexflo.ai/bid"
PBS_URL = "https://pbs.nexflo.ai/openrtb2/auction"
PBS_COOKIE_SYNC = "https://pbs.nexflo.ai/cookie_sync"

# Regions that match missions 1001/1002
MATCHING_REGIONS = ["NY", "CA"]
NON_MATCHING_REGIONS = ["WA", "OR", "TX", "FL", "IL", "OH", "GA", "NC"]

# IAB categories
MATCHING_CATS = ["IAB1"]
ALL_CATS = ["IAB1", "IAB2", "IAB3", "IAB4", "IAB5", "IAB6", "IAB7",
            "IAB8", "IAB9", "IAB10", "IAB11", "IAB12", "IAB13"]

# Banner sizes known to have creatives in the DSP
COMMON_SIZES = [(300, 250), (728, 90), (320, 50), (160, 600)]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/119.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_2) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
    "Mozilla/5.0 (iPad; CPU OS 17_2 like Mac OS X) AppleWebKit/605.1.15 Safari/604.1",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
]

DOMAINS = [
    "techcrunch.example.com", "nytimes.example.com", "reddit.example.com",
    "medium.example.com", "bbc.example.com", "cnn.example.com",
    "weather.example.com", "recipes.example.com", "sports.example.com",
    "finance.example.com", "health.example.com", "travel.example.com",
]

# ─── Request builders ───────────────────────────────────────────────────

def random_ip():
    return f"{random.randint(1,223)}.{random.randint(0,255)}.{random.randint(0,255)}.{random.randint(1,254)}"

def make_matching_request(i):
    """Bid request that matches mission targeting (region=NY/CA, cat=IAB1, standard sizes)"""
    sizes = random.sample(COMMON_SIZES, k=random.randint(1, 2))
    return json.dumps({
        "id": f"match-{i}-{random.randint(100000,999999)}",
        "imp": [{
            "id": str(j + 1),
            "banner": {"format": [{"w": w, "h": h}]},
            "bidfloor": round(random.uniform(0.10, 1.50), 2)
        } for j, (w, h) in enumerate(sizes)],
        "site": {
            "cat": ["IAB1"],
            "domain": random.choice(DOMAINS),
            "page": f"https://{random.choice(DOMAINS)}/article/{random.randint(1, 50000)}"
        },
        "device": {
            "ua": random.choice(USER_AGENTS),
            "ip": random_ip(),
            "geo": {"region": random.choice(MATCHING_REGIONS)}
        },
        "user": {"id": f"match-user-{random.randint(1, 100000)}"},
        "tmax": 500
    })

def make_multi_imp_request(i):
    """2-6 impressions per request, mixed sizes"""
    num_imps = random.randint(2, 6)
    imps = []
    for j in range(num_imps):
        w, h = random.choice(COMMON_SIZES)
        imps.append({
            "id": str(j + 1),
            "banner": {"format": [{"w": w, "h": h}]},
            "bidfloor": round(random.uniform(0.05, 2.00), 2)
        })
    return json.dumps({
        "id": f"multi-{i}-{random.randint(100000,999999)}",
        "imp": imps,
        "site": {
            "cat": ["IAB1"],
            "domain": random.choice(DOMAINS),
            "page": f"https://{random.choice(DOMAINS)}/page/{random.randint(1,10000)}"
        },
        "device": {
            "ua": random.choice(USER_AGENTS),
            "ip": random_ip(),
            "geo": {"region": random.choice(MATCHING_REGIONS)}
        },
        "user": {"id": f"multi-user-{random.randint(1, 100000)}"},
        "tmax": 500
    })

def make_mixed_media_request(i):
    """Banner + video + native in same request"""
    imps = [
        {
            "id": "1",
            "banner": {"format": [{"w": 300, "h": 250}, {"w": 728, "h": 90}]},
            "bidfloor": round(random.uniform(0.10, 1.0), 2)
        },
        {
            "id": "2",
            "video": {
                "mimes": ["video/mp4", "video/webm"],
                "protocols": [2, 5],
                "w": 640, "h": 480,
                "minduration": 5, "maxduration": 30,
                "linearity": 1,
                "api": [1, 2]
            },
            "bidfloor": round(random.uniform(1.0, 5.0), 2)
        },
        {
            "id": "3",
            "native": {
                "request": json.dumps({
                    "ver": "1.1",
                    "assets": [
                        {"id": 1, "required": 1, "title": {"len": 90}},
                        {"id": 2, "required": 1, "img": {"type": 3, "w": 300, "h": 250}},
                        {"id": 3, "required": 0, "data": {"type": 2, "len": 150}}
                    ]
                }),
                "ver": "1.1"
            },
            "bidfloor": round(random.uniform(0.50, 3.0), 2)
        }
    ]
    return json.dumps({
        "id": f"mixed-{i}-{random.randint(100000,999999)}",
        "imp": imps,
        "site": {
            "cat": [random.choice(ALL_CATS)],
            "domain": random.choice(DOMAINS),
            "page": f"https://{random.choice(DOMAINS)}/content/{random.randint(1,5000)}"
        },
        "device": {
            "ua": random.choice(USER_AGENTS),
            "ip": random_ip(),
            "geo": {"region": random.choice(MATCHING_REGIONS + NON_MATCHING_REGIONS)}
        },
        "user": {"id": f"mixed-user-{random.randint(1, 100000)}"},
        "tmax": 500
    })

def make_geo_request(i, region):
    """Specific geo-targeted request"""
    return json.dumps({
        "id": f"geo-{region}-{i}",
        "imp": [{
            "id": "1",
            "banner": {"format": [{"w": 300, "h": 250}]},
            "bidfloor": 0.10
        }],
        "site": {"cat": ["IAB1"], "domain": "geo-test.example.com"},
        "device": {
            "ua": random.choice(USER_AGENTS),
            "ip": random_ip(),
            "geo": {"region": region}
        },
        "user": {"id": f"geo-{region}-user-{i}"},
        "tmax": 500
    })

def make_dsp_request(i):
    """Non-matching throughput-only request"""
    return json.dumps({
        "id": f"s-{i}-{random.randint(100000,999999)}",
        "imp": [{"id": "i1", "banner": {"w": 300, "h": 250, "format": [{"w": 300, "h": 250}]}, "bidfloor": 0.01}],
        "site": {"page": f"https://site{i % 1000}.example.com/p", "domain": f"site{i % 1000}.example.com", "publisher": {"id": f"pub_{i % 500}"}},
        "device": {"ua": "Mozilla/5.0", "ip": random_ip(), "geo": {"country": "USA"}},
        "user": {"id": f"u-{random.randint(1, 100000)}"}, "tmax": 300
    })

def make_pbs_request(i):
    """PBS routed request"""
    return json.dumps({
        "id": f"p-{i}-{random.randint(100000,999999)}",
        "imp": [{"id": "i1", "banner": {"w": 300, "h": 250}, "bidfloor": 0.05,
                  "ext": {"prebid": {"bidder": {"nexflo": {}}}}}],
        "site": {"page": f"https://blog{i % 1000}.example.com/post", "domain": f"blog{i % 1000}.example.com",
                 "publisher": {"id": f"pub_{i % 500}"}},
        "device": {"ua": "Mozilla/5.0", "ip": random_ip()},
        "user": {"id": f"u-{random.randint(1, 50000)}"}, "tmax": 2000
    })

# ─── Stats ───────────────────────────────────────────────────────────────

class Stats:
    def __init__(self):
        self.codes = defaultdict(int)
        self.latencies = []
        self.success = 0
        self.errors = 0
        self.bids_received = 0
        self.wins_fired = 0
        self.imps_fired = 0
        self.clicks_fired = 0
        self.chain_details = []
        self.lock = asyncio.Lock()

# ─── Workers ─────────────────────────────────────────────────────────────

async def basic_worker(session, url, queue, stats, make_req):
    """Simple fire-and-forget bid worker"""
    while True:
        try:
            i = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        body = make_req(i)
        start = time.monotonic()
        try:
            async with session.post(url, data=body, headers={"Content-Type": "application/json"},
                                     timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.read()
                elapsed = time.monotonic() - start
                async with stats.lock:
                    stats.codes[resp.status] += 1
                    stats.latencies.append(elapsed)
                    stats.success += 1
                    if resp.status == 200:
                        try:
                            j = json.loads(data)
                            if j.get("seatbid"):
                                for sb in j["seatbid"]:
                                    stats.bids_received += len(sb.get("bid", []))
                        except Exception:
                            pass
        except asyncio.TimeoutError:
            async with stats.lock:
                stats.codes["timeout"] += 1
                stats.latencies.append(time.monotonic() - start)
                stats.errors += 1
        except Exception as e:
            async with stats.lock:
                stats.codes[f"err:{type(e).__name__}"] += 1
                stats.latencies.append(time.monotonic() - start)
                stats.errors += 1
        queue.task_done()

async def chain_worker(session, queue, stats):
    """Full chain: bid → parse response → fire win nurl → fire imp pixel → fire click"""
    while True:
        try:
            i = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        body = make_matching_request(i)
        chain = {"bid": None, "win": None, "imp": None, "click": None}
        start = time.monotonic()
        try:
            # Step 1: Send bid request
            async with session.post(DSP_URL, data=body, headers={"Content-Type": "application/json"},
                                     timeout=aiohttp.ClientTimeout(total=5)) as resp:
                resp_data = await resp.read()
                elapsed = time.monotonic() - start
                async with stats.lock:
                    stats.codes[resp.status] += 1
                    stats.latencies.append(elapsed)
                    stats.success += 1

                if resp.status != 200:
                    queue.task_done()
                    continue

                bid_resp = json.loads(resp_data)
                if not bid_resp.get("seatbid"):
                    queue.task_done()
                    continue

                bid = bid_resp["seatbid"][0]["bid"][0]
                async with stats.lock:
                    stats.bids_received += 1
                chain["bid"] = bid["price"]

                # Step 2: Fire win notification (nurl)
                nurl = bid.get("nurl", "")
                if nurl:
                    # Replace ${AUCTION_PRICE} macro with the bid price
                    win_url = nurl.replace("${AUCTION_PRICE}", str(bid["price"]))
                    try:
                        async with session.get(win_url, timeout=aiohttp.ClientTimeout(total=5),
                                               allow_redirects=False) as win_resp:
                            await win_resp.read()
                            chain["win"] = win_resp.status
                            async with stats.lock:
                                stats.wins_fired += 1
                                stats.codes[f"win:{win_resp.status}"] += 1
                    except Exception as e:
                        chain["win"] = f"err:{type(e).__name__}"

                # Step 3: Fire impression pixel from adm
                adm = bid.get("adm", "")
                imp_match = re.search(r'src="(https://dsp\.nexflo\.ai/imp\?[^"]+)"', adm)
                if imp_match:
                    imp_url = imp_match.group(1).replace("&amp;", "&")
                    try:
                        async with session.get(imp_url, timeout=aiohttp.ClientTimeout(total=5)) as imp_resp:
                            await imp_resp.read()
                            chain["imp"] = imp_resp.status
                            async with stats.lock:
                                stats.imps_fired += 1
                                stats.codes[f"imp:{imp_resp.status}"] += 1
                    except Exception as e:
                        chain["imp"] = f"err:{type(e).__name__}"

                # Step 4: Fire click redirect
                click_match = re.search(r'href="(https://dsp\.nexflo\.ai/click\?[^"]+)"', adm)
                if click_match:
                    click_url = click_match.group(1).replace("&amp;", "&")
                    try:
                        async with session.get(click_url, timeout=aiohttp.ClientTimeout(total=5),
                                               allow_redirects=False) as click_resp:
                            await click_resp.read()
                            chain["click"] = click_resp.status
                            async with stats.lock:
                                stats.clicks_fired += 1
                                stats.codes[f"click:{click_resp.status}"] += 1
                    except Exception as e:
                        chain["click"] = f"err:{type(e).__name__}"

                async with stats.lock:
                    stats.chain_details.append(chain)

        except asyncio.TimeoutError:
            async with stats.lock:
                stats.codes["timeout"] += 1
                stats.latencies.append(time.monotonic() - start)
                stats.errors += 1
        except Exception as e:
            async with stats.lock:
                stats.codes[f"err:{type(e).__name__}"] += 1
                stats.latencies.append(time.monotonic() - start)
                stats.errors += 1
        queue.task_done()

async def churn_worker(url, queue, stats, make_req):
    """New TCP connection per request — simulates browser connection churn"""
    while True:
        try:
            i = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        body = make_req(i)
        start = time.monotonic()
        try:
            conn = aiohttp.TCPConnector(limit=1, force_close=True)
            async with aiohttp.ClientSession(connector=conn) as session:
                async with session.post(url, data=body, headers={"Content-Type": "application/json"},
                                         timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    await resp.read()
                    elapsed = time.monotonic() - start
                    async with stats.lock:
                        stats.codes[resp.status] += 1
                        stats.latencies.append(elapsed)
                        stats.success += 1
        except asyncio.TimeoutError:
            async with stats.lock:
                stats.codes["timeout"] += 1
                stats.latencies.append(time.monotonic() - start)
                stats.errors += 1
        except Exception as e:
            async with stats.lock:
                stats.codes[f"err:{type(e).__name__}"] += 1
                stats.latencies.append(time.monotonic() - start)
                stats.errors += 1
        queue.task_done()

# ─── Test runners ────────────────────────────────────────────────────────

async def run_basic_test(name, url, make_req, total, concurrency, worker_fn=None):
    stats = Stats()
    connector = aiohttp.TCPConnector(limit=concurrency, limit_per_host=concurrency, ttl_dns_cache=300)

    print(f"\n{'='*65}")
    print(f"  {name}")
    print(f"  Target:      {url}")
    print(f"  Requests:    {total:,}")
    print(f"  Concurrency: {concurrency}")
    print(f"{'='*65}\n")

    async with aiohttp.ClientSession(connector=connector) as session:
        # Warmup
        print("  Warming up...")
        wq = asyncio.Queue()
        ws = Stats()
        for i in range(min(20, total)):
            wq.put_nowait(i)
        ww = [asyncio.create_task(basic_worker(session, url, wq, ws, make_req)) for _ in range(20)]
        await asyncio.gather(*ww)
        print(f"  Warmup: {ws.success} ok, {ws.errors} err, {ws.bids_received} bids\n")

        queue = asyncio.Queue()
        for i in range(total):
            queue.put_nowait(i)

        async def progress():
            last = 0
            t0 = time.monotonic()
            while True:
                await asyncio.sleep(2)
                async with stats.lock:
                    done = stats.success + stats.errors
                    ok, err, bids = stats.success, stats.errors, stats.bids_received
                elapsed = time.monotonic() - t0
                qps = done / elapsed if elapsed > 0 else 0
                iqps = (done - last) / 2
                last = done
                bid_str = f"  bids:{bids}" if bids else ""
                print(f"  [{done:>7,}/{total:,}]  {iqps:,.0f} rps (avg {qps:,.0f}){bid_str}  |  {ok} ok  {err} err")
                if done >= total:
                    break

        reporter = asyncio.create_task(progress())
        wall_start = time.monotonic()

        if worker_fn:
            workers = [asyncio.create_task(worker_fn(session, url, queue, stats, make_req)) for _ in range(concurrency)]
        else:
            workers = [asyncio.create_task(basic_worker(session, url, queue, stats, make_req)) for _ in range(concurrency)]
        await asyncio.gather(*workers)
        wall_elapsed = time.monotonic() - wall_start
        reporter.cancel()
        try: await reporter
        except asyncio.CancelledError: pass

    print_results(stats, total, wall_elapsed)
    return stats

async def run_chain_test(total, concurrency):
    stats = Stats()
    connector = aiohttp.TCPConnector(limit=concurrency, limit_per_host=concurrency, ttl_dns_cache=300)

    print(f"\n{'='*65}")
    print(f"  FULL CHAIN: bid → win → imp → click")
    print(f"  Target:      {DSP_URL}")
    print(f"  Requests:    {total:,}")
    print(f"  Concurrency: {concurrency}")
    print(f"{'='*65}\n")

    async with aiohttp.ClientSession(connector=connector) as session:
        queue = asyncio.Queue()
        for i in range(total):
            queue.put_nowait(i)

        async def progress():
            last = 0
            t0 = time.monotonic()
            while True:
                await asyncio.sleep(2)
                async with stats.lock:
                    done = stats.success + stats.errors
                    bids, wins, imps, clicks = stats.bids_received, stats.wins_fired, stats.imps_fired, stats.clicks_fired
                elapsed = time.monotonic() - t0
                qps = done / elapsed if elapsed > 0 else 0
                print(f"  [{done:>6,}/{total:,}]  {qps:,.0f} rps  |  bids:{bids} wins:{wins} imps:{imps} clicks:{clicks}")
                if done >= total:
                    break

        reporter = asyncio.create_task(progress())
        wall_start = time.monotonic()

        workers = [asyncio.create_task(chain_worker(session, queue, stats)) for _ in range(concurrency)]
        await asyncio.gather(*workers)
        wall_elapsed = time.monotonic() - wall_start
        reporter.cancel()
        try: await reporter
        except asyncio.CancelledError: pass

    print_results(stats, total, wall_elapsed)

    # Chain details
    if stats.chain_details:
        print(f"\n  Chain Summary ({len(stats.chain_details)} complete chains):")
        print(f"    Bids received: {stats.bids_received}")
        print(f"    Wins fired:    {stats.wins_fired}")
        print(f"    Imps fired:    {stats.imps_fired}")
        print(f"    Clicks fired:  {stats.clicks_fired}")
        # Show first few chains
        for j, c in enumerate(stats.chain_details[:5]):
            print(f"    Chain {j+1}: bid=${c['bid']}  win={c['win']}  imp={c['imp']}  click={c['click']}")
    print()
    return stats

async def run_churn_test(total, concurrency):
    stats = Stats()

    print(f"\n{'='*65}")
    print(f"  CONNECTION CHURN (new TCP per request)")
    print(f"  Target:      {DSP_URL}")
    print(f"  Requests:    {total:,}")
    print(f"  Concurrency: {concurrency}")
    print(f"{'='*65}\n")

    queue = asyncio.Queue()
    for i in range(total):
        queue.put_nowait(i)

    async def progress():
        last = 0
        t0 = time.monotonic()
        while True:
            await asyncio.sleep(2)
            async with stats.lock:
                done = stats.success + stats.errors
            elapsed = time.monotonic() - t0
            qps = done / elapsed if elapsed > 0 else 0
            print(f"  [{done:>6,}/{total:,}]  {qps:,.0f} rps  |  {stats.success} ok  {stats.errors} err")
            if done >= total:
                break

    reporter = asyncio.create_task(progress())
    wall_start = time.monotonic()

    workers = [asyncio.create_task(churn_worker(DSP_URL, queue, stats, make_matching_request)) for _ in range(concurrency)]
    await asyncio.gather(*workers)
    wall_elapsed = time.monotonic() - wall_start
    reporter.cancel()
    try: await reporter
    except asyncio.CancelledError: pass

    print_results(stats, total, wall_elapsed)
    return stats

async def run_cookie_sync_test(total, concurrency):
    stats = Stats()
    connector = aiohttp.TCPConnector(limit=concurrency, limit_per_host=concurrency, ttl_dns_cache=300)

    print(f"\n{'='*65}")
    print(f"  PBS COOKIE_SYNC")
    print(f"  Target:      {PBS_COOKIE_SYNC}")
    print(f"  Requests:    {total:,}")
    print(f"  Concurrency: {concurrency}")
    print(f"{'='*65}\n")

    async with aiohttp.ClientSession(connector=connector) as session:
        queue = asyncio.Queue()
        for i in range(total):
            queue.put_nowait(i)

        async def cookie_worker():
            while True:
                try:
                    i = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                body = json.dumps({
                    "bidders": ["nexflo"],
                    "gdpr": 0,
                    "us_privacy": "",
                    "coopSync": True
                })
                start = time.monotonic()
                try:
                    async with session.post(PBS_COOKIE_SYNC, data=body,
                                            headers={"Content-Type": "application/json"},
                                            timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        await resp.read()
                        elapsed = time.monotonic() - start
                        async with stats.lock:
                            stats.codes[resp.status] += 1
                            stats.latencies.append(elapsed)
                            stats.success += 1
                except asyncio.TimeoutError:
                    async with stats.lock:
                        stats.codes["timeout"] += 1
                        stats.latencies.append(time.monotonic() - start)
                        stats.errors += 1
                except Exception as e:
                    async with stats.lock:
                        stats.codes[f"err:{type(e).__name__}"] += 1
                        stats.latencies.append(time.monotonic() - start)
                        stats.errors += 1
                queue.task_done()

        wall_start = time.monotonic()
        workers = [asyncio.create_task(cookie_worker()) for _ in range(concurrency)]
        await asyncio.gather(*workers)
        wall_elapsed = time.monotonic() - wall_start

    print_results(stats, total, wall_elapsed)
    return stats

async def run_geo_test(total_per_region, concurrency):
    """Test every US region to see which ones get bids"""
    ALL_REGIONS = [
        "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
        "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
        "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
        "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
        "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    ]
    total = total_per_region * len(ALL_REGIONS)
    stats = Stats()
    geo_bids = defaultdict(int)
    geo_nobids = defaultdict(int)
    connector = aiohttp.TCPConnector(limit=concurrency, limit_per_host=concurrency, ttl_dns_cache=300)

    print(f"\n{'='*65}")
    print(f"  GEO DISTRIBUTION SWEEP — {len(ALL_REGIONS)} regions x {total_per_region}")
    print(f"  Target:      {DSP_URL}")
    print(f"  Total:       {total:,}")
    print(f"  Concurrency: {concurrency}")
    print(f"{'='*65}\n")

    async with aiohttp.ClientSession(connector=connector) as session:
        queue = asyncio.Queue()
        region_map = {}
        idx = 0
        for region in ALL_REGIONS:
            for j in range(total_per_region):
                queue.put_nowait(idx)
                region_map[idx] = region
                idx += 1

        async def geo_worker():
            while True:
                try:
                    i = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                region = region_map[i]
                body = make_geo_request(i, region)
                start = time.monotonic()
                try:
                    async with session.post(DSP_URL, data=body,
                                            headers={"Content-Type": "application/json"},
                                            timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        data = await resp.read()
                        elapsed = time.monotonic() - start
                        async with stats.lock:
                            stats.codes[resp.status] += 1
                            stats.latencies.append(elapsed)
                            stats.success += 1
                            if resp.status == 200:
                                try:
                                    j = json.loads(data)
                                    if j.get("seatbid"):
                                        geo_bids[region] += 1
                                        stats.bids_received += 1
                                    else:
                                        geo_nobids[region] += 1
                                except Exception:
                                    geo_nobids[region] += 1
                            else:
                                geo_nobids[region] += 1
                except asyncio.TimeoutError:
                    async with stats.lock:
                        stats.codes["timeout"] += 1
                        stats.latencies.append(time.monotonic() - start)
                        stats.errors += 1
                except Exception as e:
                    async with stats.lock:
                        stats.codes[f"err:{type(e).__name__}"] += 1
                        stats.latencies.append(time.monotonic() - start)
                        stats.errors += 1
                queue.task_done()

        wall_start = time.monotonic()
        workers = [asyncio.create_task(geo_worker()) for _ in range(concurrency)]
        await asyncio.gather(*workers)
        wall_elapsed = time.monotonic() - wall_start

    print_results(stats, total, wall_elapsed)

    # Geo heatmap
    print(f"\n  Geo Results ({stats.bids_received} bids across {len(geo_bids)} regions):")
    print(f"  {'Region':<8} {'Bids':>6} {'NoBid':>6} {'Rate':>8}")
    print(f"  {'-'*30}")
    for region in ALL_REGIONS:
        b = geo_bids.get(region, 0)
        nb = geo_nobids.get(region, 0)
        total_r = b + nb
        rate = f"{b/total_r*100:.0f}%" if total_r > 0 else "-"
        marker = " <<<" if b > 0 else ""
        print(f"  {region:<8} {b:>6} {nb:>6} {rate:>8}{marker}")
    print()
    return stats

# ─── Report ──────────────────────────────────────────────────────────────

def print_results(stats, total, wall_elapsed):
    lats = sorted(stats.latencies)
    if not lats:
        print("\n  No results!")
        return

    print(f"\n  {'-'*55}")
    print(f"  Wall time:    {wall_elapsed:.2f}s")
    print(f"  Throughput:   {total / wall_elapsed:,.0f} req/s")
    print(f"  Success:      {stats.success:,}")
    print(f"  Errors:       {stats.errors:,}")
    print(f"  Error rate:   {stats.errors/max(total,1)*100:.2f}%")
    if stats.bids_received:
        print(f"  Bids recv'd:  {stats.bids_received:,}")
    print(f"\n  Latency (ms):")
    print(f"    Min:    {lats[0]*1000:>8.1f}")
    print(f"    p50:    {lats[int(len(lats)*0.50)]*1000:>8.1f}")
    print(f"    p90:    {lats[int(len(lats)*0.90)]*1000:>8.1f}")
    print(f"    p95:    {lats[int(len(lats)*0.95)]*1000:>8.1f}")
    print(f"    p99:    {lats[int(len(lats)*0.99)]*1000:>8.1f}")
    print(f"    Max:    {lats[-1]*1000:>8.1f}")
    print(f"\n  Status codes:")
    for code, count in sorted(stats.codes.items(), key=lambda x: -x[1]):
        print(f"    {code}: {count:,}")
    print(f"  {'-'*55}")

# ─── Main ────────────────────────────────────────────────────────────────

async def run_all(total, concurrency):
    print(f"\n{'#'*65}")
    print(f"  NEXFLO COMPREHENSIVE STRESS TEST SUITE")
    print(f"  Base load: {total:,} requests @ {concurrency} concurrency")
    print(f"{'#'*65}")

    suite_start = time.monotonic()
    results = {}

    # 1. DSP direct throughput
    results["dsp"] = await run_basic_test(
        "TEST 1: DSP DIRECT (throughput)", DSP_URL, make_dsp_request, total, concurrency)

    # 2. PBS→DSP pipeline
    results["pbs"] = await run_basic_test(
        "TEST 2: PBS→DSP PIPELINE", PBS_URL, make_pbs_request, total, min(concurrency, 500))

    # 3. Campaign-matching bids
    results["match"] = await run_basic_test(
        "TEST 3: CAMPAIGN-MATCHING BIDS", DSP_URL, make_matching_request, total, concurrency)

    # 4. Full chain
    results["chain"] = await run_chain_test(min(total, 2000), min(concurrency, 200))

    # 5. Multi-impression
    results["multi"] = await run_basic_test(
        "TEST 5: MULTI-IMPRESSION (2-6 imps/req)", DSP_URL, make_multi_imp_request, total, concurrency)

    # 6. Mixed media
    results["mixed"] = await run_basic_test(
        "TEST 6: MIXED MEDIA (banner+video+native)", DSP_URL, make_mixed_media_request, total, concurrency)

    # 7. Cookie sync
    results["cookie"] = await run_cookie_sync_test(min(total, 2000), min(concurrency, 200))

    # 8. Connection churn
    results["churn"] = await run_churn_test(min(total, 1000), min(concurrency, 100))

    # 9. Geo sweep
    results["geo"] = await run_geo_test(20, min(concurrency, 300))

    suite_elapsed = time.monotonic() - suite_start

    # Final summary
    total_reqs = sum(s.success + s.errors for s in results.values())
    total_errs = sum(s.errors for s in results.values())
    total_bids = sum(s.bids_received for s in results.values())

    print(f"\n{'#'*65}")
    print(f"  SUITE COMPLETE")
    print(f"  Total time:     {suite_elapsed:.1f}s")
    print(f"  Total requests: {total_reqs:,}")
    print(f"  Total errors:   {total_errs:,} ({total_errs/max(total_reqs,1)*100:.2f}%)")
    print(f"  Total bids:     {total_bids:,}")
    print(f"{'#'*65}\n")

async def main():
    test = sys.argv[1] if len(sys.argv) > 1 else "all"
    total = int(sys.argv[2]) if len(sys.argv) > 2 else 5000
    concurrency = int(sys.argv[3]) if len(sys.argv) > 3 else 500

    if test == "all":
        await run_all(total, concurrency)
    elif test == "dsp":
        await run_basic_test("DSP DIRECT", DSP_URL, make_dsp_request, total, concurrency)
    elif test == "pbs":
        await run_basic_test("PBS→DSP PIPELINE", PBS_URL, make_pbs_request, total, concurrency)
    elif test == "match":
        await run_basic_test("CAMPAIGN-MATCHING BIDS", DSP_URL, make_matching_request, total, concurrency)
    elif test == "chain":
        await run_chain_test(total, concurrency)
    elif test == "multi-imp":
        await run_basic_test("MULTI-IMPRESSION", DSP_URL, make_multi_imp_request, total, concurrency)
    elif test == "mixed-media":
        await run_basic_test("MIXED MEDIA", DSP_URL, make_mixed_media_request, total, concurrency)
    elif test == "cookie-sync":
        await run_cookie_sync_test(total, concurrency)
    elif test == "churn":
        await run_churn_test(total, concurrency)
    elif test == "geo":
        await run_geo_test(int(sys.argv[2]) if len(sys.argv) > 2 else 20, concurrency)
    else:
        print(f"Unknown test: {test}")
        print(__doc__)
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
