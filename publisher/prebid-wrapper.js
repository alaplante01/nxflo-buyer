/**
 * Nexflo Prebid Wrapper — Zero-config header bidding for publishers
 * Drop this script on any page to monetize with header bidding via pbs.nexflo.ai
 *
 * Usage: <script src="https://cdn.nexflo.ai/prebid-wrapper.js" data-site-id="YOUR_SITE_ID"></script>
 *
 * Optional attributes:
 *   data-site-id       — Publisher site ID (required)
 *   data-ad-sizes      — Comma-separated sizes, e.g. "728x90,300x250" (auto-detected if omitted)
 *   data-auto-place    — "true" (default) to auto-inject ad slots, "false" to use existing divs
 *   data-floor         — CPM floor price in USD, e.g. "0.50" (default: no floor)
 *   data-timeout       — Bid timeout in ms (default: 1500)
 */
(function () {
  "use strict";

  // --- Config from script tag ---
  var scriptTag = document.currentScript || document.querySelector('script[data-site-id]');
  if (!scriptTag) {
    console.error("[Nexflo] Missing script tag with data-site-id");
    return;
  }

  var CONFIG = {
    siteId: scriptTag.getAttribute("data-site-id"),
    adSizes: parseAdSizes(scriptTag.getAttribute("data-ad-sizes")),
    autoPlace: scriptTag.getAttribute("data-auto-place") !== "false",
    floor: parseFloat(scriptTag.getAttribute("data-floor")) || 0,
    timeout: parseInt(scriptTag.getAttribute("data-timeout"), 10) || 1500,
    pbsEndpoint: "https://pbs.nexflo.ai/openrtb2/auction",
    prebidCdn: "https://cdn.nexflo.ai/vendor/prebid.js",
  };

  if (!CONFIG.siteId) {
    console.error("[Nexflo] data-site-id is required");
    return;
  }

  // --- Standard IAB sizes by placement ---
  var PLACEMENTS = {
    leaderboard: { sizes: [[728, 90], [970, 90]], position: "top", mobile: [[320, 50], [320, 100]] },
    rectangle:   { sizes: [[300, 250], [336, 280]], position: "content", mobile: [[300, 250]] },
    sidebar:     { sizes: [[300, 250], [160, 600]], position: "sidebar", mobile: [] },
  };

  // --- Helpers ---
  function parseAdSizes(attr) {
    if (!attr) return null;
    return attr.split(",").map(function (s) {
      var parts = s.trim().split("x");
      return [parseInt(parts[0], 10), parseInt(parts[1], 10)];
    });
  }

  function isMobile() {
    return window.innerWidth < 768;
  }

  function generateSlotId(index) {
    return "nexflo-ad-" + index;
  }

  // --- Ad slot detection / creation ---
  function detectOrCreateSlots() {
    // Check for existing nexflo ad divs
    var existing = document.querySelectorAll('[data-nexflo-ad]');
    if (existing.length > 0) {
      return Array.prototype.map.call(existing, function (el, i) {
        var sizesAttr = el.getAttribute("data-nexflo-sizes");
        var sizes = sizesAttr ? parseAdSizes(sizesAttr) : getDefaultSizes("rectangle");
        if (!el.id) el.id = generateSlotId(i);
        return { elementId: el.id, sizes: sizes };
      });
    }

    if (!CONFIG.autoPlace) return [];

    var slots = [];
    var mobile = isMobile();

    // Top leaderboard — before main content
    var topTarget = document.querySelector("main, article, .content, .entry-content, #content, .site-content")
      || document.querySelector("body > div:first-child");
    if (topTarget) {
      var leaderSizes = mobile ? PLACEMENTS.leaderboard.mobile : PLACEMENTS.leaderboard.sizes;
      if (leaderSizes.length > 0) {
        var topSlot = createAdDiv(generateSlotId(slots.length), leaderSizes);
        topTarget.parentNode.insertBefore(topSlot, topTarget);
        slots.push({ elementId: topSlot.id, sizes: leaderSizes });
      }
    }

    // In-content rectangle — after a few paragraphs
    var paragraphs = document.querySelectorAll("article p, .entry-content p, main p, .content p");
    if (paragraphs.length >= 3) {
      var rectSizes = mobile ? PLACEMENTS.rectangle.mobile : PLACEMENTS.rectangle.sizes;
      var afterP = paragraphs[Math.min(2, paragraphs.length - 1)];
      var rectSlot = createAdDiv(generateSlotId(slots.length), rectSizes);
      afterP.parentNode.insertBefore(rectSlot, afterP.nextSibling);
      slots.push({ elementId: rectSlot.id, sizes: rectSizes });
    }

    // Sidebar rectangle
    if (!mobile) {
      var sidebar = document.querySelector("aside, .sidebar, #sidebar, .widget-area");
      if (sidebar) {
        var sideSlot = createAdDiv(generateSlotId(slots.length), PLACEMENTS.sidebar.sizes);
        sidebar.insertBefore(sideSlot, sidebar.firstChild);
        slots.push({ elementId: sideSlot.id, sizes: PLACEMENTS.sidebar.sizes });
      }
    }

    // Fallback — if nothing found, add a rectangle before </body>
    if (slots.length === 0) {
      var fallbackSizes = mobile ? [[320, 50]] : [[728, 90]];
      var fallbackSlot = createAdDiv(generateSlotId(0), fallbackSizes);
      document.body.appendChild(fallbackSlot);
      slots.push({ elementId: fallbackSlot.id, sizes: fallbackSizes });
    }

    return slots;
  }

  function createAdDiv(id, sizes) {
    var div = document.createElement("div");
    div.id = id;
    div.className = "nexflo-ad-slot";
    div.style.cssText =
      "text-align:center;margin:16px auto;min-height:" +
      sizes[0][1] + "px;max-width:" + sizes[0][0] + "px;overflow:hidden;";
    return div;
  }

  function getDefaultSizes(placement) {
    var p = PLACEMENTS[placement] || PLACEMENTS.rectangle;
    return isMobile() ? p.mobile : p.sizes;
  }

  // --- Load Prebid.js and run auction ---
  function loadPrebid(callback) {
    // Check if already loaded
    if (window.pbjs) {
      callback();
      return;
    }

    var script = document.createElement("script");
    script.async = true;
    script.src = CONFIG.prebidCdn;
    script.onload = callback;
    script.onerror = function () {
      console.error("[Nexflo] Failed to load Prebid.js");
    };
    document.head.appendChild(script);
  }

  function runAuction(slots) {
    window.pbjs = window.pbjs || {};
    window.pbjs.que = window.pbjs.que || [];

    window.pbjs.que.push(function () {
      var pbjs = window.pbjs;

      // S2S config pointing to our Prebid Server
      pbjs.setConfig({
        s2sConfig: {
          accountId: CONFIG.siteId,
          bidders: ["nexflo"],
          defaultVendor: "appnexus", // PBS adapter base
          timeout: CONFIG.timeout,
          endpoint: {
            p1Consent: CONFIG.pbsEndpoint,
            noP1Consent: CONFIG.pbsEndpoint,
          },
          syncEndpoint: {
            p1Consent: "https://pbs.nexflo.ai/cookie_sync",
            noP1Consent: "https://pbs.nexflo.ai/cookie_sync",
          },
        },
        bidderTimeout: CONFIG.timeout,
        enableSendAllBids: false,
        useBidCache: true,
        priceGranularity: "dense",
        consentManagement: {
          gdpr: {
            cmpApi: "iab",
            timeout: 1500,
            defaultGdprScope: false,
          },
          usp: {
            cmpApi: "iab",
            timeout: 1000,
          },
        },
      });

      // Build ad units from detected slots
      var adUnits = slots.map(function (slot, i) {
        var unit = {
          code: slot.elementId,
          mediaTypes: {
            banner: { sizes: CONFIG.adSizes || slot.sizes },
          },
          bids: [
            {
              bidder: "nexflo",
              params: {
                publisherId: CONFIG.siteId,
                placementId: slot.elementId,
              },
            },
          ],
        };

        // Apply floor if set
        if (CONFIG.floor > 0) {
          unit.floors = { values: { "banner|*": CONFIG.floor } };
        }

        return unit;
      });

      pbjs.addAdUnits(adUnits);

      pbjs.requestBids({
        timeout: CONFIG.timeout,
        bidsBackHandler: function (bidResponses) {
          slots.forEach(function (slot) {
            var el = document.getElementById(slot.elementId);
            if (!el) return;

            var highestBid = pbjs.getHighestCpmBids(slot.elementId)[0];
            if (highestBid) {
              renderAd(el, highestBid);
              trackImpression(slot, highestBid);
            } else {
              // No bids — collapse the slot
              el.style.display = "none";
            }
          });
        },
      });
    });
  }

  function renderAd(element, bid) {
    var iframe = document.createElement("iframe");
    iframe.id = "nexflo-frame-" + element.id;
    iframe.width = bid.width;
    iframe.height = bid.height;
    iframe.frameBorder = "0";
    iframe.scrolling = "no";
    iframe.style.cssText = "border:0;margin:0 auto;display:block;";
    element.innerHTML = "";
    element.appendChild(iframe);

    var doc = iframe.contentDocument || iframe.contentWindow.document;
    doc.open();
    doc.write(
      "<!DOCTYPE html><html><head><style>body{margin:0;overflow:hidden;}</style></head><body>" +
        bid.ad +
        "</body></html>"
    );
    doc.close();
  }

  function trackImpression(slot, bid) {
    // Impression tracking handled by DSP pixel embedded in ad markup (adm)
    // No additional client-side pixel needed
  }

  // --- Init ---
  function init() {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", run);
    } else {
      run();
    }
  }

  function run() {
    var slots = detectOrCreateSlots();
    if (slots.length === 0) {
      console.warn("[Nexflo] No ad slots detected or created");
      return;
    }

    console.log("[Nexflo] Found " + slots.length + " ad slot(s), loading Prebid.js...");
    loadPrebid(function () {
      console.log("[Nexflo] Running auction for " + slots.length + " slot(s)");
      runAuction(slots);
    });
  }

  init();
})();
