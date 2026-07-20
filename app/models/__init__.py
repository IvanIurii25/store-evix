"""ORM models aggregator.

Importing this package registers every domain model on ``Base.metadata`` so
Alembic autogenerate and ``create_all`` see the full schema. Import order is
chosen so cross-domain foreign keys (cart_item / order_item -> product) resolve.
"""

from app.models import cart, catalog, order, promo, settings, user
from app.models.base import Base

__all__ = ["Base", "catalog", "user", "cart", "order", "promo", "settings"]
