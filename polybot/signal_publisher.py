import os, requests, json

_session = None

def _get_session():
    global _session
    if _session is None:
        _session = requests.Session()
    return _session

def send_signal(city, bucket, price):
    url = os.environ.get("CF_WORKER_URL")
    if url:
        try:
            s = _get_session()
            r = s.post(url, json={"city": city, "bucket": bucket, "price": price}, timeout=2)
            if r.status_code != 200:
                print(f"Signal error: {r.status_code}")
        except Exception as e:
            print(f"Signal exception: {e}")
