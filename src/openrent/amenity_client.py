"""Shared HTTP connections and request budgets for metadata providers."""

import asyncio
import math
import time
from email.utils import parsedate_to_datetime

import httpx
from pydantic import BaseModel, Field, ValidationError

from .client import FetchError

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_FALLBACK_URL = "http://127.0.0.1:8003/api/interpreter"
VALHALLA_URL = "http://127.0.0.1:8002/sources_to_targets"
TFL_URL = "https://api.tfl.gov.uk"
WALKING_SPEED_KMH = 5.0


class OSMElement(BaseModel):
    type: str
    id: int
    lat: float | None = None
    lon: float | None = None
    center: dict[str, float] | None = None
    tags: dict[str, str] = Field(default_factory=dict)


class _OverpassResult(BaseModel):
    elements: list[OSMElement]


class WalkingRoute(BaseModel):
    to_index: int = Field(ge=0)
    time: float | None = Field(ge=0, allow_inf_nan=False)
    distance: float | None = Field(ge=0, allow_inf_nan=False)


class _MatrixResult(BaseModel):
    sources_to_targets: list[list[WalkingRoute]]


class AmenityClient:
    """One shared client with separate, serialized provider request budgets."""

    def __init__(
        self,
        *,
        timeout: float = 60,
        overpass_url: str = OVERPASS_URL,
        overpass_fallback_url: str | None = OVERPASS_FALLBACK_URL,
        valhalla_url: str = VALHALLA_URL,
        tfl_url: str = TFL_URL,
    ):
        self.overpass_url = overpass_url
        self.overpass_fallback_url = overpass_fallback_url
        self._overpass_retry_at = 0.0
        self.valhalla_url = valhalla_url
        self.tfl_url = tfl_url
        self.http = httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": "openrent-fetch/0.1 (personal rental search)"},
        )
        self._locks = {
            name: asyncio.Lock() for name in ("overpass", "overpass_local", "valhalla", "tfl")
        }
        self._next_request = {name: 0.0 for name in self._locks}
        self._intervals = {"overpass": 10.0, "overpass_local": 0.0, "valhalla": 1.0, "tfl": 1.5}

    async def __aenter__(self):
        await self.http.__aenter__()
        return self

    async def __aexit__(self, *args):
        await self.http.__aexit__(*args)

    async def osm_elements(self, query: str) -> list[OSMElement]:
        raw = await self.request("overpass", data={"data": query})
        if raw.get("remark"):
            raise FetchError(f"Overpass did not complete the amenity lookup: {raw['remark']}")
        try:
            return _OverpassResult.model_validate(raw).elements
        except ValidationError as exc:
            raise FetchError(f"Overpass returned invalid amenity metadata: {exc}") from exc

    async def walking_routes(
        self, latitude: float, longitude: float, targets: list[tuple[float, float]]
    ) -> list[WalkingRoute]:
        """Return pedestrian routes in target order, with at most 25 targets per request."""
        routes = []
        try:
            for start in range(0, len(targets), 25):
                batch = targets[start : start + 25]
                matrix = await self.request(
                    "valhalla",
                    json={
                        "sources": [{"lat": latitude, "lon": longitude}],
                        "targets": [{"lat": lat, "lon": lon} for lat, lon in batch],
                        "costing": "pedestrian",
                        "costing_options": {"pedestrian": {"walking_speed": WALKING_SPEED_KMH}},
                        "units": "kilometers",
                    },
                )
                result = _MatrixResult.model_validate(matrix)
                if len(result.sources_to_targets) != 1:
                    raise FetchError("Valhalla returned an unexpected number of matrix sources")
                batch_routes = result.sources_to_targets[0]
                if len(batch_routes) != len(batch) or {
                    route.to_index for route in batch_routes
                } != set(range(len(batch))):
                    raise FetchError("Valhalla returned an incomplete pedestrian matrix")
                routes.extend(sorted(batch_routes, key=lambda route: route.to_index))
        except ValidationError as exc:
            raise FetchError(f"Valhalla returned invalid walking metadata: {exc}") from exc
        return routes

    async def request(self, service: str, *, path: str = "", **kwargs) -> dict:
        async with self._locks[service]:
            if (
                service == "overpass"
                and self.overpass_fallback_url
                and time.monotonic() < self._overpass_retry_at
            ):
                return await self.request("overpass_local", path=path, **kwargs)
            wait = self._next_request[service] - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_request[service] = time.monotonic() + self._intervals[service]
            url = {
                "overpass": self.overpass_url,
                "overpass_local": self.overpass_fallback_url,
                "valhalla": self.valhalla_url,
                "tfl": self.tfl_url,
            }[service]
            if path:
                url = url.rstrip("/") + "/" + path.lstrip("/")
            try:
                response = await self.http.request(
                    "GET" if service == "tfl" else "POST", url, **kwargs
                )
                if response.status_code in {429, 503}:
                    retry_after = response.headers.get("Retry-After", "")
                    try:
                        delay = float(retry_after)
                    except ValueError:
                        try:
                            delay = parsedate_to_datetime(retry_after).timestamp() - time.time()
                        except (ValueError, TypeError, OverflowError):
                            delay = 30.0
                    if math.isfinite(delay) and delay >= 0:
                        self._next_request[service] = max(
                            self._next_request[service], time.monotonic() + delay
                        )
                response.raise_for_status()
                result = response.json()
            except httpx.HTTPError as exc:
                retryable = isinstance(exc, httpx.RequestError) or (
                    isinstance(exc, httpx.HTTPStatusError)
                    and (exc.response.status_code == 429 or 500 <= exc.response.status_code < 600)
                )
                if service == "overpass" and self.overpass_fallback_url and retryable:
                    self._overpass_retry_at = max(
                        self._next_request["overpass"], time.monotonic() + 30
                    )
                    return await self.request("overpass_local", path=path, **kwargs)
                raise FetchError(f"{service} amenity lookup failed: {exc}") from exc
            except ValueError as exc:
                raise FetchError(f"{service} amenity lookup failed: {exc}") from exc
            if not isinstance(result, dict):
                raise FetchError(f"{service} returned invalid amenity JSON")
            return result
