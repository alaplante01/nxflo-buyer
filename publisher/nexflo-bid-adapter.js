/**
 * Nexflo Prebid.js Bid Adapter — S2S only (all bidding via pbs.nexflo.ai)
 *
 * This adapter is intentionally minimal. All auction logic runs server-side
 * on Prebid Server (pbs.nexflo.ai). The client-side adapter only exists so
 * Prebid.js recognizes "nexflo" as a valid bidder and includes it in S2S calls.
 *
 * Build a custom prebid.js bundle that includes this adapter:
 *   git clone https://github.com/prebid/Prebid.js.git
 *   cp nexflo-bid-adapter.js Prebid.js/modules/nexfloBidAdapter.js
 *   cd Prebid.js
 *   npm install
 *   gulp bundle --modules=nexfloBidAdapter,prebidServerBidAdapter
 *   # Output: build/dist/prebid.js → upload to cdn.nexflo.ai/vendor/prebid.js
 */

import { registerBidder } from "../src/adapters/bidderFactory.js";

const BIDDER_CODE = "nexflo";

export const spec = {
  code: BIDDER_CODE,
  supportedMediaTypes: ["banner"],

  /**
   * Validate bid params. publisherId is required (set by prebid-wrapper.js).
   */
  isBidRequestValid(bid) {
    return !!(bid.params && bid.params.publisherId);
  },

  /**
   * No client-side requests — all bidding is server-side via PBS S2S.
   * Prebid.js will route bids to pbs.nexflo.ai via s2sConfig.
   */
  buildRequests() {
    return [];
  },

  /**
   * No client-side responses to interpret.
   */
  interpretResponse() {
    return [];
  },

  /**
   * getUserSyncs — redirect to PBS cookie sync
   */
  getUserSyncs(syncOptions, serverResponses, gdprConsent, uspConsent) {
    if (syncOptions.pixelEnabled) {
      let syncUrl = "https://pbs.nexflo.ai/setuid?bidder=nexflo&uid=";
      if (gdprConsent) {
        syncUrl += "&gdpr=" + (gdprConsent.gdprApplies ? 1 : 0);
        syncUrl += "&gdpr_consent=" + encodeURIComponent(gdprConsent.consentString || "");
      }
      return [{ type: "image", url: syncUrl }];
    }
    return [];
  },
};

registerBidder(spec);
