"""Schemas for the public delivery endpoints (Nova Post phase P1).

The address-field specification is served to the storefront rather than
duplicated in the front-end: the courier form is then built from the contract
instead of from a guess, and adding or relaxing a field is a back-end change
only. The idea is taken from ``ecom-obr``
(``NovaPostaDeliveryIntegrationV2.FIELD_SPECIFICATION``).
"""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

# Pickup-point categories the carrier can serve.
BRANCH: str = "branch"
POSTOMAT: str = "postomat"
COURIER: str = "courier"

# What a Nova Post courier address must contain. ``required`` drives both the
# storefront form and the server-side check, so they cannot drift apart.
ADDRESS_FIELDS: list[dict] = [
    {
        "name": "city",
        "required": True,
        "max_length": 100,
        "label_ru": "Город",
        "label_ro": "Oraș",
    },
    {
        "name": "street",
        "required": True,
        "max_length": 100,
        "label_ru": "Улица",
        "label_ro": "Stradă",
    },
    {
        "name": "building",
        "required": True,
        "max_length": 100,
        "label_ru": "Дом",
        "label_ro": "Casă",
    },
    {
        "name": "postCode",
        "required": True,
        "max_length": 10,
        "label_ru": "Индекс",
        "label_ro": "Cod poștal",
    },
    {
        "name": "flat",
        "required": False,
        "max_length": 10,
        "label_ru": "Квартира",
        "label_ro": "Apartament",
    },
    {
        "name": "block",
        "required": False,
        "max_length": 100,
        "label_ru": "Блок",
        "label_ro": "Bloc",
    },
    {
        "name": "note",
        "required": False,
        "max_length": 100,
        "label_ru": "Примечание",
        "label_ro": "Notă",
    },
]


class DeliveryMethodOut(BaseModel):
    """One delivery option the storefront may offer."""

    # ``own`` = the store's own logistics (pickup / flat-rate courier);
    # ``novapost`` = the carrier.
    service: str
    # pickup | courier | branch | postomat
    type: str
    # Flat price for own logistics; ``None`` when the carrier quotes it.
    flat_cost: Decimal | None = None
    # Goods amount (after discount) above which this method ships free.
    free_from: Decimal | None = None
    # Address fields to collect (courier-to-the-door only).
    address_fields: list[dict] = Field(default_factory=list)


class DeliveryMethodsOut(BaseModel):
    """Envelope for the storefront's delivery-options call."""

    methods: list[DeliveryMethodOut] = Field(default_factory=list)
    novapost_enabled: bool = False


class SettlementQuery(BaseModel):
    """Body of the settlement (city) lookup."""

    query: str = Field(default="", max_length=100)


class DivisionQuery(BaseModel):
    """Body of the pickup-point lookup inside a settlement."""

    settlement_id: str = Field(min_length=1, max_length=64)
    category: str = Field(default=BRANCH, pattern=f"^({BRANCH}|{POSTOMAT})$")
    query: str = Field(default="", max_length=100)


class SettlementOut(BaseModel):
    """A settlement as shown in the city typeahead."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str


class DivisionOut(BaseModel):
    """A pickup point as shown in the branch/postomat list."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    number: str = ""
    address: str = ""
    settlement_name: str = ""


class SettlementListOut(BaseModel):
    """Envelope for the settlement lookup."""

    data: list[SettlementOut] = Field(default_factory=list)


class DivisionListOut(BaseModel):
    """Envelope for the pickup-point lookup."""

    data: list[DivisionOut] = Field(default_factory=list)
