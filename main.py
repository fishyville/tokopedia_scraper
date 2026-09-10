from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from scraper_tokopedia import run_scrape

app = FastAPI(title="Tokopedia Scraper API")


class ScrapeRequest(BaseModel):
    search: int
    keywords: str = Field(..., min_length=1)
    target_count: int = Field(..., gt=0)


class ScrapeResponse(BaseModel):
    keywords: str
    target_count: int
    items_scraped: int
    items_inserted: int
    variants_inserted: int
    unresolved_links: int
    debug_file: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/scrape", response_model=ScrapeResponse)
def scrape(payload: ScrapeRequest):
    """
    Runs a full Tokopedia scrape (search -> scroll/collect -> visit
    each product page for variants) and inserts results directly
    into dbo.Items / dbo.ItemVariants.

    This is a synchronous, blocking Selenium job (can take minutes
    for a large target_count). FastAPI runs sync `def` routes in a
    thread pool automatically, so it won't block the event loop for
    other requests, but each call spins up its own Chrome instance -
    fine for occasional/manual triggering, not built for high
    concurrency as-is.
    """
    try:
        return run_scrape(payload.search, payload.keywords, payload.target_count)
    except RuntimeError as e:
        # e.g. missing DB env vars
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Scrape failed: {e}")