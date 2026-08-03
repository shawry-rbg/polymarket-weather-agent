import modal
from fastapi import FastAPI, Request, Response
import httpx

app = modal.App("poly-proxy-modal")
web_app = FastAPI()

CLOB_BASE = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"


@web_app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy(request: Request, path: str):
    if path.startswith("clob/"):
        target = f"{CLOB_BASE}/{path[5:]}"
    elif path.startswith("gamma/"):
        target = f"{GAMMA_BASE}/{path[6:]}"
    else:
        return Response(content="Path must start with clob/ or gamma/", status_code=404)

    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}

    async with httpx.AsyncClient() as client:
        resp = await client.request(
            method=request.method,
            url=target,
            headers=headers,
            content=body,
            timeout=30.0,
        )

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=dict(resp.headers),
    )


@app.function(
    image=modal.Image.debian_slim().pip_install("fastapi", "httpx"),
    region="us-east-1",
)
@modal.concurrent(max_inputs=100)
@modal.asgi_app()
def fastapi_app():
    return web_app
