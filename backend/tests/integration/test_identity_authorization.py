import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import NegotiationSession, Product
from app.services.errors import ProductNotFoundError
from app.services.product_service import ProductService
from tests.integration.factories import create_negotiation

pytestmark = pytest.mark.mysql_integration


def test_authenticated_seller_identity_cannot_cross_product_ownership(
    service_session_factory: sessionmaker[Session],
) -> None:
    session_id, _ = create_negotiation(service_session_factory)
    with service_session_factory() as db:
        negotiation = db.get(NegotiationSession, session_id)
        assert negotiation is not None
        product = db.get(Product, negotiation.product_id)
        assert product is not None
        product_id = product.id
        seller_id = product.seller_id

    service = ProductService(service_session_factory)

    assert service.get_owned_product(
        product_id=product_id,
        seller_id=seller_id,
    ).id == product_id
    with pytest.raises(ProductNotFoundError, match="无权访问"):
        service.get_owned_product(
            product_id=product_id,
            seller_id="another-seller",
        )
