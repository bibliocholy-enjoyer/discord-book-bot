import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    discord_token: str
    hardcover_token: str
    heartbeat_url: str | None = None
    owner_id: int | None = None
    usage_db: str | None = None
    usage_salt: str | None = None
    support_url: str | None = None

    @classmethod
    def from_env(cls) -> "Config":
        missing = [name for name in ("DISCORD_TOKEN", "HARDCOVER_TOKEN") if not os.getenv(name)]
        if missing:
            raise SystemExit(f"Missing required environment variables: {', '.join(missing)}")
        owner_id = os.getenv("OWNER_ID")
        usage_db = os.getenv("USAGE_DB") or None
        usage_salt = os.getenv("USAGE_SALT") or None
        if usage_db and not usage_salt:
            # Without a secret salt the hashes of user IDs could be reversed by trying
            # every ID, so refuse to record anything rather than record weak hashes.
            raise SystemExit("USAGE_DB is set, so USAGE_SALT must be set to a long random string")
        return cls(
            discord_token=os.environ["DISCORD_TOKEN"],
            hardcover_token=os.environ["HARDCOVER_TOKEN"],
            heartbeat_url=os.getenv("HEARTBEAT_URL") or None,
            owner_id=int(owner_id) if owner_id else None,
            usage_db=usage_db,
            usage_salt=usage_salt,
            support_url=os.getenv("SUPPORT_URL") or None,
        )
