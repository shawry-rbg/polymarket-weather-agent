import os, redis, json

_price_client = None

def _get_client():
    global _price_client
    if _price_client is None:
        url = os.environ.get("REDIS_URL")
        if url:
            _price_client = redis.from_url(url)
    return _price_client

def publish_price_update(city, bucket, price):
    r = _get_client()
    if r:
        try:
            r.publish("price_updates", json.dumps({"city": city, "bucket": bucket, "price": price}))
        except Exception as e:
            print(f"Redis error: {e}")
