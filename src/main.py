from fastapi import FastAPI

app = FastAPI(
    title="GenWorx AP Core Service",
    version="0.1.0",
    description="Document ingestion, AP processing, matching and review API.",
)


@app.get("/health", summary="AP Core health", description="Returns service health.")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "ap-core"}
