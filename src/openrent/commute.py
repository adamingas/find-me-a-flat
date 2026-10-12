"""TfL public transport journeys to the search target, arriving by 9am."""

from builtins import type as ModelType
from datetime import date, datetime, time, timedelta
from typing import ClassVar
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, ValidationError

from .amenity_client import AmenityClient
from .client import FetchError
from .models import Property

LONDON = ZoneInfo("Europe/London")
_TRANSIT_MODES = {
    "bus",
    "public-bus",
    "tube",
    "overground",
    "train",
    "dlr",
    "tram",
    "coach",
    "river-bus",
    "river",
    "cable-car",
    "cablecar",
    "elizabeth-line",
}


def next_weekday(today: date) -> date:
    day = today + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


class CommuteAmenities(BaseModel):
    target: str
    target_latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    target_longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    origin_latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    origin_longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    arrive_by: datetime
    source: str = "TfL"
    duration_minutes: int | None = Field(default=None, ge=0)
    departure_time: datetime | None = None
    arrival_time: datetime | None = None
    route: list[str] = Field(default_factory=list)


class _Mode(BaseModel):
    id: str


class _Instruction(BaseModel):
    summary: str


class _Leg(BaseModel):
    mode: _Mode
    instruction: _Instruction


class _Journey(BaseModel):
    duration: int = Field(ge=0)
    startDateTime: datetime
    arrivalDateTime: datetime
    legs: list[_Leg]


class _JourneyResult(BaseModel):
    journeys: list[_Journey]


def _local_time(value: datetime) -> datetime:
    return value.replace(tzinfo=LONDON) if value.tzinfo is None else value.astimezone(LONDON)


class CommuteMetadataFetcher:
    type: ClassVar[str] = "commute"
    model: ClassVar[ModelType[BaseModel]] = CommuteAmenities

    def __init__(
        self,
        client: AmenityClient,
        target: str,
        latitude: float,
        longitude: float,
        *,
        arrival_date: date | None = None,
    ):
        self.client = client
        self.target = target
        self.latitude = latitude
        self.longitude = longitude
        self.arrival_date = arrival_date

    async def fetch(self, property: Property) -> dict:
        arrival_date = self.arrival_date or next_weekday(datetime.now(LONDON).date())
        try:
            payload = CommuteAmenities(
                target=self.target,
                target_latitude=self.latitude,
                target_longitude=self.longitude,
                origin_latitude=property.latitude,
                origin_longitude=property.longitude,
                arrive_by=datetime.combine(arrival_date, time(9), tzinfo=LONDON),
            )
            result = await self.client.request(
                "tfl",
                path=(
                    f"/Journey/JourneyResults/{property.latitude},{property.longitude}"
                    f"/to/{self.latitude},{self.longitude}"
                ),
                params={
                    "date": arrival_date.strftime("%Y%m%d"),
                    "time": "0900",
                    "timeIs": "Arriving",
                    "journeyPreference": "LeastTime",
                    "calcOneDirection": "true",
                },
            )
            journeys = []
            for journey in _JourneyResult.model_validate(result).journeys:
                journey.startDateTime = _local_time(journey.startDateTime)
                journey.arrivalDateTime = _local_time(journey.arrivalDateTime)
                if (
                    journey.startDateTime <= journey.arrivalDateTime <= payload.arrive_by
                    and journey.arrivalDateTime.date() == arrival_date
                    and any(leg.mode.id in _TRANSIT_MODES for leg in journey.legs)
                ):
                    journeys.append(journey)
            if journeys:
                fastest = min(
                    journeys, key=lambda journey: (journey.duration, journey.arrivalDateTime)
                )
                payload.duration_minutes = fastest.duration
                payload.departure_time = fastest.startDateTime
                payload.arrival_time = fastest.arrivalDateTime
                payload.route = [leg.instruction.summary for leg in fastest.legs]
            return payload.model_dump(mode="json")
        except ValidationError as exc:
            raise FetchError(f"TfL returned invalid commute metadata: {exc}") from exc
