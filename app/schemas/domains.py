"""Domain / membership / invite schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, model_validator

from app.rbac import ASSIGNABLE_ROLES, Role


class DomainCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)


class DomainUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    settings: dict | None = None


class DomainOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    description: str | None
    owner_id: uuid.UUID
    settings: dict
    created_at: datetime
    my_role: Role | None = None

    model_config = {"from_attributes": True}


class MemberOut(BaseModel):
    user_id: uuid.UUID
    username: str
    email: EmailStr
    role: Role
    added_at: datetime


class MemberAdd(BaseModel):
    username: str
    role: Role

    @model_validator(mode="after")
    def _assignable(self) -> MemberAdd:
        if self.role not in ASSIGNABLE_ROLES:
            raise ValueError(f"role must be one of {[r.value for r in ASSIGNABLE_ROLES]}")
        return self


class MemberUpdate(BaseModel):
    role: Role

    @model_validator(mode="after")
    def _assignable(self) -> MemberUpdate:
        if self.role not in ASSIGNABLE_ROLES:
            raise ValueError(f"role must be one of {[r.value for r in ASSIGNABLE_ROLES]}")
        return self


class InviteCreate(BaseModel):
    role: Role
    email: EmailStr | None = None
    username: str | None = None

    @model_validator(mode="after")
    def _one_target(self) -> InviteCreate:
        if not self.email and not self.username:
            raise ValueError("email or username is required")
        if self.role not in ASSIGNABLE_ROLES:
            raise ValueError("that role cannot be invited")
        return self


class InviteOut(BaseModel):
    id: uuid.UUID
    domain_id: uuid.UUID
    role: Role
    email: str | None
    username: str | None
    token: str
    expires_at: datetime

    model_config = {"from_attributes": True}
