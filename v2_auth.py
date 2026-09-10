import hashlib

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

import models
from database import get_db


v2_bearer_scheme = HTTPBearer(auto_error=False)


def _hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def get_v2_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(v2_bearer_scheme),
    db: Session = Depends(get_db),
) -> models.User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="valid bearer token required", headers={"WWW-Authenticate": "Bearer"})
    user = db.query(models.User).filter(models.User.api_token_hash == _hash_token(credentials.credentials)).first()
    if user is None:
        raise HTTPException(status_code=401, detail="valid bearer token required", headers={"WWW-Authenticate": "Bearer"})
    return user
