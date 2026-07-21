"""Authentication request and response bodies."""

import uuid

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class FirebaseExchangeRequest(BaseModel):
    id_token: str = Field(min_length=16, description="Firebase ID token from the client SDK")
    timezone: str = Field(
        default="UTC",
        max_length=64,
        description="IANA timezone, used to seed the account on first sign-in",
    )


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=16)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Access token lifetime in seconds")


class UserResponse(ORMModel):
    id: uuid.UUID
    email: str | None
    display_name: str | None
    timezone: str
    is_admin: bool


class AuthResponse(BaseModel):
    user: UserResponse
    tokens: TokenResponse
