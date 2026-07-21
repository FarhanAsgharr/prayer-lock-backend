"""Shared response envelopes and base schema configuration."""

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class ORMModel(BaseModel):
    """Base for schemas populated directly from SQLAlchemy instances."""

    model_config = ConfigDict(from_attributes=True)


class ErrorResponse(BaseModel):
    """Uniform error body.

    `code` is a stable machine-readable identifier the mobile client branches
    on; `message` is human-facing text that may be reworded freely without
    breaking clients.
    """

    code: str
    message: str
    detail: dict | None = None


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int
