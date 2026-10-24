"""
Application module for main.
Flow: shared support code called by the application flow.
Called by: the framework, CLI, or application entry point.
Calls: standard-library or framework helpers.
Tables: no database tables directly.
"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "src.api.rest.app:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
    )
