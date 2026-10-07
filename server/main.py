from fastapi import FastAPI
from routers.spotify import router as spotify_router

app = FastAPI(title="Spotlist Downloader")

app.include_router(spotify_router)

@app.get("/")
async def root():
    return {"message": "the revolution will not be streamed."}