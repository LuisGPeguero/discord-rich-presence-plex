import requests
from typing import Optional, List
from core.config import config
from utils.logging import logger

TMDB_API_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"

def get_tmdb_poster_url(
    guids: Optional[List[str]],
    title: str,
    year: Optional[int] = None,
    media_type: str = "movie",
    country: Optional[str] = None
) -> Optional[str]:
    logger.info(f"get_tmdb_poster_url called with guids={guids}, title={title!r}, year={year}, media_type={media_type}, country={country}")
    tmdbApiKey = config["display"]["posters"].get("tmdbApiKey", "")
    logger.info(f"TMDb API Key loaded: {'*' * (len(tmdbApiKey) - 4) + tmdbApiKey[-4:] if tmdbApiKey else 'None'}")
    if not tmdbApiKey:
        logger.warning("TMDb API key is missing or empty. Poster fetching will be skipped.")
        return None

    tmdb_id = None
    if guids:
        for guid in guids:
            if guid.startswith("tmdb://"):
                tmdb_id = guid.split("tmdb://")[1]
                break
    if tmdb_id:
        endpoint = f"{TMDB_API_BASE}/{media_type}/{tmdb_id}"
        params = {"api_key": tmdbApiKey}
        logger.info(f"TMDb direct lookup endpoint: {endpoint}")
        try:
            resp = requests.get(endpoint, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            poster_path = data.get("poster_path")
            if poster_path:
                poster_url = f"{TMDB_IMAGE_BASE}{poster_path}"
                logger.info(f"TMDb poster found via GUID: {poster_url}")
                return poster_url
            else:
                logger.info("TMDb: No poster_path found in direct lookup.")
        except Exception as e:
            logger.exception(f"TMDB: Exception during direct poster fetch: {e}")

    params = {
        "api_key": tmdbApiKey,
        "query": title,
    }
    if year:
        params["year" if media_type == "movie" else "first_air_date_year"] = str(year)
    endpoint = f"{TMDB_API_BASE}/search/{media_type}"
    logger.info(f"TMDb search endpoint: {endpoint} with params: {params}")
    try:
        resp = requests.get(endpoint, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        if not results:
            logger.info("TMDb: No results found in search.")
            return None
        if country:
            filtered = [r for r in results if country in r.get("origin_country", [])]
            if filtered:
                results = filtered
        poster_path = results[0].get("poster_path")
        if not poster_path:
            logger.info("TMDb: No poster_path in first search result.")
            return None
        poster_url = f"{TMDB_IMAGE_BASE}{poster_path}"
        logger.info(f"TMDb poster found via search: {poster_url}")
        return poster_url
    except Exception as e:
        logger.exception(f"TMDB: Exception during poster fetch: {e}")
        return None 