"""Household profile: who the plan is for, restrictions, and API keys."""

from __future__ import annotations

from dataclasses import dataclass, field

PROFILE_SCHEMA_VERSION = 1

# Names of the API keys the app knows about (also the profile dict keys).
API_KEY_NAMES = (
    "kroger_client_id",
    "kroger_client_secret",
    "instacart_api_key",
    "fdc_api_key",
    "openai_api_key",
    "bls_api_key",
)


@dataclass
class HouseholdProfile:
    adults: int = 1
    children: int = 0
    seniors: int = 0
    vegetarian: bool = False
    allergies: list[str] = field(default_factory=list)
    no_pork: bool = False
    lactose_free: bool = False
    city: str = ""
    zip_code: str = ""
    api_keys: dict[str, str] = field(default_factory=dict)

    @property
    def total_members(self) -> int:
        return self.adults + self.children + self.seniors

    def to_dict(self) -> dict:
        return {
            "version": PROFILE_SCHEMA_VERSION,
            "adults": self.adults,
            "children": self.children,
            "seniors": self.seniors,
            "vegetarian": self.vegetarian,
            "allergies": list(self.allergies),
            "no_pork": self.no_pork,
            "lactose_free": self.lactose_free,
            "city": self.city,
            "zip_code": self.zip_code,
            "api_keys": {k: v for k, v in self.api_keys.items() if v},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "HouseholdProfile":
        return cls(
            adults=int(data.get("adults", 1)),
            children=int(data.get("children", 0)),
            seniors=int(data.get("seniors", 0)),
            vegetarian=bool(data.get("vegetarian", False)),
            allergies=[str(a).strip().lower() for a in data.get("allergies", []) if str(a).strip()],
            no_pork=bool(data.get("no_pork", False)),
            lactose_free=bool(data.get("lactose_free", False)),
            city=str(data.get("city", "")),
            zip_code=str(data.get("zip_code", "")),
            api_keys={k: str(v) for k, v in dict(data.get("api_keys", {})).items() if k in API_KEY_NAMES},
        )
