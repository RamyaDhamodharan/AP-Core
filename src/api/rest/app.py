from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.rest.routers import vendors

app = FastAPI(
    title="GenWorx AP Core Service",
    version="0.1.0",
    description="Core API for GenWorx AP Assistant",
)


@app.get("/health")
def health_check():
    return {"status": "ok", "service": "ap-core"}


app.include_router(vendors.router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
