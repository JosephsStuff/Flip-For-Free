"""CardTrader API v2 client (https://www.cardtrader.com/docs/api/full).

Each seller uses their own token from CardTrader → Settings → API. Prices sent to CardTrader are in the account's
currency; money coming back is {"cents": int, "currency": "EUR"}.
"""
import time

import requests

BASE = "https://api.cardtrader.com/api/v2"
TIMEOUT = 30
EXPORT_TIMEOUT = 180


class CardTraderError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class CardTraderClient:
    def __init__(self, token: str):
        if not token:
            raise CardTraderError("CardTrader isn't connected. Add your API token in Settings → Other shops.")
        self.token = token
        self._last_marketplace = 0.0

    def _req(self, method, path, params=None, body=None, timeout=TIMEOUT):
        for attempt in range(4):
            try:
                r = requests.request(method, BASE + path, params=params, json=body, timeout=timeout,
                                     headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"})
            except requests.RequestException as e:
                if attempt == 3:
                    raise CardTraderError(f"Couldn't reach CardTrader: {e}")
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(float(r.headers.get("Retry-After") or 2 ** attempt * 2))
                continue
            if r.status_code == 401:
                raise CardTraderError("CardTrader rejected the API token. Check it in Settings → Other shops.", 401)
            if r.status_code >= 400:
                try:
                    j = r.json()
                    msg = j.get("error") or j.get("message") or j.get("errors") or r.text
                except ValueError:
                    msg = r.text
                raise CardTraderError(f"CardTrader said: {str(msg)[:300]}", r.status_code)
            return r.json() if r.content else {}
        raise CardTraderError("CardTrader kept failing; try again later.")

    # account and catalogue
    def info(self):
        return self._req("GET", "/info")

    def games(self):
        return self._req("GET", "/games")

    def expansions(self):
        return self._req("GET", "/expansions")

    def blueprints(self, expansion_id: int):
        return self._req("GET", "/blueprints/export", {"expansion_id": expansion_id}, timeout=60)

    def categories(self, game_id: int):
        return self._req("GET", "/categories", {"game_id": game_id})

    def marketplace(self, blueprint_id: int, language: str | None = None):
        """The cheapest listings for a card (up to 25). CardTrader allows 1 call a second here."""
        wait = 1.05 - (time.time() - self._last_marketplace)
        if wait > 0:
            time.sleep(wait)
        self._last_marketplace = time.time()
        params = {"blueprint_id": blueprint_id}
        if language:
            params["language"] = language
        data = self._req("GET", "/marketplace/products", params)
        return data.get(str(blueprint_id)) or data.get(blueprint_id) or []

    # your stock
    def my_products(self, blueprint_id: int | None = None):
        params = {"blueprint_id": blueprint_id} if blueprint_id else None
        return self._req("GET", "/products/export", params, timeout=EXPORT_TIMEOUT)

    def create_product(self, blueprint_id, price, quantity, properties=None, description=None, user_data=None):
        body = {"blueprint_id": int(blueprint_id), "price": round(float(price), 2), "quantity": int(quantity),
                "error_mode": "strict"}
        if properties:
            body["properties"] = properties
        if description:
            body["description"] = description
        if user_data:
            body["user_data_field"] = user_data
        return self._req("POST", "/products", body=body)

    def update_product(self, product_id, **fields):
        body = {k: v for k, v in fields.items() if v is not None}
        body["error_mode"] = "strict"
        return self._req("PUT", f"/products/{int(product_id)}", body=body)

    def increment(self, product_id, delta: int):
        """Change the quantity by delta; CardTrader deletes the listing if it reaches 0."""
        return self._req("POST", f"/products/{int(product_id)}/increment", body={"delta_quantity": int(delta)})

    def delete_product(self, product_id):
        return self._req("DELETE", f"/products/{int(product_id)}")

    # orders
    def iter_orders(self, since_date: str, page_size: int = 100):
        page = 1
        while True:
            batch = self._req("GET", "/orders", {"order_as": "seller", "from": since_date, "sort": "id.asc",
                                                 "page": page, "limit": page_size})
            if isinstance(batch, dict):
                batch = batch.get("array") or batch.get("orders") or []
            yield from batch
            if len(batch) < page_size:
                return
            page += 1


def money(m) -> tuple[float, str | None]:
    """CardTrader money {'cents': 170, 'currency': 'EUR'} -> (1.70, 'EUR')."""
    if not m:
        return 0.0, None
    if isinstance(m, (int, float)):
        return float(m), None
    return (m.get("cents") or 0) / 100, m.get("currency")
