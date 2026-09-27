from pydantic import BaseModel, ConfigDict, Field


class PublicSellerSummaryResponse(BaseModel):
    """商品公开响应中允许展示的最小卖家信息。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    display_name: str


class PublicSellerResponse(PublicSellerSummaryResponse):
    available_product_count: int = Field(ge=1)


class PublicSellerListResponse(BaseModel):
    sellers: list[PublicSellerResponse]
