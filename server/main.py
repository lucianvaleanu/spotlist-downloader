from fastapi import FastAPI
from routers.download import router as download_router
from routers.spotify import router as spotify_router

app = FastAPI(
    title="Spotlist Downloader API",
    description=(
        "local api for starting slskd in docker and queueing spotify playlist "
        "downloads through Soulseek."
    ),
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

app.include_router(spotify_router)
app.include_router(download_router)

@app.get("/")
async def root():
    return {
        "message": "the revolution will not be streamed.",
        "swagger_ui": "/docs",
        "redoc": "/redoc",
        "openapi": "/openapi.json",
    }