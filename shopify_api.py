"""Shopify Admin GraphQL client for your own store.

Connects with an app made in Shopify's Dev Dashboard (client ID + secret) using the client credentials grant: the app
and the store must be in the same Shopify organisation, and tokens last 24 hours (refreshed automatically).
https://shopify.dev/docs/apps/build/authentication-authorization/client-credentials-grant
"""
import re
import time
import uuid

import requests

API_VERSION = "2026-07"
TIMEOUT = 40
SCOPES_NEEDED = ["read_products", "write_products", "read_inventory", "write_inventory", "read_locations",
                 "read_orders", "read_publications", "write_publications"]


class ShopifyError(Exception):
    pass


def clean_shop(shop: str) -> str:
    """'My Shop', 'my-shop', 'https://my-shop.myshopify.com/admin' -> 'my-shop.myshopify.com'."""
    s = (shop or "").strip().lower()
    s = re.sub(r"^https?://", "", s).split("/")[0]
    if s and not s.endswith(".myshopify.com"):
        s = s.split(".")[0] + ".myshopify.com"
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.myshopify\.com", s or ""):
        raise ShopifyError("Enter your store's myshopify.com address, e.g. my-shop.myshopify.com.")
    return s


class ShopifyClient:
    _tokens: dict = {}          # shop -> (token, expires_at, scopes)

    def __init__(self, shop: str, client_id: str, client_secret: str, version: str | None = None):
        if not (shop and client_id and client_secret):
            raise ShopifyError("Shopify isn't connected. Add your store and app keys in Settings → Other shops.")
        self.shop, self.client_id, self.secret = clean_shop(shop), client_id.strip(), client_secret.strip()
        self.version = version or API_VERSION

    # ------------------------------------------------------------------ auth
    def token(self) -> str:
        tok = self._tokens.get(self.shop)
        if tok and tok[1] > time.time() + 300:
            return tok[0]
        try:
            r = requests.post(f"https://{self.shop}/admin/oauth/access_token", timeout=TIMEOUT,
                              data={"grant_type": "client_credentials", "client_id": self.client_id,
                                    "client_secret": self.secret})
        except requests.RequestException as e:
            raise ShopifyError(f"Couldn't reach Shopify: {e}")
        if r.status_code != 200:
            body = r.text[:300]
            if "shop_not_permitted" in body:
                raise ShopifyError("Shopify says this app can't connect to that store: the app and the store must "
                                   "be in the same Shopify organisation, and the app must be installed on the store.")
            raise ShopifyError(f"Shopify didn't accept the app keys ({r.status_code}): {body}")
        j = r.json()
        self._tokens[self.shop] = (j["access_token"], time.time() + int(j.get("expires_in") or 86399),
                                   (j.get("scope") or "").split(","))
        return j["access_token"]

    def scopes(self) -> list[str]:
        self.token()
        return self._tokens[self.shop][2]

    def gql(self, query: str, variables: dict | None = None) -> dict:
        for attempt in range(5):
            try:
                r = requests.post(f"https://{self.shop}/admin/api/{self.version}/graphql.json", timeout=TIMEOUT,
                                  json={"query": query, "variables": variables or {}},
                                  headers={"X-Shopify-Access-Token": self.token(), "Content-Type": "application/json"})
            except requests.RequestException as e:
                if attempt == 4:
                    raise ShopifyError(f"Couldn't reach Shopify: {e}")
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 401:
                self._tokens.pop(self.shop, None)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(float(r.headers.get("Retry-After") or 2 ** attempt))
                continue
            if r.status_code >= 400:
                raise ShopifyError(f"Shopify returned {r.status_code}: {r.text[:300]}")
            j = r.json()
            errs = j.get("errors")
            if errs:
                if any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errs if isinstance(e, dict)):
                    time.sleep(2 ** attempt)
                    continue
                msg = "; ".join(e.get("message", str(e)) if isinstance(e, dict) else str(e) for e in errs)
                if "access" in msg.lower() and "denied" in msg.lower():
                    msg += (" Give the app these access scopes in the Dev Dashboard, release a new version and "
                            "reinstall it: " + ", ".join(SCOPES_NEEDED))
                raise ShopifyError(f"Shopify: {msg}")
            return j["data"]
        raise ShopifyError("Shopify kept failing; try again later.")

    @staticmethod
    def _user_errors(payload: dict):
        errs = (payload or {}).get("userErrors") or []
        if errs:
            raise ShopifyError("Shopify: " + "; ".join(f"{'.'.join(e.get('field') or [])} {e['message']}".strip()
                                                       for e in errs))

    # ------------------------------------------------------------------ store
    def shop_info(self) -> dict:
        d = self.gql("""{ shop { name myshopifyDomain currencyCode }
                          locations(first: 20) { nodes { id name isActive } } }""")
        return {"name": d["shop"]["name"], "domain": d["shop"]["myshopifyDomain"],
                "currency": d["shop"]["currencyCode"],
                "locations": [n for n in d["locations"]["nodes"] if n.get("isActive", True)]}

    # ------------------------------------------------------------------ products
    def create_product(self, *, title, description_html, price, quantity, location_id, sku=None, images=(),
                       status="ACTIVE", product_type=None, tags=()) -> dict:
        variant = {"optionValues": [{"optionName": "Title", "name": "Default Title"}], "price": round(float(price), 2),
                   "inventoryItem": {"tracked": True, **({"sku": sku} if sku else {})},
                   "inventoryQuantities": [{"locationId": location_id, "name": "available",
                                            "quantity": int(quantity)}]}
        inp = {"title": title, "descriptionHtml": description_html, "status": status,
               "productOptions": [{"name": "Title", "position": 1, "values": [{"name": "Default Title"}]}],
               "variants": [variant],
               "files": [{"originalSource": u, "contentType": "IMAGE"} for u in list(images)[:10]]}
        if product_type:
            inp["productType"] = product_type
        if tags:
            inp["tags"] = list(tags)
        d = self.gql("""mutation set($input: ProductSetInput!) {
              productSet(synchronous: true, input: $input) {
                product { id handle onlineStoreUrl variants(first: 1) { nodes { id inventoryItem { id } } } }
                userErrors { field message } } }""", {"input": inp})
        self._user_errors(d["productSet"])
        p = d["productSet"]["product"]
        v = p["variants"]["nodes"][0]
        return {"product_id": p["id"], "variant_id": v["id"], "inventory_item_id": v["inventoryItem"]["id"],
                "handle": p["handle"], "url": p.get("onlineStoreUrl")}

    def publish_online_store(self, product_id: str) -> bool:
        """Put the product on the Online Store sales channel (apps' products aren't on it by default)."""
        try:
            d = self.gql("{ publications(first: 20) { nodes { id name } } }")
            pubs = [n for n in d["publications"]["nodes"] if "online store" in (n.get("name") or "").lower()]
            if not pubs:
                return False
            d = self.gql("""mutation pub($id: ID!, $input: [PublicationInput!]!) {
                  publishablePublish(id: $id, input: $input) { userErrors { field message } } }""",
                         {"id": product_id, "input": [{"publicationId": pubs[0]["id"]}]})
            self._user_errors(d["publishablePublish"])
            return True
        except ShopifyError:
            return False

    def set_status(self, product_id: str, status: str) -> None:
        d = self.gql("""mutation up($p: ProductUpdateInput!) { productUpdate(product: $p) {
              userErrors { field message } } }""", {"p": {"id": product_id, "status": status}})
        self._user_errors(d["productUpdate"])

    def set_price(self, product_id: str, variant_id: str, price: float) -> None:
        d = self.gql("""mutation pr($pid: ID!, $v: [ProductVariantsBulkInput!]!) {
              productVariantsBulkUpdate(productId: $pid, variants: $v) { userErrors { field message } } }""",
                     {"pid": product_id, "v": [{"id": variant_id, "price": round(float(price), 2)}]})
        self._user_errors(d["productVariantsBulkUpdate"])

    def delete_product(self, product_id: str) -> None:
        d = self.gql("""mutation del($i: ProductDeleteInput!) { productDelete(input: $i) {
              userErrors { field message } } }""", {"i": {"id": product_id}})
        self._user_errors(d["productDelete"])

    # ------------------------------------------------------------------ stock
    def available(self, inventory_item_id: str, location_id: str) -> int:
        d = self.gql("""query q($id: ID!, $loc: ID!) { inventoryItem(id: $id) {
              inventoryLevel(locationId: $loc) { quantities(names: ["available"]) { name quantity } } } }""",
                     {"id": inventory_item_id, "loc": location_id})
        lvl = (d.get("inventoryItem") or {}).get("inventoryLevel") or {}
        return sum(q["quantity"] for q in lvl.get("quantities") or [])

    def adjust(self, inventory_item_id: str, location_id: str, delta: int) -> int:
        """Change stock by delta (compare-and-set against the current level). Returns the new level."""
        current = self.available(inventory_item_id, location_id)
        d = self.gql("""mutation adj($input: InventoryAdjustQuantitiesInput!, $key: String!) {
              inventoryAdjustQuantities(input: $input) @idempotent(key: $key) { userErrors { field message } } }""",
                     {"key": str(uuid.uuid4()), "input": {
                         "reason": "correction", "name": "available",
                         "changes": [{"delta": int(delta), "inventoryItemId": inventory_item_id,
                                      "locationId": location_id, "changeFromQuantity": current}]}})
        self._user_errors(d["inventoryAdjustQuantities"])
        return current + int(delta)

    # ------------------------------------------------------------------ orders
    def iter_orders(self, updated_since_iso: str):
        after = None
        q = """query o($q: String!, $after: String) {
          orders(first: 50, after: $after, query: $q, sortKey: UPDATED_AT) {
            pageInfo { hasNextPage endCursor }
            nodes { id name createdAt processedAt cancelledAt displayFinancialStatus
              totalShippingPriceSet { shopMoney { amount currencyCode } }
              shippingAddress { countryCodeV2 city zip }
              lineItems(first: 50) { nodes { id title quantity sku
                originalUnitPriceSet { shopMoney { amount currencyCode } }
                discountedTotalSet { shopMoney { amount currencyCode } }
                variant { id } product { id } } } } } }"""
        while True:
            d = self.gql(q, {"q": f"updated_at:>='{updated_since_iso}'", "after": after})
            o = d["orders"]
            yield from o["nodes"]
            if not o["pageInfo"]["hasNextPage"]:
                return
            after = o["pageInfo"]["endCursor"]
