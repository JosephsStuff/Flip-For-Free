"""eBay API client: OAuth sign-in (SSO), token refresh, digital signatures and
the three APIs we read from:

  * Identity API     - who signed in
  * Fulfillment API  - orders / line items (what sold, for how much)
  * Finances API     - every fee, refund, postage label and ad charge

UK / EU sellers must digitally sign Finances API calls (RFC 9421). We create an
Ed25519 signing key once through eBay's Key Management API, store it in the
database, and sign each Finances request with it.
"""
import base64
import json
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse

import requests
from cryptography.hazmat.primitives import serialization

from db import Settings

ENVIRONMENTS = {
    "production": {
        "auth": "https://auth.ebay.com/oauth2/authorize",
        "api": "https://api.ebay.com",
        "apiz": "https://apiz.ebay.com",
    },
    "sandbox": {
        "auth": "https://auth.sandbox.ebay.com/oauth2/authorize",
        "api": "https://api.sandbox.ebay.com",
        "apiz": "https://apiz.sandbox.ebay.com",
    },
}

USER_SCOPES = [
    "https://api.ebay.com/oauth/api_scope",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly",
    "https://api.ebay.com/oauth/api_scope/sell.finances",
    "https://api.ebay.com/oauth/api_scope/commerce.identity.readonly",
    "https://api.ebay.com/oauth/api_scope/sell.marketing",
]
# What sign-ins made before automations existed were granted.
LEGACY_SCOPES = USER_SCOPES[:4]
MARKETING_SCOPE = "https://api.ebay.com/oauth/api_scope/sell.marketing"

TRADING_NS = "urn:ebay:apis:eBLBaseComponents"
TRADING_COMPAT = "1349"
SITE_IDS = {"EBAY_GB": "3", "EBAY_US": "0", "EBAY_DE": "77", "EBAY_IE": "205", "EBAY_AU": "15"}
APP_SCOPES = ["https://api.ebay.com/oauth/api_scope"]

TIMEOUT = 60


class EbayError(Exception):
    def __init__(self, message, status=None, body=None):
        super().__init__(message)
        self.status = status
        self.body = body


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class EbayClient:
    # access tokens are cached in memory only (they last ~2 hours)
    _user_token = {"value": None, "expires": 0}
    _app_token = {"value": None, "expires": 0}

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        s = self.settings.get_many(
            ["ebay_env", "client_id", "client_secret", "ru_name", "marketplace_id"]
        )
        self.env_name = s.get("ebay_env") or "production"
        self.env = ENVIRONMENTS[self.env_name]
        self.client_id = s.get("client_id") or ""
        self.client_secret = s.get("client_secret") or ""
        self.ru_name = s.get("ru_name") or ""
        self.marketplace = s.get("marketplace_id") or "EBAY_GB"

    # ------------------------------------------------------------------ #
    # OAuth
    # ------------------------------------------------------------------ #
    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.ru_name)

    def authorize_url(self, state: str) -> str:
        q = {
            "client_id": self.client_id,
            "redirect_uri": self.ru_name,
            "response_type": "code",
            "scope": " ".join(USER_SCOPES),
            "state": state,
            "prompt": "login",
        }
        return f"{self.env['auth']}?{urlencode(q)}"

    def _basic(self) -> str:
        raw = f"{self.client_id}:{self.client_secret}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def _token_request(self, data: dict) -> dict:
        r = requests.post(
            f"{self.env['api']}/identity/v1/oauth2/token",
            headers={
                "Authorization": self._basic(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data=data,
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            raise EbayError(f"eBay token request failed ({r.status_code}): {r.text[:400]}",
                            r.status_code, r.text)
        return r.json()

    def exchange_code(self, code: str) -> dict:
        tok = self._token_request(
            {"grant_type": "authorization_code", "code": code, "redirect_uri": self.ru_name}
        )
        self._store_user_tokens(tok)
        self.settings.set("granted_scopes", " ".join(USER_SCOPES))
        return tok

    def granted_scopes(self) -> list[str]:
        g = self.settings.get("granted_scopes")
        return g.split() if g else list(LEGACY_SCOPES)

    def can_promote(self) -> bool:
        return MARKETING_SCOPE in self.granted_scopes()

    def _store_user_tokens(self, tok: dict) -> None:
        EbayClient._user_token = {
            "value": tok["access_token"],
            "expires": time.time() + int(tok.get("expires_in", 7200)) - 120,
        }
        if tok.get("refresh_token"):
            expiry = datetime.now(timezone.utc) + timedelta(
                seconds=int(tok.get("refresh_token_expires_in", 47304000))
            )
            self.settings.set_many(
                {"refresh_token": tok["refresh_token"], "refresh_token_expiry": _iso(expiry)}
            )

    def user_token(self) -> str:
        t = EbayClient._user_token
        if t["value"] and t["expires"] > time.time():
            return t["value"]
        refresh = self.settings.get("refresh_token")
        if not refresh:
            raise EbayError("Not connected to eBay - please sign in with eBay again.")
        tok = self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh,
             "scope": " ".join(self.granted_scopes())}
        )
        self._store_user_tokens(tok)
        return EbayClient._user_token["value"]

    def app_token(self) -> str:
        t = EbayClient._app_token
        if t["value"] and t["expires"] > time.time():
            return t["value"]
        tok = self._token_request(
            {"grant_type": "client_credentials", "scope": " ".join(APP_SCOPES)}
        )
        EbayClient._app_token = {
            "value": tok["access_token"],
            "expires": time.time() + int(tok.get("expires_in", 7200)) - 120,
        }
        return tok["access_token"]

    @classmethod
    def forget_tokens(cls):
        cls._user_token = {"value": None, "expires": 0}
        cls._app_token = {"value": None, "expires": 0}

    # ------------------------------------------------------------------ #
    # Digital signatures (required for UK/EU sellers on the Finances API)
    # ------------------------------------------------------------------ #
    def ensure_signing_key(self) -> dict:
        s = self.settings.get_many(["signing_jwe", "signing_private_key", "signing_expiry"])
        if s.get("signing_jwe") and s.get("signing_private_key"):
            exp = s.get("signing_expiry")
            if not exp or exp > _iso(datetime.now(timezone.utc) + timedelta(days=7)):
                return {"jwe": s["signing_jwe"], "private_key": s["signing_private_key"]}
        r = requests.post(
            f"{self.env['apiz']}/developer/key_management/v1/signing_key",
            headers={
                "Authorization": f"Bearer {self.app_token()}",
                "Content-Type": "application/json",
            },
            json={"signingKeyCipher": "ED25519"},
            timeout=TIMEOUT,
        )
        if r.status_code not in (200, 201):
            raise EbayError(
                f"Could not create an eBay signing key ({r.status_code}): {r.text[:400]}",
                r.status_code, r.text)
        data = r.json()
        expiry = data.get("expirationTime")
        if isinstance(expiry, (int, float)):
            expiry = _iso(datetime.fromtimestamp(expiry, tz=timezone.utc))
        self.settings.set_many({
            "signing_key_id": data.get("signingKeyId"),
            "signing_jwe": data["jwe"],
            "signing_private_key": data["privateKey"],
            "signing_expiry": expiry or "",
        })
        return {"jwe": data["jwe"], "private_key": data["privateKey"]}

    @staticmethod
    def _load_private_key(text: str):
        text = text.strip()
        if "BEGIN" in text:
            return serialization.load_pem_private_key(text.encode(), password=None)
        return serialization.load_der_private_key(base64.b64decode(text), password=None)

    def signature_headers(self, method: str, url: str) -> dict:
        key = self.ensure_signing_key()
        p = urlparse(url)
        created = int(time.time())
        params = ('("x-ebay-signature-key" "@method" "@path" "@authority")'
                  f";created={created}")
        base = (
            f'"x-ebay-signature-key": {key["jwe"]}\n'
            f'"@method": {method.upper()}\n'
            f'"@path": {p.path}\n'
            f'"@authority": {p.netloc.lower()}\n'
            f'"@signature-params": {params}'
        )
        priv = self._load_private_key(key["private_key"])
        sig = base64.b64encode(priv.sign(base.encode("utf-8"))).decode()
        return {
            "x-ebay-signature-key": key["jwe"],
            "x-ebay-enforce-signature": "true",
            "Signature": f"sig1=:{sig}:",
            "Signature-Input": f"sig1={params}",
        }

    # ------------------------------------------------------------------ #
    # Low level GET with paging
    # ------------------------------------------------------------------ #
    def _get(self, url: str, params: dict | None = None, signed: bool = False) -> dict:
        full = url if not params else f"{url}?{urlencode(params)}"
        for attempt in range(4):
            headers = {
                "Authorization": f"Bearer {self.user_token()}",
                "Accept": "application/json",
                "X-EBAY-C-MARKETPLACE-ID": self.marketplace,
            }
            if signed:
                headers.update(self.signature_headers("GET", full))
            r = requests.get(full, headers=headers, timeout=TIMEOUT)
            if r.status_code == 401 and attempt == 0:
                EbayClient._user_token = {"value": None, "expires": 0}
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt * 2)
                continue
            if r.status_code == 204:
                return {}
            if r.status_code != 200:
                raise EbayError(f"eBay API error {r.status_code} for {urlparse(full).path}: "
                                f"{r.text[:500]}", r.status_code, r.text)
            return r.json()
        raise EbayError(f"eBay API kept failing for {urlparse(full).path}")

    def _paged(self, url, params, key, signed=False):
        data = self._get(url, params, signed)
        while True:
            for item in data.get(key, []) or []:
                yield item
            nxt = data.get("next")
            if not nxt:
                break
            data = self._get(nxt, None, signed)

    # ------------------------------------------------------------------ #
    # APIs
    # ------------------------------------------------------------------ #
    def get_user(self) -> dict:
        return self._get(f"{self.env['apiz']}/commerce/identity/v1/user/")

    def iter_orders(self, since: datetime, by_modified: bool = False):
        field = "lastmodifieddate" if by_modified else "creationdate"
        # Fulfillment API only keeps ~2 years of orders.
        floor = datetime.now(timezone.utc) - timedelta(days=729)
        since = max(since, floor)
        params = {"filter": f"{field}:[{_iso(since)}..]", "limit": 200}
        yield from self._paged(f"{self.env['api']}/sell/fulfillment/v1/order",
                               params, "orders")

    def shipped_date(self, order_id: str) -> str | None:
        """Earliest dispatch date recorded for an order (None if not shipped yet)."""
        data = self._get(f"{self.env['api']}/sell/fulfillment/v1/order/{order_id}/shipping_fulfillment")
        dates = [f.get("shippedDate") for f in data.get("fulfillments", []) if f.get("shippedDate")]
        return min(dates) if dates else None

    def iter_transactions(self, since: datetime, until: datetime | None = None):
        until = until or datetime.now(timezone.utc)
        start = since
        while start < until:
            end = min(start + timedelta(days=90), until)
            params = {
                "filter": f"transactionDate:[{_iso(start)}..{_iso(end)}]",
                "limit": 1000,
            }
            yield from self._paged(f"{self.env['apiz']}/sell/finances/v1/transaction",
                                   params, "transactions", signed=True)
            start = end


    # ------------------------------------------------------------------ #
    # JSON writes (Marketing API)
    # ------------------------------------------------------------------ #
    def _send(self, method: str, url: str, body=None) -> requests.Response:
        for attempt in range(4):
            r = requests.request(method, url, json=body, timeout=TIMEOUT, headers={
                "Authorization": f"Bearer {self.user_token()}",
                "Content-Type": "application/json", "Accept": "application/json",
                "X-EBAY-C-MARKETPLACE-ID": self.marketplace,
            })
            if r.status_code == 401 and attempt == 0:
                EbayClient._user_token = {"value": None, "expires": 0}
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt * 2)
                continue
            if r.status_code >= 400:
                raise EbayError(f"eBay API error {r.status_code} for {urlparse(url).path}: {r.text[:500]}",
                                r.status_code, r.text)
            return r
        raise EbayError(f"eBay API kept failing for {urlparse(url).path}")

    def ensure_campaign(self, name: str, ad_rate: float) -> str:
        base = f"{self.env['api']}/sell/marketing/v1/ad_campaign"
        try:
            r = self._send("GET", f"{base}/get_campaign_by_name?{urlencode({'campaign_name': name})}")
            data = r.json()
            if data.get("campaignStatus") in ("RUNNING", "SCHEDULED", "PAUSED"):
                if data.get("campaignStatus") == "PAUSED":
                    self._send("POST", f"{base}/{data['campaignId']}/resume")
                return data["campaignId"]
        except EbayError as e:
            if e.status not in (404, 400):
                raise
        start = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        r = self._send("POST", base, {
            "campaignName": name, "marketplaceId": self.marketplace, "startDate": start,
            "fundingStrategy": {"fundingModel": "COST_PER_SALE", "bidPercentage": f"{ad_rate:.1f}"},
        })
        loc = r.headers.get("Location", "")
        cid = loc.rstrip("/").split("/")[-1] if loc else (r.json() if r.content else {}).get("campaignId")
        if not cid:
            raise EbayError("eBay created the campaign but didn't return its ID.")
        return cid

    def add_ads(self, campaign_id: str, listing_ids: list[str], ad_rate: float) -> dict:
        """Returns {listing_id: None (ok) or error message}."""
        url = f"{self.env['api']}/sell/marketing/v1/ad_campaign/{campaign_id}/bulk_create_ads_by_listing_id"
        out = {}
        for i in range(0, len(listing_ids), 500):
            chunk = listing_ids[i:i + 500]
            r = self._send("POST", url, {"requests": [
                {"listingId": lid, "bidPercentage": f"{ad_rate:.1f}"} for lid in chunk]})
            data = r.json() if r.content else {}
            for resp in data.get("responses", []):
                lid = resp.get("listingId")
                ok = 200 <= int(resp.get("statusCode", 201)) < 300
                errs = resp.get("errors") or []
                # 35036 = already has an ad: treat as success
                if not ok and any(str(e.get("errorId")) == "35036" for e in errs):
                    ok = True
                out[lid] = None if ok else "; ".join(e.get("message", "") for e in errs) or "failed"
            for lid in chunk:
                out.setdefault(lid, None)
        return out

    # ------------------------------------------------------------------ #
    # Trading API (XML) - your listings, ending and relisting
    # ------------------------------------------------------------------ #
    def trading(self, call: str, inner_xml: str) -> ET.Element:
        body = (f'<?xml version="1.0" encoding="utf-8"?><{call}Request xmlns="{TRADING_NS}">'
                f"<ErrorLanguage>en_GB</ErrorLanguage><WarningLevel>High</WarningLevel>"
                f"{inner_xml}</{call}Request>")
        for attempt in range(4):
            r = requests.post(f"{self.env['api']}/ws/api.dll", data=body.encode("utf-8"), timeout=TIMEOUT,
                              headers={
                                  "X-EBAY-API-CALL-NAME": call,
                                  "X-EBAY-API-SITEID": SITE_IDS.get(self.marketplace, "3"),
                                  "X-EBAY-API-COMPATIBILITY-LEVEL": self.settings.get("trading_compat") or TRADING_COMPAT,
                                  "X-EBAY-API-IAF-TOKEN": self.user_token(),
                                  "Content-Type": "text/xml",
                              })
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt * 2)
                continue
            try:
                root = ET.fromstring(r.content)
            except ET.ParseError:
                raise EbayError(f"Trading API {call} returned {r.status_code}: {r.text[:300]}", r.status_code)
            ack = _tx(root, "Ack")
            if ack in ("Failure", "PartialFailure"):
                errs = [(_tx(e, "ErrorCode"), _tx(e, "LongMessage") or _tx(e, "ShortMessage"))
                        for e in root.findall(f"{{{TRADING_NS}}}Errors")
                        if _tx(e, "SeverityCode") == "Error"]
                if any(code == "21917053" for code, _ in errs) and attempt == 0:  # token expired
                    EbayClient._user_token = {"value": None, "expires": 0}
                    continue
                raise EbayError(f"{call} failed: " + "; ".join(f"{m} ({c})" for c, m in errs), r.status_code,
                                r.text)
            return root
        raise EbayError(f"Trading API {call} kept failing")

    def iter_active_listings(self):
        page, pages = 1, 1
        while page <= pages:
            root = self.trading("GetMyeBaySelling", (
                "<ActiveList><Include>true</Include><IncludeWatchCount>true</IncludeWatchCount>"
                f"<Pagination><EntriesPerPage>200</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>"
                "</ActiveList><DetailLevel>ReturnAll</DetailLevel>"))
            al = root.find(f"{{{TRADING_NS}}}ActiveList")
            if al is None:
                return
            pages = int(_tx(al, "PaginationResult/TotalNumberOfPages") or 1)
            for it in al.findall(f"{{{TRADING_NS}}}ItemArray/{{{TRADING_NS}}}Item"):
                yield _parse_item(it)
            page += 1

    def end_listing(self, legacy_id: str) -> None:
        self.trading("EndFixedPriceItem",
                     f"<ItemID>{escape(legacy_id)}</ItemID><EndingReason>NotAvailable</EndingReason>")

    def get_item_details(self, legacy_id: str) -> dict:
        """Description, condition, photos and item specifics of one of your listings (for copying it elsewhere)."""
        root = self.trading("GetItem", f"<ItemID>{escape(legacy_id)}</ItemID><DetailLevel>ReturnAll</DetailLevel>"
                                       "<IncludeItemSpecifics>true</IncludeItemSpecifics>")
        it = root.find(f"{{{TRADING_NS}}}Item")
        if it is None:
            raise EbayError("eBay returned no item.")
        ns = f"{{{TRADING_NS}}}"
        specifics = []
        for nv in it.findall(f"{ns}ItemSpecifics/{ns}NameValueList"):
            vals = [v.text for v in nv.findall(f"{ns}Value") if v.text]
            if _tx(nv, "Name") and vals:
                specifics.append((_tx(nv, "Name"), ", ".join(vals)))
        qty, sold = int(_tx(it, "Quantity") or 1), int(_tx(it, "SellingStatus/QuantitySold") or 0)
        return {
            "title": _tx(it, "Title"), "description_html": _tx(it, "Description") or "",
            "condition_id": _tx(it, "ConditionID"), "condition": _tx(it, "ConditionDisplayName"),
            "condition_note": _tx(it, "ConditionDescription"),
            "category": _tx(it, "PrimaryCategory/CategoryName"),
            "pictures": [p.text for p in it.findall(f"{ns}PictureDetails/{ns}PictureURL") if p.text],
            "specifics": specifics, "price": float(_tx(it, "SellingStatus/CurrentPrice") or 0),
            "quantity_available": max(qty - sold, 0), "quantity_sold": sold,
        }

    def listing_quantity(self, legacy_id: str) -> tuple[int, int]:
        """(available, sold) for a fixed-price listing."""
        root = self.trading("GetItem", f"<ItemID>{escape(legacy_id)}</ItemID><DetailLevel>ItemReturnAttributes"
                                       "</DetailLevel>")
        it = root.find(f"{{{TRADING_NS}}}Item")
        qty, sold = int(_tx(it, "Quantity") or 1), int(_tx(it, "SellingStatus/QuantitySold") or 0)
        return max(qty - sold, 0), sold

    def set_available(self, legacy_id: str, available: int) -> int:
        """Set how many are left for sale on a fixed-price listing, then read it back to check.

        eBay's guidance is that the revised Quantity is the number available. In case a listing behaves the other
        way (available + sold), the result is checked and corrected once. Returns the available count eBay reports.
        """
        def revise(q):
            self.trading("ReviseInventoryStatus",
                         f"<InventoryStatus><ItemID>{escape(legacy_id)}</ItemID><Quantity>{int(q)}</Quantity>"
                         "</InventoryStatus>")
        revise(available)
        now, sold = self.listing_quantity(legacy_id)
        if now != available:
            revise(available + sold)
            now, _ = self.listing_quantity(legacy_id)
        return now

    def relist(self, legacy_id: str, new_price: float | None) -> str:
        price = f"<StartPrice>{new_price:.2f}</StartPrice>" if new_price else ""
        root = self.trading("RelistFixedPriceItem",
                            f"<Item><ItemID>{escape(legacy_id)}</ItemID>{price}</Item>")
        new_id = _tx(root, "ItemID")
        if not new_id:
            raise EbayError("Relist succeeded but eBay returned no new item ID.")
        return new_id


def _tx(el, path):
    parts = "/".join(f"{{{TRADING_NS}}}{p}" for p in path.split("/"))
    found = el.find(parts)
    return found.text if found is not None and found.text is not None else None


def _parse_item(it) -> dict:
    def f(path):
        return _tx(it, path)
    price = f("SellingStatus/CurrentPrice") or f("BuyItNowPrice") or f("StartPrice")
    cur_el = it.find(f"{{{TRADING_NS}}}SellingStatus/{{{TRADING_NS}}}CurrentPrice")
    qty = int(f("Quantity") or 1)
    sold = int(f("SellingStatus/QuantitySold") or 0)
    avail = f("QuantityAvailable")
    return {
        "legacy_item_id": f("ItemID"), "title": f("Title"), "sku": f("SKU"),
        "price": float(price) if price else None,
        "currency": cur_el.get("currencyID") if cur_el is not None else "GBP",
        "quantity": qty, "quantity_sold": sold,
        "quantity_available": int(avail) if avail else max(qty - sold, 0),
        "listing_type": f("ListingType"), "duration": f("ListingDuration"),
        "start_time": f("ListingDetails/StartTime"), "end_time": f("ListingDetails/EndTime"),
        "watch_count": int(f("WatchCount") or 0),
        "image_url": f("PictureDetails/GalleryURL"), "url": f("ListingDetails/ViewItemURL"),
        "bids": int(f("SellingStatus/BidCount") or 0),
    }


def new_state() -> str:
    return secrets.token_urlsafe(24)


def money(obj) -> float:
    """eBay Amount -> float (0.0 if missing)."""
    if not obj:
        return 0.0
    try:
        return float(obj.get("value", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def currency(obj, default="GBP") -> str:
    return (obj or {}).get("currency") or default


def dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))
